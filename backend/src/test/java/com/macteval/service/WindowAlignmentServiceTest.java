package com.macteval.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.macteval.model.entity.BehaviorObservationEntity;
import com.macteval.model.entity.LessonTranscriptEntity;
import com.macteval.model.entity.LessonWindowFeatureEntity;
import com.macteval.repository.BehaviorObservationRepository;
import com.macteval.repository.LessonTranscriptRepository;
import com.macteval.repository.LessonWindowFeatureRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.*;

class WindowAlignmentServiceTest {

    private BehaviorObservationRepository behaviorRepository;
    private LessonTranscriptRepository transcriptRepository;
    private LessonWindowFeatureRepository windowRepository;
    private FusionService fusionService;
    private RuleBaselineService ruleBaselineService;
    private com.macteval.repository.LessonSessionRepository lessonSessionRepository;
    private com.macteval.security.ResourceGuard resourceGuard;
    private com.macteval.repository.AnalysisJobRepository analysisJobRepository;
    private WindowAlignmentService service;

    @BeforeEach
    void setUp() {
        behaviorRepository = mock(BehaviorObservationRepository.class);
        transcriptRepository = mock(LessonTranscriptRepository.class);
        windowRepository = mock(LessonWindowFeatureRepository.class);
        fusionService = new FusionService();
        ruleBaselineService = new RuleBaselineService();
        lessonSessionRepository = mock(com.macteval.repository.LessonSessionRepository.class);
        resourceGuard = mock(com.macteval.security.ResourceGuard.class);
        analysisJobRepository = mock(com.macteval.repository.AnalysisJobRepository.class);
        service = new WindowAlignmentService(behaviorRepository, transcriptRepository, windowRepository,
                fusionService, ruleBaselineService, new ObjectMapper(), lessonSessionRepository,
                resourceGuard, analysisJobRepository,
                mock(com.macteval.repository.BehaviorEvidenceFrameRepository.class),
                mock(com.macteval.service.MinioService.class));
    }

    @Test
    void alignLessonTreatsMalformedSegmentsAsMissingSpeech() {
        LocalDateTime t0 = LocalDateTime.of(2026, 8, 9, 10, 0, 0);
        when(behaviorRepository.findByLessonSessionIdOrderByWindowStartAsc("ls-1")).thenReturn(List.of(
                obs(t0, t0.plusSeconds(60), "0.8")));
        when(transcriptRepository.findFirstByLessonSessionIdOrderByCreatedAtDescIdDesc("ls-1"))
                .thenReturn(Optional.of(transcript("not-json")));

        service.alignLesson("ls-1");

        @SuppressWarnings("unchecked")
        org.mockito.ArgumentCaptor<List<LessonWindowFeatureEntity>> captor =
                org.mockito.ArgumentCaptor.forClass(List.class);
        verify(windowRepository).saveAll(captor.capture());
        LessonWindowFeatureEntity w = captor.getValue().get(0);
        assertNull(w.getSpeechScore(), "损坏的 segments 不得伪造成静音 0 分");
        assertEquals(80.0, w.getVideoScore(), 1e-9);
    }

    @Test
    void alignLessonScoresSilentTranscriptAsZero() {
        when(behaviorRepository.findByLessonSessionIdOrderByWindowStartAsc("ls-1")).thenReturn(List.of());
        when(transcriptRepository.findFirstByLessonSessionIdOrderByCreatedAtDescIdDesc("ls-1"))
                .thenReturn(Optional.of(transcript("[]")));

        service.alignLesson("ls-1");

        @SuppressWarnings("unchecked")
        org.mockito.ArgumentCaptor<List<LessonWindowFeatureEntity>> captor =
                org.mockito.ArgumentCaptor.forClass(List.class);
        verify(windowRepository).saveAll(captor.capture());
        LessonWindowFeatureEntity w = captor.getValue().get(0);
        assertEquals(0.0, w.getSpeechScore(), 1e-9, "合法空 segments 是静音 0 分");
        // 视频缺失中性 50，语音 0 → 等权融合 25
        assertEquals(25.0, w.getFusedScore(), 1e-9);
    }

    private BehaviorObservationEntity obs(LocalDateTime start, LocalDateTime end, String upright) {
        BehaviorObservationEntity e = new BehaviorObservationEntity();
        e.setLessonSessionId("ls-1");
        e.setWindowStart(start);
        e.setWindowEnd(end);
        e.setAggregatedUprightProportion(upright);
        return e;
    }

    private LessonTranscriptEntity transcript(String segmentsJson) {
        LessonTranscriptEntity t = new LessonTranscriptEntity();
        t.setLessonSessionId("ls-1");
        t.setSegments(segmentsJson);
        return t;
    }

    @Test
    void alignLessonCreatesWindowsForBothModalities() {
        LocalDateTime t0 = LocalDateTime.of(2026, 8, 9, 10, 0, 0);
        when(behaviorRepository.findByLessonSessionIdOrderByWindowStartAsc("ls-1")).thenReturn(List.of(
                obs(t0, t0.plusSeconds(30), "0.5"),
                obs(t0.plusSeconds(30), t0.plusSeconds(120), "0.8")));
        when(transcriptRepository.findFirstByLessonSessionIdOrderByCreatedAtDescIdDesc("ls-1"))
                .thenReturn(Optional.of(transcript("[{\"start\":0,\"end\":60,\"text\":\"今天我们学习\"}]")));

        service.alignLesson("ls-1");

        // R04：对齐只清空自己版本组合的行，不再整课堂删除。
        verify(windowRepository).deleteByLessonSessionIdAndJobIds("ls-1", "", "");
        verify(windowRepository, never()).deleteByLessonSessionId(anyString());
        @SuppressWarnings("unchecked")
        org.mockito.ArgumentCaptor<List<LessonWindowFeatureEntity>> captor =
                org.mockito.ArgumentCaptor.forClass(List.class);
        verify(windowRepository).saveAll(captor.capture());
        List<LessonWindowFeatureEntity> windows = captor.getValue();
        // 120 秒 → 2 个 60s 窗口
        assertEquals(2, windows.size());
        // 窗口0 [0,60): 观测1[0,30)比例0.5 与观测2[30,120)比例0.8 均与窗口相交 → 规则基线 posture=0.65 → 65
        assertEquals(65.0, windows.get(0).getVideoScore(), 1e-9);
        // 窗口0 语音: [0,60) coverage=1, 0 问, 语速 6 字/分钟
        // 100 × (0.5×1 + 0.3×0 + 0.2×0.04) = 50.8
        assertEquals(50.8, windows.get(0).getSpeechScore(), 1e-9);
        // 窗口1 [60,120): 仅观测2 相交 → video=80；转写存在但该窗口无语音，
        // 属于合法的 0 分观测（不是缺失）。
        assertEquals(80.0, windows.get(1).getVideoScore(), 1e-9);
        assertEquals(0.0, windows.get(1).getSpeechScore(), 1e-9);
        // 等权融合：窗口0 (65+50.8)/2=57.9；窗口1 (80+0)/2=40
        assertEquals(57.9, windows.get(0).getFusedScore(), 1e-9);
        assertEquals(40.0, windows.get(1).getFusedScore(), 1e-9);
    }

    @Test
    void adjacentWindowBoundaryIsNotDoubleCounted() {
        LocalDateTime t0 = LocalDateTime.of(2026, 8, 9, 10, 0, 0);
        // 相邻窗口 [0,60)、[60,120)，姿态比例分别为 1 和 0。
        // 闭区间相交会把 t0+60 的贴边观测同时算进两个窗口，得到 [50,50]；
        // 半开区间协议下必须是 [100, 0]。
        when(behaviorRepository.findByLessonSessionIdOrderByWindowStartAsc("ls-boundary")).thenReturn(List.of(
                obs(t0, t0.plusSeconds(60), "1.0"),
                obs(t0.plusSeconds(60), t0.plusSeconds(120), "0.0")));
        when(transcriptRepository.findFirstByLessonSessionIdOrderByCreatedAtDescIdDesc("ls-boundary"))
                .thenReturn(Optional.empty());

        service.alignLesson("ls-boundary");

        @SuppressWarnings("unchecked")
        org.mockito.ArgumentCaptor<List<LessonWindowFeatureEntity>> captor =
                org.mockito.ArgumentCaptor.forClass(List.class);
        verify(windowRepository).saveAll(captor.capture());
        List<LessonWindowFeatureEntity> windows = captor.getValue();
        assertEquals(2, windows.size());
        assertEquals(100.0, windows.get(0).getVideoScore(), 1e-9,
                "窗口 0 只能计入 [0,60) 的观测");
        assertEquals(0.0, windows.get(1).getVideoScore(), 1e-9,
                "窗口 1 只能计入 [60,120) 的观测");
    }

    @Test
    void crossWindowObservationIsWeightedByOverlap() {
        LocalDateTime t0 = LocalDateTime.of(2026, 8, 9, 10, 0, 0);
        // 单条 120s 观测跨两个窗口；每个窗口应获得其重叠部分的姿态比例，
        // 而不是把整条观测按全权重重复计入。
        when(behaviorRepository.findByLessonSessionIdOrderByWindowStartAsc("ls-cross")).thenReturn(List.of(
                obs(t0, t0.plusSeconds(120), "0.9")));
        when(transcriptRepository.findFirstByLessonSessionIdOrderByCreatedAtDescIdDesc("ls-cross"))
                .thenReturn(Optional.empty());

        service.alignLesson("ls-cross");

        @SuppressWarnings("unchecked")
        org.mockito.ArgumentCaptor<List<LessonWindowFeatureEntity>> captor =
                org.mockito.ArgumentCaptor.forClass(List.class);
        verify(windowRepository).saveAll(captor.capture());
        // 两个窗口都与该观测重叠 60s，权重相同 → 姿态比例保持 0.9。
        assertEquals(90.0, captor.getValue().get(0).getVideoScore(), 1e-9);
        assertEquals(90.0, captor.getValue().get(1).getVideoScore(), 1e-9);
    }

    @Test
    void audioEndingBeforeVideoMarksLaterWindowsAsNotCovered() {
        LocalDateTime t0 = LocalDateTime.of(2026, 8, 9, 10, 0, 0);
        when(behaviorRepository.findByLessonSessionIdOrderByWindowStartAsc("ls-short-audio")).thenReturn(List.of(
                obs(t0, t0.plusSeconds(120), "0.8")));
        LessonTranscriptEntity t = transcript("[{\"start\":0,\"end\":30,\"text\":\"只有前三十秒\"}]");
        t.setMediaDurationMs(30_000L);
        when(transcriptRepository.findFirstByLessonSessionIdOrderByCreatedAtDescIdDesc("ls-short-audio"))
                .thenReturn(Optional.of(t));

        service.alignLesson("ls-short-audio");

        @SuppressWarnings("unchecked")
        org.mockito.ArgumentCaptor<List<LessonWindowFeatureEntity>> captor =
                org.mockito.ArgumentCaptor.forClass(List.class);
        verify(windowRepository).saveAll(captor.capture());
        List<LessonWindowFeatureEntity> windows = captor.getValue();
        assertEquals(2, windows.size());
        assertNotNull(windows.get(0).getSpeechScore(), "窗口 0 在音频覆盖范围内");
        assertNull(windows.get(1).getSpeechScore(), "音频结束后的窗口不能当作静音 0 分");
        assertTrue(windows.get(1).getSpeechFeatures().contains("\"coverage_state\":\"not_covered\""));
    }

    @Test
    void tailWindowUsesActualRemainingDuration() {
        LocalDateTime t0 = LocalDateTime.of(2026, 8, 9, 10, 0, 0);
        when(behaviorRepository.findByLessonSessionIdOrderByWindowStartAsc("ls-tail")).thenReturn(List.of(
                obs(t0, t0.plusSeconds(90), "0.8")));
        LessonTranscriptEntity t = transcript("[{\"start\":0,\"end\":90,\"text\":\"完整九十分钟\"}]");
        t.setMediaDurationMs(90_000L);
        when(transcriptRepository.findFirstByLessonSessionIdOrderByCreatedAtDescIdDesc("ls-tail"))
                .thenReturn(Optional.of(t));

        service.alignLesson("ls-tail");

        @SuppressWarnings("unchecked")
        org.mockito.ArgumentCaptor<List<LessonWindowFeatureEntity>> captor =
                org.mockito.ArgumentCaptor.forClass(List.class);
        verify(windowRepository).saveAll(captor.capture());
        List<LessonWindowFeatureEntity> windows = captor.getValue();
        assertEquals(2, windows.size());
        assertEquals(t0.plusSeconds(90), windows.get(1).getWindowEnd(),
                "尾窗结束时间应为媒体真实结束时间，而不是强行补满 60s");
        assertTrue(windows.get(1).getSpeechFeatures().contains("\"window_duration_seconds\":30.0"));
    }

    @Test
    void missingEarlyWindowsKeepMediaIndexInsteadOfRenumberingToZero() {
        LocalDateTime t0 = LocalDateTime.of(2026, 8, 9, 10, 0, 0);
        BehaviorObservationEntity o = obs(t0.plusSeconds(120), t0.plusSeconds(180), "0.8");
        o.setMediaAnchor(t0);
        o.setWindowIndex(2);
        o.setWindowStartOffsetMs(120_000L);
        o.setWindowEndOffsetMs(180_000L);
        when(behaviorRepository.findByLessonSessionIdOrderByWindowStartAsc("ls-partial")).thenReturn(List.of(o));
        LessonTranscriptEntity t = transcript("[{\"start\":0,\"end\":180,\"text\":\"完整音频\"}]");
        t.setMediaDurationMs(180_000L);
        when(transcriptRepository.findFirstByLessonSessionIdOrderByCreatedAtDescIdDesc("ls-partial"))
                .thenReturn(Optional.of(t));

        service.alignLesson("ls-partial");

        @SuppressWarnings("unchecked")
        org.mockito.ArgumentCaptor<List<LessonWindowFeatureEntity>> captor =
                org.mockito.ArgumentCaptor.forClass(List.class);
        verify(windowRepository).saveAll(captor.capture());
        List<LessonWindowFeatureEntity> windows = captor.getValue();
        assertEquals(3, windows.size(), "媒体 180s 应有 3 个窗口，即使前两个窗口没有视频观测");
        assertNull(windows.get(0).getVideoScore());
        assertNull(windows.get(1).getVideoScore());
        assertEquals(80.0, windows.get(2).getVideoScore(), 1e-9);
        assertEquals(t0.plusSeconds(120), windows.get(2).getWindowStart());
    }

    @Test
    void alignLessonSkipsWhenNoData() {
        when(behaviorRepository.findByLessonSessionIdOrderByWindowStartAsc("ls-1")).thenReturn(List.of());
        when(transcriptRepository.findFirstByLessonSessionIdOrderByCreatedAtDescIdDesc("ls-1")).thenReturn(Optional.empty());

        service.alignLesson("ls-1");

        verify(windowRepository, never()).saveAll(any());
        verify(windowRepository, never()).deleteByLessonSessionId(anyString());
        verify(windowRepository, never())
                .deleteByLessonSessionIdAndJobIds(anyString(), anyString(), anyString());
    }

    @Test
    void getByLessonAggregatesSpeechMetrics() {
        LessonWindowFeatureEntity w = new LessonWindowFeatureEntity();
        w.setId("w-1");
        w.setLessonSessionId("ls-1");
        w.setWindowIndex(0);
        w.setVideoScore(50.0);
        w.setSpeechScore(70.0);
        w.setFusedScore(60.0);
        w.setVideoFeatures("{\"upright_proportion\":0.5,\"observation_count\":2}");
        w.setSpeechFeatures("{\"speech_coverage\":1.0,\"question_count\":1,\"speech_rate\":180.0}");
        when(lessonSessionRepository.findById("ls-1"))
                .thenReturn(Optional.of(new com.macteval.model.entity.LessonSessionEntity()));
        when(windowRepository.findByLessonSessionIdOrderByWindowIndexAsc("ls-1")).thenReturn(List.of(w));

        var dto = service.getByLesson("ls-1", "teacher-1");

        verify(resourceGuard).assertTeacherOwnsCourse("teacher-1", null);
        assertEquals(1, dto.windows().size());
        assertEquals(60.0, dto.windows().get(0).fusedScore(), 1e-9);
        assertEquals(1.0, dto.speechMetrics().speechCoverageAvg(), 1e-9);
        assertEquals(1, dto.speechMetrics().totalQuestionCount());
        assertEquals(60.0, dto.speechMetrics().totalSpeechSeconds(), 1e-9);
    }

    @Test
    void alignLessonWithVideoOnly() {
        LocalDateTime t0 = LocalDateTime.of(2026, 8, 9, 10, 0, 0);
        when(behaviorRepository.findByLessonSessionIdOrderByWindowStartAsc("ls-1")).thenReturn(List.of(
                obs(t0, t0.plusSeconds(30), "0.5")));
        when(transcriptRepository.findFirstByLessonSessionIdOrderByCreatedAtDescIdDesc("ls-1")).thenReturn(Optional.empty());

        service.alignLesson("ls-1");

        @SuppressWarnings("unchecked")
        org.mockito.ArgumentCaptor<List<LessonWindowFeatureEntity>> captor =
                org.mockito.ArgumentCaptor.forClass(List.class);
        verify(windowRepository).saveAll(captor.capture());
        List<LessonWindowFeatureEntity> windows = captor.getValue();
        assertEquals(1, windows.size());
        assertEquals(50.0, windows.get(0).getVideoScore(), 1e-9);
        assertNull(windows.get(0).getSpeechScore());
        // 缺失语音中性填充 50 → (50+50)/2 = 50
        assertEquals(50.0, windows.get(0).getFusedScore(), 1e-9);
    }

    @Test
    void alignLessonUsesPositiveBehaviorRatioWhenAvailable() {
        LocalDateTime t0 = LocalDateTime.of(2026, 8, 9, 10, 0, 0);
        BehaviorObservationEntity o = obs(t0, t0.plusSeconds(60), "0.8");
        o.setHandRaisedCount(2);
        o.setVisibleSeatCount(10);
        when(behaviorRepository.findByLessonSessionIdOrderByWindowStartAsc("ls-1")).thenReturn(List.of(o));
        when(transcriptRepository.findFirstByLessonSessionIdOrderByCreatedAtDescIdDesc("ls-1")).thenReturn(Optional.empty());

        service.alignLesson("ls-1");

        @SuppressWarnings("unchecked")
        org.mockito.ArgumentCaptor<List<LessonWindowFeatureEntity>> captor =
                org.mockito.ArgumentCaptor.forClass(List.class);
        verify(windowRepository).saveAll(captor.capture());
        LessonWindowFeatureEntity w = captor.getValue().get(0);
        // posture 0.8 (w=0.5) + positive_behavior_ratio 2/10=0.2 (w=0.3)；stability 缺失重归一化
        // 100 × (0.5×0.8 + 0.3×0.2) / 0.8 = 57.5
        assertEquals(57.5, w.getVideoScore(), 1e-9);
        assertTrue(w.getVideoFeatures().contains("\"upright_proportion\":0.8"));
        assertTrue(w.getVideoFeatures().contains("\"rule_protocol\":\"v1-system-proxy\""));
    }

    @Test
    void alignLessonTreatsAllZeroHandRaisePlaceholderAsMissing() {
        LocalDateTime t0 = LocalDateTime.of(2026, 8, 9, 10, 0, 0);
        BehaviorObservationEntity o = obs(t0, t0.plusSeconds(60), "0.8");
        // 当前检测器尚未实现举手识别，0 是占位值；不得当作真实零值。
        o.setHandRaisedCount(0);
        o.setVisibleSeatCount(10);
        when(behaviorRepository.findByLessonSessionIdOrderByWindowStartAsc("ls-1")).thenReturn(List.of(o));
        when(transcriptRepository.findFirstByLessonSessionIdOrderByCreatedAtDescIdDesc("ls-1")).thenReturn(Optional.empty());

        service.alignLesson("ls-1");

        @SuppressWarnings("unchecked")
        org.mockito.ArgumentCaptor<List<LessonWindowFeatureEntity>> captor =
                org.mockito.ArgumentCaptor.forClass(List.class);
        verify(windowRepository).saveAll(captor.capture());
        LessonWindowFeatureEntity w = captor.getValue().get(0);
        // posture 0.8 单独可用 → 80；positive_behavior_ratio 按缺失重归一化。
        assertEquals(80.0, w.getVideoScore(), 1e-9);
        assertFalse(w.getVideoFeatures().contains("positive_behavior_ratio"));
    }

    @Test
    void alignLessonTreatsUnavailableObservationAsMissingVideo() {
        LocalDateTime t0 = LocalDateTime.of(2026, 8, 9, 10, 0, 0);
        BehaviorObservationEntity o = obs(t0, t0.plusSeconds(60), "0.9");
        o.setDataQuality("unavailable");
        when(behaviorRepository.findByLessonSessionIdOrderByWindowStartAsc("ls-1")).thenReturn(List.of(o));
        when(transcriptRepository.findFirstByLessonSessionIdOrderByCreatedAtDescIdDesc("ls-1"))
                .thenReturn(Optional.of(transcript("[{\"start\":0,\"end\":60,\"text\":\"今天我们学习\"}]")));

        service.alignLesson("ls-1");

        @SuppressWarnings("unchecked")
        org.mockito.ArgumentCaptor<List<LessonWindowFeatureEntity>> captor =
                org.mockito.ArgumentCaptor.forClass(List.class);
        verify(windowRepository).saveAll(captor.capture());
        LessonWindowFeatureEntity w = captor.getValue().get(0);
        assertNull(w.getVideoScore());
        assertNotNull(w.getSpeechScore());
        // 视频缺失中性 50，语音 50.8 → 50.4
        assertEquals(50.4, w.getFusedScore(), 1e-9);
    }
}
