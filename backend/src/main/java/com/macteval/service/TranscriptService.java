package com.macteval.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.macteval.dto.LessonTranscriptDTO;
import com.macteval.model.entity.AnalysisJobEntity;
import com.macteval.model.entity.LessonSessionEntity;
import com.macteval.model.entity.LessonTranscriptEntity;
import com.macteval.repository.LessonSessionRepository;
import com.macteval.repository.LessonTranscriptRepository;
import com.macteval.security.ResourceGuard;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;
import java.util.Map;
import java.util.NoSuchElementException;
import java.util.UUID;

@Slf4j
@Service
@RequiredArgsConstructor
public class TranscriptService {

    private final LessonTranscriptRepository transcriptRepository;
    private final LessonSessionRepository lessonSessionRepository;
    private final ObjectMapper objectMapper;
    private final ResourceGuard resourceGuard;

    /** 音频分析任务完成后：转写文本落库（每次分析一条，getByLesson 取最新）。 */
    @Transactional(propagation = org.springframework.transaction.annotation.Propagation.REQUIRES_NEW)
    public void ingestCompletedAudioJob(AnalysisJobEntity job) {
        if (job.getType() != AnalysisJobEntity.JobType.AUDIO_ANALYSIS
                || job.getStatus() != AnalysisJobEntity.JobStatus.SUCCEEDED) {
            return;
        }
        Map<String, Object> params = parseJson(job.getParams());
        Map<String, Object> result = parseJson(job.getResultMetadata());
        String lessonSessionId = str(params.get("lessonSessionId"));
        String text = str(result.get("text"));
        if (lessonSessionId == null || result.isEmpty()
                || (!result.containsKey("text") && !result.containsKey("segments"))) {
            log.warn("Skip transcript ingest for job {}: missing lessonSessionId/text/segments", job.getId());
            return;
        }

        if (text == null) {
            // 成功的 ASR 任务可以是整段静音（text 为空、segments 为空数组）。
            // 必须落库，后续窗口对齐才能把静音窗口记为有效 0 分，而不是缺失。
            text = "";
        }

        // 同一任务回调重放只更新原转写行（V22 唯一索引兜底）。
        LessonTranscriptEntity entity = transcriptRepository.findByJobId(job.getId())
                .orElseGet(() -> {
                    LessonTranscriptEntity created = new LessonTranscriptEntity();
                    created.setId(UUID.randomUUID().toString());
                    created.setJobId(job.getId());
                    return created;
                });
        entity.setLessonSessionId(lessonSessionId);
        entity.setText(text);
        entity.setLanguage(str(result.get("language")));
        entity.setLanguageProbability(doubleValue(result.get("language_probability")));
        entity.setSegments(writeJson(result.get("segments")));
        entity.setModelVersion(str(result.get("model_version")));
        Long mediaDuration = longValue(result.get("media_duration_ms"));
        entity.setMediaDurationMs(mediaDuration);
        if (mediaDuration != null) {
            entity.setCoverageStartMs(0L);
            entity.setCoverageEndMs(mediaDuration);
        }
        entity.setModelDigest(str(result.get("model_digest")));
        entity.setConfigJson(writeJson(result.get("config")));
        transcriptRepository.save(entity);
        log.info("Transcript persisted for lesson {} from job {}", lessonSessionId, job.getId());
    }

    @Transactional(readOnly = true)
    public LessonTranscriptDTO getByLesson(String lessonSessionId, String authenticatedTeacherId) {
        LessonSessionEntity session = lessonSessionRepository.findById(lessonSessionId)
                .orElseThrow(() -> new NoSuchElementException("课堂不存在"));
        resourceGuard.assertTeacherOwnsCourse(authenticatedTeacherId, session.getTeacherId());
        LessonTranscriptEntity entity = transcriptRepository
                .findFirstByLessonSessionIdOrderByCreatedAtDescIdDesc(lessonSessionId)
                .orElseThrow(() -> new NoSuchElementException("该课堂尚无转写记录"));
        return toDTO(entity);
    }

    private LessonTranscriptDTO toDTO(LessonTranscriptEntity e) {
        return new LessonTranscriptDTO(
                e.getId(), e.getLessonSessionId(), e.getText(),
                e.getLanguage(), e.getLanguageProbability(),
                parseSegments(e.getSegments()), e.getModelVersion(), e.getCreatedAt());
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

    private List<Map<String, Object>> parseSegments(String json) {
        if (json == null || json.isBlank()) return List.of();
        try {
            return objectMapper.readValue(json, new TypeReference<List<Map<String, Object>>>() {});
        } catch (Exception e) {
            return List.of();
        }
    }

    private String str(Object v) {
        return v == null ? null : String.valueOf(v);
    }

    private Long longValue(Object v) {
        if (v == null) return null;
        try {
            return v instanceof Number n ? n.longValue() : Long.parseLong(String.valueOf(v));
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

    private String writeJson(Object v) {
        if (v == null) return null;
        try {
            return objectMapper.writeValueAsString(v);
        } catch (Exception e) {
            return null;
        }
    }
}
