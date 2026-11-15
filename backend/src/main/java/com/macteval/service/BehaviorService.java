package com.macteval.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.macteval.dto.BehaviorDTO;
import com.macteval.model.entity.AnalysisJobEntity;
import com.macteval.model.entity.BehaviorObservationEntity;
import com.macteval.model.entity.LessonSessionEntity;
import com.macteval.model.entity.BehaviorEvidenceFrameEntity;
import com.macteval.repository.BehaviorEvidenceFrameRepository;
import com.macteval.repository.BehaviorObservationRepository;
import com.macteval.repository.LessonSessionRepository;
import com.macteval.security.ResourceGuard;
import com.macteval.websocket.ClassroomWebSocketHandler;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;
import java.time.temporal.ChronoUnit;
import java.util.List;
import java.util.Map;
import java.util.NoSuchElementException;
import java.util.UUID;

@Slf4j
@Service
@RequiredArgsConstructor
public class BehaviorService {

    private final BehaviorObservationRepository behaviorRepository;
    private final BehaviorEvidenceFrameRepository evidenceFrameRepository;
    private final LessonSessionRepository lessonSessionRepository;
    private final ObjectMapper objectMapper;
    private final ResourceGuard resourceGuard;
    private final ClassroomWebSocketHandler webSocketHandler;

    /** 视频分析任务完成后：观测窗口落库 + 聚合推送（REQUIRES_NEW 隔离，失败不影响 job 状态）。 */
    @Transactional(propagation = org.springframework.transaction.annotation.Propagation.REQUIRES_NEW)
    public void ingestCompletedVideoJob(AnalysisJobEntity job) {
        if (job.getType() != AnalysisJobEntity.JobType.VIDEO_ANALYSIS
                || job.getStatus() != AnalysisJobEntity.JobStatus.SUCCEEDED) {
            return;
        }
        Map<String, Object> params = parseJson(job.getParams());
        Map<String, Object> result = parseJson(job.getResultMetadata());
        String lessonSessionId = str(params.get("lessonSessionId"));
        if (lessonSessionId == null || result.isEmpty()) {
            return;
        }
        Object rawObs = result.get("observations");
        if (!(rawObs instanceof List<?> observations)) {
            log.warn("Video job {} has no observations list", job.getId());
            return;
        }

        // 生产者 window_*_ms 是视频相对偏移（自第 0 帧起），非 epoch——以任务创建时间为锚转为有意义的时间
        LocalDateTime anchor = job.getCreatedAt() != null ? job.getCreatedAt() : LocalDateTime.now();
        long windowDurationMs = windowDurationMs(params);
        BehaviorObservationEntity last = null;
        int savedCount = 0;
        for (Object o : observations) {
            if (!(o instanceof Map<?, ?> m)) continue;
            Long startMs = longValue(m.get("window_start_ms"));
            Long endMs = longValue(m.get("window_end_ms"));
            if (startMs == null || endMs == null) {
                log.warn("Skip observation with missing window times for job {}", job.getId());
                continue;
            }
            Integer explicitIndex = intValue(m.get("window_index"));
            int windowIndex = explicitIndex != null
                    ? explicitIndex
                    : Math.max(0, (int) (startMs / Math.max(1, windowDurationMs)));
            // 同一 job + windowIndex 是幂等键：重复/部分重放只更新原行，不新增观测。
            BehaviorObservationEntity e = behaviorRepository
                    .findByJobIdAndWindowIndex(job.getId(), windowIndex)
                    .orElseGet(() -> {
                        BehaviorObservationEntity created = new BehaviorObservationEntity();
                        created.setId(UUID.randomUUID().toString());
                        created.setJobId(job.getId());
                        created.setWindowIndex(windowIndex);
                        return created;
                    });
            e.setLessonSessionId(lessonSessionId);
            e.setWindowStart(anchor.plus(startMs, ChronoUnit.MILLIS));
            e.setWindowEnd(anchor.plus(endMs, ChronoUnit.MILLIS));
            e.setWindowStartOffsetMs(startMs);
            e.setWindowEndOffsetMs(endMs);
            e.setMediaAnchor(anchor);
            e.setVisibleSeatCount(intValue(m.get("visible_seat_count")));
            e.setTotalConfiguredSeats(intValue(m.get("total_configured_seats")));
            e.setCoverage(doubleValue(m.get("coverage")));
            e.setDataQuality(str(m.get("data_quality")));
            applySceneQuality(e, startMs, explicitIndex, windowDurationMs, result);
            e.setInSeatCount(intValue(m.get("in_seat_count")));
            e.setLeaveSeatCount(intValue(m.get("leave_seat_count")));
            e.setHandRaisedCount(intValue(m.get("hand_raised_count")));
            e.setStandingCount(intValue(m.get("standing_count")));
            e.setUprightPostureCount(intValue(m.get("upright_posture_count")));
            e.setAggregatedUprightProportion(str(m.get("upright_proportion")));
            behaviorRepository.save(e);
            last = e;
            savedCount++;
        }
        persistEvidenceFrames(job, lessonSessionId, result);
        log.info("Persisted {} behavior observations for lesson {} from job {}", savedCount, lessonSessionId, job.getId());
        if (last != null) {
            pushBehaviorUpdate(lessonSessionId, last);
        }
    }

    /** 保存/更新 AI 抽帧证据；重放同一任务时先清空该任务旧帧再写入。 */
    private void persistEvidenceFrames(AnalysisJobEntity job, String lessonSessionId,
                                       Map<String, Object> result) {
        Object raw = result.get("evidence_frames");
        if (!(raw instanceof List<?> frames)) {
            return;
        }
        evidenceFrameRepository.deleteByJobId(job.getId());
        int saved = 0;
        for (Object item : frames) {
            if (!(item instanceof Map<?, ?> m)) continue;
            Integer windowIndex = intValue(m.get("window_index"));
            String bucket = str(m.get("bucket"));
            String objectKey = str(m.get("object_name"));
            String code = str(m.get("code"));
            if (windowIndex == null || bucket == null || objectKey == null || code == null) {
                continue;
            }
            BehaviorEvidenceFrameEntity entity = new BehaviorEvidenceFrameEntity();
            entity.setId(UUID.randomUUID().toString());
            entity.setLessonSessionId(lessonSessionId);
            entity.setJobId(job.getId());
            entity.setWindowIndex(windowIndex);
            entity.setTimestampMs(longValue(m.get("timestamp_ms")));
            entity.setBucket(bucket);
            entity.setObjectKey(objectKey);
            entity.setCode(code);
            entity.setLabel(str(m.get("label")));
            if (m.get("metadata") instanceof Map<?, ?> metadata) {
                try {
                    entity.setMetadata(objectMapper.writeValueAsString(metadata));
                } catch (Exception e) {
                    log.warn("Could not serialize evidence metadata for job {}", job.getId());
                }
            }
            evidenceFrameRepository.save(entity);
            saved++;
        }
        log.info("Persisted {} evidence frames for job {}", saved, job.getId());
    }

    /** 兼容旧调用：默认 60s 窗口、无显式 windowIndex。 */
    static void applySceneQuality(BehaviorObservationEntity observation, long startMs, Map<String, Object> result) {
        applySceneQuality(observation, startMs, null, 60_000L, result);
    }

    /**
     * 把场景门控质量映射到对应窗口。
     *
     * <p>Python 端按任务配置的 windowDuration 生成 window_index。Java 端不能
     * 固定按 60s 反推索引，否则 30s 窗口会关联到错误的质量元数据。优先使用
     * 结果中显式的 window_index，缺失时才按任务的窗口时长换算。</p>
     */
    static void applySceneQuality(BehaviorObservationEntity observation, long startMs,
                                  Integer windowIndex, long windowDurationMs,
                                  Map<String, Object> result) {
        Object raw = result.get("window_scene_metadata");
        if (!(raw instanceof List<?> windows)) return;
        long duration = windowDurationMs > 0 ? windowDurationMs : 60_000L;
        int index = windowIndex != null
                ? windowIndex
                : Math.max(0, (int) (startMs / duration));
        Map<?, ?> metadata = null;
        for (Object item : windows) {
            if (item instanceof Map<?, ?> candidate
                    && candidate.get("window_index") instanceof Number n
                    && n.intValue() == index) {
                metadata = candidate;
                break;
            }
        }
        if (metadata == null) return;
        Object quality = metadata.get("data_quality");
        if (quality != null) {
            var ranks = Map.of("unavailable", 0, "limited", 1, "usable", 2);
            String sceneQuality = String.valueOf(quality);
            Integer current = observation.getDataQuality() == null
                    ? null : ranks.get(observation.getDataQuality());
            Integer scene = ranks.get(sceneQuality);
            // A usable camera view cannot make an unsupported measurement available.
            if (scene != null && (current == null || scene < current)) {
                observation.setDataQuality(sceneQuality);
            }
        }
    }

    /** 从任务参数读取分析窗口时长（毫秒），缺省/非法时回退 60s。 */
    static long windowDurationMs(Map<String, Object> params) {
        Object raw = params.get("windowDuration");
        if (raw == null) raw = params.get("window_duration");
        long seconds = 60;
        if (raw instanceof Number n) {
            seconds = n.longValue();
        } else if (raw != null) {
            try {
                seconds = Long.parseLong(String.valueOf(raw));
            } catch (NumberFormatException ignored) {
                seconds = 60;
            }
        }
        if (seconds <= 0 || seconds > 3600) seconds = 60;
        return seconds * 1000L;
    }

    @Transactional(readOnly = true)
    public List<BehaviorDTO> getByLesson(String lessonSessionId, String authenticatedTeacherId) {
        LessonSessionEntity session = lessonSessionRepository.findById(lessonSessionId)
                .orElseThrow(() -> new NoSuchElementException("课堂不存在"));
        resourceGuard.assertTeacherOwnsCourse(authenticatedTeacherId, session.getTeacherId());
        return behaviorRepository.findByLessonSessionIdOrderByWindowStartAsc(lessonSessionId).stream()
                .map(this::toDTO).toList();
    }

    /** 推送本批最后一个窗口的聚合指标（供实时监控前端消费）。 */
    private void pushBehaviorUpdate(String lessonSessionId, BehaviorObservationEntity last) {
        Double upright = parseProportion(last.getAggregatedUprightProportion());
        Map<String, Object> payload = new java.util.LinkedHashMap<>();
        payload.put("upright_proportion", upright);
        payload.putAll(Map.of(
                "coverage", last.getCoverage() != null ? last.getCoverage() : 0.0,
                "data_quality", last.getDataQuality() != null ? last.getDataQuality() : "unavailable",
                "visible_seat_count", last.getVisibleSeatCount() != null ? last.getVisibleSeatCount() : 0,
                "total_configured_seats", last.getTotalConfiguredSeats() != null ? last.getTotalConfiguredSeats() : 0,
                "visible_person_count", last.getVisibleSeatCount() != null ? last.getVisibleSeatCount() : 0
        ));
        payload.put("upright_posture_count", last.getUprightPostureCount());
        try {
            webSocketHandler.pushBehaviorUpdate(lessonSessionId, payload);
        } catch (Exception e) {
            log.warn("Failed to push behavior update for {}: {}", lessonSessionId, e.getMessage());
        }
    }

    private BehaviorDTO toDTO(BehaviorObservationEntity e) {
        Double upright = parseProportion(e.getAggregatedUprightProportion());
        return new BehaviorDTO(
                e.getId(), e.getLessonSessionId(), e.getWindowStart(), e.getWindowEnd(),
                e.getVisibleSeatCount(), e.getTotalConfiguredSeats(), e.getCoverage(),
                e.getDataQuality(), e.getInSeatCount(), e.getLeaveSeatCount(), e.getHandRaisedCount(),
                e.getStandingCount(), e.getUprightPostureCount(), upright, e.getCreatedAt());
    }

    private Long longValue(Object v) {
        if (v == null) return null;
        try {
            return v instanceof Number n ? n.longValue() : Long.parseLong(String.valueOf(v));
        } catch (NumberFormatException e) {
            return null;
        }
    }

    private Double parseProportion(String s) {
        if (s == null) return null;
        try {
            double value = Double.parseDouble(s);
            return Double.isFinite(value) && value >= 0 && value <= 1 ? value : null;
        } catch (NumberFormatException e) {
            return null;
        }
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> parseJson(String json) {
        if (json == null || json.isBlank()) return Map.of();
        try {
            return objectMapper.readValue(json, Map.class);
        } catch (Exception e) {
            return Map.of();
        }
    }

    private String str(Object v) {
        return v == null ? null : String.valueOf(v);
    }

    private Integer intValue(Object v) {
        if (v == null) return null;
        try {
            return v instanceof Number n ? n.intValue() : Integer.parseInt(String.valueOf(v));
        } catch (NumberFormatException e) {
            return null;
        }
    }

    private Double doubleValue(Object v) {
        if (v == null) return null;
        try {
            return v instanceof Number n ? n.doubleValue() : Double.parseDouble(String.valueOf(v));
        } catch (NumberFormatException e) {
            return null;
        }
    }
}
