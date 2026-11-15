package com.macteval.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.macteval.model.entity.BehaviorObservationEntity;
import com.macteval.model.entity.LessonTranscriptEntity;
import com.macteval.model.entity.LessonWindowFeatureEntity;
import com.macteval.model.entity.BehaviorEvidenceFrameEntity;
import com.macteval.repository.AnalysisJobRepository;
import com.macteval.repository.BehaviorEvidenceFrameRepository;
import com.macteval.repository.BehaviorObservationRepository;
import com.macteval.repository.LessonTranscriptRepository;
import com.macteval.repository.LessonWindowFeatureRepository;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Duration;
import java.time.LocalDateTime;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;

/**
 * 统一时间窗对齐（导师建议：明确多模态时间对齐方案，设计统一时间窗粒度）。
 * <p>
 * 将已落库的视频行为观测（窗口）与语音转写分段（带 start/end 秒）按统一 60 秒窗口
 * 对齐聚合：窗口级视频/语音得分由 {@link RuleBaselineService} 规则基线计算，
 * 经 {@link FusionService} 晚期加权融合后落库 lesson_window_features。
 * <p>
 * 时间基准：两类数据均为相对各自媒体起点的偏移（视频观测锚 job.createdAt、
 * 转写为秒偏移），以首个行为观测窗口起点为 0 锚点换算毫秒偏移后按 60s 窗口切分，
 * 窗口索引对齐（同一堂课视频与音频时长一致的前提）。
 * <p>
 * 版本语义（R04）：窗口行记录产生它的 video_job_id / audio_job_id，行的唯一身份是
 * (lesson_session_id, video_job_id, audio_job_id, window_index)。对齐只重建自己
 * 版本组合的行，旧任务的补偿不会再覆盖新版本窗口。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class WindowAlignmentService {

    private final BehaviorObservationRepository behaviorRepository;
    private final LessonTranscriptRepository transcriptRepository;
    private final LessonWindowFeatureRepository windowRepository;
    private final FusionService fusionService;
    private final RuleBaselineService ruleBaselineService;
    private final ObjectMapper objectMapper;
    private final com.macteval.repository.LessonSessionRepository lessonSessionRepository;
    private final com.macteval.security.ResourceGuard resourceGuard;
    private final AnalysisJobRepository analysisJobRepository;
    private final BehaviorEvidenceFrameRepository evidenceFrameRepository;
    private final MinioService minioService;

    /**
     * 一条行为观测与当前评估窗口的重叠片段。
     *
     * <p>跨窗观测不能在每个窗口都按完整窗口权重重复平均；按实际重叠秒数
     * 加权，使一条 120s 观测对相邻两个 60s 窗口各贡献 50% 权重。</p>
     */
    record WindowObservation(BehaviorObservationEntity observation, double overlapSeconds) {
    }

    static Double videoScoreOrNull(RuleBaselineService ruleBaselineService,
                                   List<BehaviorObservationEntity> observations) {
        return videoScoreForWindow(ruleBaselineService, observations.stream()
                .map(o -> new WindowObservation(o, 1.0))
                .toList());
    }

    static Double videoScoreForWindow(RuleBaselineService ruleBaselineService,
                                      List<WindowObservation> observations) {
        if (observations.isEmpty() || observations.stream()
                .anyMatch(w -> "unavailable".equals(w.observation().getDataQuality()))) {
            return null;
        }
        // 当前行为观测提供 posture（端正坐姿比例）与 positive_behavior_ratio（举手/可见座位），
        // stability 需要头部姿态序列，当前实体无该字段，按缺失分量重归一化。
        return ruleBaselineService.videoScore(
                averageUprightProportion(observations),
                null,
                positiveBehaviorRatio(observations));
    }

    /** 窗口内有效端正坐姿比例（按重叠秒数加权，0-1）；无有效值返回 null。 */
    static Double averageUprightProportion(List<WindowObservation> observations) {
        double weightedSum = 0;
        double weight = 0;
        for (WindowObservation w : observations) {
            Double proportion = parseProportionOrNull(w.observation().getAggregatedUprightProportion());
            if (proportion != null && w.overlapSeconds() > 0) {
                weightedSum += proportion * w.overlapSeconds();
                weight += w.overlapSeconds();
            }
        }
        return weight == 0 ? null : weightedSum / weight;
    }

    /**
     * 积极行为比例 = 举手总数 / max(1, 可见座位总数)（0-1）。
     * 当前实体没有 reading/writing 检测与总检测数，以可见座位（缺失时用在座人数）为分母；
     * 无举手计数或无分母时返回 null，按缺失分量处理。
     * <p>
     * 重要：当前 ai-service 检测器尚未实现举手识别，{@code hand_raised_count=0}
     * 是占位值而不是真实观测。因此“全部观测的举手数均为 0”按缺失处理，
     * 避免把占位 0 当作真实零值、以 0.3 权重系统性压低视频分。待举手检测
     * 接入后，应改为由检测器显式提供可用性标记。
     */
    static Double positiveBehaviorRatio(List<WindowObservation> observations) {
        boolean hasPositiveHandRaised = false;
        double handRaised = 0;
        double detected = 0;
        for (WindowObservation w : observations) {
            BehaviorObservationEntity o = w.observation();
            double fraction = overlapFraction(w);
            if (fraction <= 0) continue;
            if (o.getHandRaisedCount() != null) {
                int count = Math.max(0, o.getHandRaisedCount());
                handRaised += count * fraction;
                if (count > 0) hasPositiveHandRaised = true;
            }
            if (o.getVisibleSeatCount() != null && o.getVisibleSeatCount() > 0) {
                detected += o.getVisibleSeatCount() * fraction;
            } else if (o.getInSeatCount() != null && o.getInSeatCount() > 0) {
                detected += o.getInSeatCount() * fraction;
            }
        }
        if (!hasPositiveHandRaised || detected <= 0) return null;
        return Math.min(1.0, handRaised / detected);
    }

    /** 观测被当前窗口覆盖的比例（0-1）：用于把计数类指标按重叠部分折算。 */
    private static double overlapFraction(WindowObservation w) {
        if (w.observation().getWindowStart() == null || w.observation().getWindowEnd() == null) {
            return 0;
        }
        double duration = Duration.between(
                w.observation().getWindowStart(), w.observation().getWindowEnd()).toMillis() / 1000.0;
        if (duration <= 0) return 0;
        return Math.max(0, Math.min(1.0, w.overlapSeconds() / duration));
    }

    static Map<String, Object> videoFeatureMetadata(List<BehaviorObservationEntity> observations) {
        java.util.LinkedHashMap<String, Object> features = new java.util.LinkedHashMap<>();
        List<WindowObservation> weighted = observations.stream()
                .map(o -> new WindowObservation(o, 1.0))
                .toList();
        Double upright = averageUprightProportion(weighted);
        if (upright != null) features.put("upright_proportion", upright);
        Double positiveBehavior = positiveBehaviorRatio(weighted);
        if (positiveBehavior != null) features.put("positive_behavior_ratio", positiveBehavior);
        features.put("observation_count", observations.size());
        // 在线视频特征是 v1 的系统代理：posture 用端正坐姿比例而非头部 pitch，
        // stability 不可用，positive_behavior 用举手/可见座位而非三类行为比例。
        // 与离线 v1 特征分开标注，避免 E6 在线/离线结果被误认为同一口径。
        features.put("rule_protocol", "v1-system-proxy");
        features.put("rule_protocol_note",
                "posture=upright proportion; positive_behavior=hand_raises/visible_seats; "
                        + "stability unavailable");
        String quality = observations.stream().map(BehaviorObservationEntity::getDataQuality)
                .filter(java.util.Objects::nonNull)
                .min(java.util.Comparator.comparingInt(WindowAlignmentService::qualityRank))
                .orElse(null);
        if (quality != null) features.put("data_quality", quality);
        return features;
    }

    private static int qualityRank(String quality) {
        return switch (quality) {
            case "unavailable" -> 0;
            case "limited" -> 1;
            case "usable" -> 2;
            default -> 0;
        };
    }

    /** 语音窗口得分：走 {@link RuleBaselineService#speechScore}（spec 5.2 权重 0.5/0.3/0.2）。 */
    Double speechScoreFor(SpeechMetricsCalculator.WindowSpeechMetrics m, double windowSeconds) {
        return ruleBaselineService.speechScore(
                m.speechCoverage(), m.questionCount(), m.speechRate(), windowSeconds);
    }

    /**
     * 对齐并重建指定课堂的全部窗口特征（幂等：先清后建）。
     * 视频或语音任一有数据即执行；两者皆无则跳过。
     * REQUIRES_NEW：在 AnalysisJobService.completeJob 事务内被调用，
     * 独立事务隔离，对齐失败回滚不影响任务 SUCCEEDED 状态落库。
     */
    public void alignLesson(String lessonSessionId) {
        alignLesson(lessonSessionId, null, null);
    }

    /**
     * 对齐指定分析版本的观测/转写。
     *
     * <p>回调内调用时作业状态尚未随外层事务提交，因此不能靠“查询最新成功任务”
     * 找到刚完成的版本；调用方显式传入 jobId，直接按 job_id 读取已提交的观测。</p>
     *
     * @param videoJobId 本次完成的视频任务；null 时回退到最近一次成功任务
     * @param audioJobId 本次完成的音频任务；null 时回退到最近一次成功任务
     */
    @Transactional(propagation = org.springframework.transaction.annotation.Propagation.REQUIRES_NEW)
    public void alignLesson(String lessonSessionId, String videoJobId, String audioJobId) {
        alignVersion(lessonSessionId, videoJobId, audioJobId);
    }

    /**
     * 对齐指定分析版本，并返回本次写入的版本组合（供调用方核对写入了哪一版）。
     *
     * <p>无任何数据可对齐时不写库，但仍返回版本组合，便于调用方记录“已处理”。
     * 版本组合即窗口行的身份：空串表示该模态本版本没有任务。</p>
     */
    @Transactional(propagation = org.springframework.transaction.annotation.Propagation.REQUIRES_NEW)
    public WindowVersion alignVersion(String lessonSessionId, String videoJobId, String audioJobId) {
        // 只使用“最近一次成功分析”的观测：历史重跑结果不参与对齐，避免同一媒体
        // 隔日重跑后把课堂时间跨度拉长，也避免旧版本结果覆盖新版本。
        // 版本组合的“实际”取值：显式传入的 job 就是本版本身份；回退路径按实际读到的
        // 最新成功任务记录，落到整课堂全量读取（历史数据）时保持无版本（空串）。
        String actualVideoJobId = videoJobId;
        List<BehaviorObservationEntity> behaviors;
        if (videoJobId != null) {
            behaviors = behaviorRepository
                    .findByLessonSessionIdAndJobIdOrderByWindowIndexAsc(lessonSessionId, videoJobId);
        } else {
            var latestVideoJob = latestSuccessfulJob(
                    lessonSessionId, com.macteval.model.entity.AnalysisJobEntity.JobType.VIDEO_ANALYSIS);
            List<BehaviorObservationEntity> latestBehaviors = latestVideoJob
                    .map(job -> behaviorRepository
                            .findByLessonSessionIdAndJobIdOrderByWindowIndexAsc(lessonSessionId, job.getId()))
                    .orElse(List.of());
            if (!latestBehaviors.isEmpty()) {
                behaviors = latestBehaviors;
                actualVideoJobId = latestVideoJob.map(
                        com.macteval.model.entity.AnalysisJobEntity::getId).orElse(null);
            } else {
                behaviors = behaviorRepository
                        .findByLessonSessionIdOrderByWindowStartAsc(lessonSessionId);
            }
        }

        String actualAudioJobId = audioJobId;
        LessonTranscriptEntity transcript;
        if (audioJobId != null) {
            transcript = transcriptRepository.findByJobId(audioJobId).orElse(null);
        } else {
            var latestAudioJob = latestSuccessfulJob(
                    lessonSessionId, com.macteval.model.entity.AnalysisJobEntity.JobType.AUDIO_ANALYSIS);
            java.util.Optional<LessonTranscriptEntity> latestTranscript = latestAudioJob
                    .flatMap(job -> transcriptRepository.findByJobId(job.getId()));
            if (latestTranscript.isPresent()) {
                transcript = latestTranscript.get();
                actualAudioJobId = latestAudioJob.map(
                        com.macteval.model.entity.AnalysisJobEntity::getId).orElse(null);
            } else {
                transcript = transcriptRepository
                        .findFirstByLessonSessionIdOrderByCreatedAtDescIdDesc(lessonSessionId)
                        .orElse(null);
            }
        }
        List<Map<String, Object>> parsedSegments =
                transcript == null ? null : parseSegments(transcript.getSegments());
        // transcript 行存在且 segments 是合法 JSON 列表（包括空数组）才算 ASR 可用；
        // segments 缺失/损坏属于 ASR 不可用，不能伪造成“整段静音”。
        boolean speechAvailable = transcript != null && parsedSegments != null;
        List<Map<String, Object>> segments = speechAvailable ? parsedSegments : List.of();
        WindowVersion version = new WindowVersion(normalize(actualVideoJobId), normalize(actualAudioJobId));
        if (behaviors.isEmpty() && !speechAvailable) {
            return version;
        }

        // 有媒体锚点/偏移时按媒体时间轴对齐，避免首个窗口缺失时把后续窗口
        // 重新编号到 0（旧实现只用首条观测作为 0 锚点）。
        LocalDateTime zeroAnchor;
        long videoSpanMs;
        if (!behaviors.isEmpty() && behaviors.get(0).getMediaAnchor() != null
                && behaviors.get(0).getWindowEndOffsetMs() != null) {
            zeroAnchor = behaviors.get(0).getMediaAnchor();
            videoSpanMs = behaviors.stream()
                    .map(BehaviorObservationEntity::getWindowEndOffsetMs)
                    .filter(java.util.Objects::nonNull)
                    .mapToLong(Long::longValue)
                    .max().orElse(0L);
        } else {
            zeroAnchor = behaviors.isEmpty()
                    ? LocalDateTime.now()
                    : behaviors.get(0).getWindowStart();
            videoSpanMs = videoSpanMs(behaviors);
        }
        // 语音覆盖以媒体总时长为准（含尾部静音），而不是最后一句话的时间。
        Long mediaDurationMs = transcript == null ? null : transcript.getMediaDurationMs();
        long speechSpanMs = mediaDurationMs != null ? mediaDurationMs : segmentsSpanMs(segments);
        long totalMs = Math.max(videoSpanMs, speechSpanMs);
        long windowMs = (long) (SpeechMetricsCalculator.WINDOW_SECONDS * 1000);
        int windowCount = (int) Math.max(1, Math.ceil(totalMs / (double) windowMs));

        // 只清空同一版本组合：旧任务的补偿对齐不得删除新版本已发布的窗口（R04）。
        windowRepository.deleteByLessonSessionIdAndJobIds(
                lessonSessionId, version.videoJobId(), version.audioJobId());
        List<LessonWindowFeatureEntity> saved = new ArrayList<>();
        boolean mediaDurationKnown = mediaDurationMs != null && mediaDurationMs > 0;
        for (int i = 0; i < windowCount; i++) {
            long windowStartOffsetMs = (long) i * windowMs;
            // 尾窗按真实剩余时长计算，不能把不足 60s 的尾部当成完整窗口。
            long windowEndOffsetMs = Math.min(windowStartOffsetMs + windowMs, totalMs);
            double windowSeconds = Math.max(0.001, (windowEndOffsetMs - windowStartOffsetMs) / 1000.0);
            LocalDateTime ws = zeroAnchor.plus(windowStartOffsetMs, ChronoUnit.MILLIS);
            LocalDateTime we = zeroAnchor.plus(windowEndOffsetMs, ChronoUnit.MILLIS);

            // 半开区间 [ws, we)：end == ws 或 start == we 的观测属于相邻窗口，
            // 不能重复计入本窗口。跨窗观测按实际重叠秒数加权。
            List<WindowObservation> winObs = behaviors.stream()
                    .filter(o -> o.getWindowEnd() != null && o.getWindowStart() != null
                            && o.getWindowEnd().isAfter(ws) && o.getWindowStart().isBefore(we))
                    .map(o -> new WindowObservation(o, overlapSeconds(o, ws, we)))
                    .toList();
            Double videoScore = winObs.isEmpty() ? null : videoScoreForWindow(ruleBaselineService, winObs);

            // available: 音频覆盖该窗口（窗口内无语音是有效 0 分）。
            // not_covered: 音频时长在该窗口开始前已结束，不能当成“静音 0 分”。
            boolean speechCovered = speechAvailable
                    && (!mediaDurationKnown || windowStartOffsetMs < mediaDurationMs);
            SpeechMetricsCalculator.WindowSpeechMetrics m = SpeechMetricsCalculator.windowMetrics(
                    segments, windowStartOffsetMs / 1000.0, windowEndOffsetMs / 1000.0);
            Double speechScore = speechCovered ? speechScoreFor(m, windowSeconds) : null;

            Map<String, Double> modality = new java.util.LinkedHashMap<>();
            modality.put(FusionService.VIDEO, videoScore);
            modality.put(FusionService.SPEECH, speechScore);
            FusionService.FusedScore fused = fusionService.lateFuse(modality);
            if (fused == null) continue; // 该窗口无任何模态数据

            LessonWindowFeatureEntity e = new LessonWindowFeatureEntity();
            e.setId(UUID.randomUUID().toString());
            e.setLessonSessionId(lessonSessionId);
            e.setVideoJobId(version.videoJobId());
            e.setAudioJobId(version.audioJobId());
            e.setWindowIndex(i);
            e.setWindowStart(ws);
            e.setWindowEnd(we);
            e.setVideoScore(videoScore);
            e.setSpeechScore(speechScore);
            e.setFusedScore(fused.total());
            e.setFusionFeatures(writeJson(Map.of(
                    "weight_source", fused.weightSource().name(),
                    "weights", fused.weights(),
                    "contributions", fused.contributions())));
            e.setVideoFeatures(winObs.isEmpty() ? null : writeJson(videoFeatureMetadata(
                    winObs.stream().map(WindowObservation::observation).toList())));
            if (speechAvailable) {
                java.util.LinkedHashMap<String, Object> speechFeatures = new java.util.LinkedHashMap<>();
                speechFeatures.put("speech_coverage", m.speechCoverage());
                speechFeatures.put("question_count", m.questionCount());
                speechFeatures.put("speech_rate", m.speechRate());
                speechFeatures.put("rule_protocol", "v1");
                speechFeatures.put("coverage_state", speechCovered ? "available" : "not_covered");
                speechFeatures.put("window_duration_seconds", windowSeconds);
                if (mediaDurationKnown) speechFeatures.put("media_duration_ms", mediaDurationMs);
                e.setSpeechFeatures(writeJson(speechFeatures));
            } else {
                e.setSpeechFeatures(null);
            }
            e.setCreatedAt(LocalDateTime.now());
            saved.add(e);
        }
        if (!saved.isEmpty()) {
            windowRepository.saveAll(saved);
            log.info("Aligned {} windows for lesson {} (videoJob={}, audioJob={})",
                    saved.size(), lessonSessionId, version.videoJobId(), version.audioJobId());
        }
        return version;
    }

    /** 一次对齐对应的分析版本组合；空串表示该模态本版本没有任务。 */
    public record WindowVersion(String videoJobId, String audioJobId) {
    }

    /** 空串统一表示“该模态本版本没有任务”（与历史行的 NULL 等价）。 */
    private static String normalize(String jobId) {
        return jobId == null || jobId.isBlank() ? WindowVersionSelector.NO_JOB : jobId;
    }

    /**
     * 该课堂某类型最近一次成功任务；没有则空。
     *
     * <p>注意：回调内调用 {@link #alignLesson} 时作业状态尚未随外层事务提交，
     * 这条查询看不到刚完成的版本，因此调用方必须显式传入 jobId。</p>
     */
    private java.util.Optional<com.macteval.model.entity.AnalysisJobEntity> latestSuccessfulJob(
            String lessonSessionId, com.macteval.model.entity.AnalysisJobEntity.JobType type) {
        return analysisJobRepository.findFirstByTypeAndLessonSessionIdAndStatusOrderByCompletedAtDesc(
                type, lessonSessionId, com.macteval.model.entity.AnalysisJobEntity.JobStatus.SUCCEEDED);
    }

    /** 查询课堂窗口特征并汇总语音话语指标（教师/admin 属主守卫）。 */
    @Transactional(readOnly = true)
    public com.macteval.dto.WindowFeaturesDTO getByLesson(String lessonSessionId, String authenticatedTeacherId) {
        com.macteval.model.entity.LessonSessionEntity session = lessonSessionRepository.findById(lessonSessionId)
                .orElseThrow(() -> new java.util.NoSuchElementException("课堂不存在"));
        resourceGuard.assertTeacherOwnsCourse(authenticatedTeacherId, session.getTeacherId());

        // 读“最新版本组合”的窗口：与报告同一套选取规则，避免把旧版本的分数
        // 和最新任务的证据帧混在一起展示（R04）。
        List<LessonWindowFeatureEntity> windows = WindowVersionSelector.read(
                windowRepository, lessonSessionId,
                latestSuccessfulJobId(lessonSessionId,
                        com.macteval.model.entity.AnalysisJobEntity.JobType.VIDEO_ANALYSIS),
                latestSuccessfulJobId(lessonSessionId,
                        com.macteval.model.entity.AnalysisJobEntity.JobType.AUDIO_ANALYSIS));
        // 只展示生成当前窗口特征的那次视频任务的证据帧，避免旧版本截图混入。
        String videoJobId = latestSuccessfulJobId(lessonSessionId,
                com.macteval.model.entity.AnalysisJobEntity.JobType.VIDEO_ANALYSIS);
        List<BehaviorEvidenceFrameEntity> frames = videoJobId == null
                ? evidenceFrameRepository.findByLessonSessionIdOrderByWindowIndexAsc(lessonSessionId)
                : evidenceFrameRepository.findByLessonSessionIdAndJobIdOrderByWindowIndexAsc(
                        lessonSessionId, videoJobId);
        Map<Integer, List<com.macteval.dto.EvidenceFrameDTO>> framesByWindow = new java.util.HashMap<>();
        for (BehaviorEvidenceFrameEntity frame : frames) {
            String url = null;
            try {
                url = minioService.generateDownloadUrl(frame.getBucket(), frame.getObjectKey(), 30);
            } catch (Exception e) {
                log.warn("Could not sign evidence frame {}: {}", frame.getId(), e.getMessage());
            }
            framesByWindow.computeIfAbsent(frame.getWindowIndex(), k -> new ArrayList<>())
                    .add(new com.macteval.dto.EvidenceFrameDTO(
                            frame.getId(), frame.getWindowIndex(), frame.getTimestampMs(),
                            frame.getCode(), frame.getLabel(), url, parseMap(frame.getMetadata())));
        }

        List<com.macteval.dto.WindowFeatureDTO> dtos = windows.stream().map(e -> {
            Map<String, Object> vf = parseMap(e.getVideoFeatures());
            Map<String, Object> sf = parseMap(e.getSpeechFeatures());
            return new com.macteval.dto.WindowFeatureDTO(
                    e.getId(), e.getLessonSessionId(), e.getWindowIndex(),
                    e.getWindowStart(), e.getWindowEnd(),
                    e.getVideoScore(), e.getSpeechScore(), e.getFusedScore(), vf, sf,
                    framesByWindow.getOrDefault(e.getWindowIndex(), List.of()));
        }).toList();

        double coverageSum = 0;
        int questionSum = 0;
        double speechSeconds = 0;
        int withSpeech = 0;
        for (LessonWindowFeatureEntity e : windows) {
            Map<String, Object> sf = parseMap(e.getSpeechFeatures());
            if (sf.isEmpty()) continue; // parseMap 永不返回 null
            if (!"available".equals(sf.getOrDefault("coverage_state", "available"))) continue;
            double coverage = ((Number) sf.getOrDefault("speech_coverage", 0.0)).doubleValue();
            double windowSeconds = ((Number) sf.getOrDefault(
                    "window_duration_seconds", SpeechMetricsCalculator.WINDOW_SECONDS)).doubleValue();
            coverageSum += coverage;
            questionSum += ((Number) sf.getOrDefault("question_count", 0)).intValue();
            speechSeconds += coverage * windowSeconds;
            withSpeech++;
        }
        double coverageAvg = withSpeech == 0 ? 0.0 : coverageSum / withSpeech;
        return new com.macteval.dto.WindowFeaturesDTO(lessonSessionId, dtos,
                new com.macteval.dto.SpeechMetricsDTO(lessonSessionId, windows.size(), coverageAvg, questionSum, speechSeconds));
    }

    /** 该课堂某类型最近一次成功任务 id；没有则 null。 */
    private String latestSuccessfulJobId(String lessonSessionId,
                                         com.macteval.model.entity.AnalysisJobEntity.JobType type) {
        return latestSuccessfulJob(lessonSessionId, type)
                .map(com.macteval.model.entity.AnalysisJobEntity::getId)
                .orElse(null);
    }

    private Map<String, Object> parseMap(String json) {
        if (json == null || json.isBlank()) return Map.of();
        try {
            return objectMapper.readValue(json, new TypeReference<Map<String, Object>>() {
            });
        } catch (Exception e) {
            return Map.of();
        }
    }

    /** 观测与窗口 [ws, we) 的重叠秒数；无重叠返回 0。 */
    private static double overlapSeconds(BehaviorObservationEntity o, LocalDateTime ws, LocalDateTime we) {
        LocalDateTime start = o.getWindowStart().isAfter(ws) ? o.getWindowStart() : ws;
        LocalDateTime end = o.getWindowEnd().isBefore(we) ? o.getWindowEnd() : we;
        if (!end.isAfter(start)) return 0;
        return Duration.between(start, end).toMillis() / 1000.0;
    }

    /** 视频观测跨度（从首个观测起点到最后一个观测终点，毫秒）。 */
    private long videoSpanMs(List<BehaviorObservationEntity> behaviors) {
        if (behaviors.isEmpty()) return 0;
        BehaviorObservationEntity first = behaviors.get(0);
        BehaviorObservationEntity last = behaviors.get(behaviors.size() - 1);
        if (first.getWindowStart() == null || last.getWindowEnd() == null) return 0;
        return Duration.between(first.getWindowStart(), last.getWindowEnd()).toMillis();
    }

    /** 转写分段跨度（最大 end 秒 ×1000，毫秒）。 */
    private long segmentsSpanMs(List<Map<String, Object>> segments) {
        double maxEnd = 0;
        for (Map<String, Object> seg : segments) {
            Object end = seg.get("end");
            if (end instanceof Number n) maxEnd = Math.max(maxEnd, n.doubleValue());
        }
        return (long) (maxEnd * 1000);
    }

    /**
     * 解析转写分段。null/空白/损坏的 JSON 返回 null（语音不可用），
     * 合法的空数组返回空列表（ASR 成功但整段静音，是有效 0 分观测）。
     */
    private List<Map<String, Object>> parseSegments(String json) {
        if (json == null || json.isBlank()) return null;
        try {
            return objectMapper.readValue(json, new TypeReference<List<Map<String, Object>>>() {
            });
        } catch (Exception e) {
            log.warn("Malformed transcript segments; treating speech as unavailable", e);
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

    /** 解析比例；null/空/非法返回 null（视为缺失分量，而不是 0 分）。 */
    private static Double parseProportionOrNull(String s) {
        if (s == null || s.isBlank()) return null;
        try {
            return Double.parseDouble(s);
        } catch (NumberFormatException e) {
            return null;
        }
    }
}
