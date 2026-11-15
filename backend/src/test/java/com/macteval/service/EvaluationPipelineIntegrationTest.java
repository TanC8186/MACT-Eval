package com.macteval.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.macteval.dto.ReportDetailDTO;
import com.macteval.model.entity.*;
import com.macteval.repository.*;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

/**
 * 评价管线端到端集成测试 — 验证"行为观测→窗口对齐→REAL 分数聚合→建议生成"全链路。
 * <p>
 * 串接 {@link WindowAlignmentService#alignLesson} 与 {@link ReportService#generateReport}，
 * 模拟真实场景：视频+语音数据落库 → 对齐窗口特征 → 聚合 REAL 分数 → 调用 SuggestionEngine。
 * 纯 Mockito 风格（无 Spring 上下文），与工程现有 192 个测试一致。
 */
class EvaluationPipelineIntegrationTest {

    // WindowAlignmentService 依赖
    private BehaviorObservationRepository behaviorRepository;
    private LessonTranscriptRepository transcriptRepository;
    private LessonWindowFeatureRepository windowFeatureRepository;
    private FusionService fusionService;
    private com.macteval.repository.LessonSessionRepository lessonSessionRepository;
    private com.macteval.security.ResourceGuard resourceGuard;
    private WindowAlignmentService alignmentService;

    // ReportService 依赖
    private EvaluationReportRepository reportRepository;
    private CourseRepository courseRepository;
    private ClassGroupRepository classGroupRepository;
    private AttendanceRecordRepository attendanceRepository;
    private SuggestionEngine suggestionEngine;
    private EvaluationReportVersionRepository reportVersionRepository;
    private SuggestionRepository suggestionRepository;
    private AnalysisJobRepository analysisJobRepository;
    private StudentRepository studentRepository;
    private ReportService reportService;

    private ObjectMapper objectMapper;

    @BeforeEach
    void setUp() {
        objectMapper = new ObjectMapper();

        // WindowAlignmentService 依赖
        behaviorRepository = mock(BehaviorObservationRepository.class);
        transcriptRepository = mock(LessonTranscriptRepository.class);
        windowFeatureRepository = mock(LessonWindowFeatureRepository.class);
        fusionService = new FusionService();
        lessonSessionRepository = mock(com.macteval.repository.LessonSessionRepository.class);
        resourceGuard = mock(com.macteval.security.ResourceGuard.class);
        alignmentService = new WindowAlignmentService(
                behaviorRepository, transcriptRepository, windowFeatureRepository,
                fusionService, new RuleBaselineService(), objectMapper,
                lessonSessionRepository, resourceGuard,
                mock(com.macteval.repository.AnalysisJobRepository.class),
                mock(com.macteval.repository.BehaviorEvidenceFrameRepository.class),
                mock(com.macteval.service.MinioService.class));

        // ReportService 依赖
        reportRepository = mock(EvaluationReportRepository.class);
        courseRepository = mock(CourseRepository.class);
        classGroupRepository = mock(ClassGroupRepository.class);
        attendanceRepository = mock(AttendanceRecordRepository.class);
        suggestionEngine = new SuggestionEngine(objectMapper);
        studentRepository = mock(StudentRepository.class);
        reportVersionRepository = mock(EvaluationReportVersionRepository.class);
        suggestionRepository = mock(SuggestionRepository.class);
        analysisJobRepository = mock(AnalysisJobRepository.class);
        reportService = new ReportService(
                reportRepository, reportVersionRepository, lessonSessionRepository,
                courseRepository, classGroupRepository,
                behaviorRepository, suggestionRepository, analysisJobRepository,
                suggestionEngine, studentRepository, windowFeatureRepository,
                objectMapper);
    }

    @Test
    void fullPipelineWindowAlignmentToRealScores() {
        // =========================================================
        // 阶段 1: 模拟视频+语音数据 → WindowAlignmentService.alignLesson
        // =========================================================
        LocalDateTime t0 = LocalDateTime.of(2026, 8, 19, 10, 0, 0);
        // 两个行为观测窗口（各约 59s，中间留 1s 间隙，总共 120s → 2 个 60s 窗口）
        when(behaviorRepository.findByLessonSessionIdOrderByWindowStartAsc("pipeline-ls-1"))
                .thenReturn(List.of(
                        obs(t0, t0.plusSeconds(59), "0.7"),
                        obs(t0.plusSeconds(61), t0.plusSeconds(120), "0.5")));
        // 一段连续语音转写（60s 内）
        when(transcriptRepository.findFirstByLessonSessionIdOrderByCreatedAtDescIdDesc("pipeline-ls-1"))
                .thenReturn(Optional.of(transcript(
                        "[{\"start\":0,\"end\":60,\"text\":\"今天我们学习\"}]")));

        // 执行对齐
        alignmentService.alignLesson("pipeline-ls-1");

        // 断言：只清空本版本组合的行（R04），并写入窗口特征
        verify(windowFeatureRepository).deleteByLessonSessionIdAndJobIds("pipeline-ls-1", "", "");
        verify(windowFeatureRepository, never()).deleteByLessonSessionId(anyString());
        @SuppressWarnings("unchecked")
        org.mockito.ArgumentCaptor<List<LessonWindowFeatureEntity>> alignCaptor =
                org.mockito.ArgumentCaptor.forClass(List.class);
        verify(windowFeatureRepository).saveAll(alignCaptor.capture());
        List<LessonWindowFeatureEntity> alignedWindows = alignCaptor.getValue();
        assertEquals(2, alignedWindows.size(), "120s 视频应产生 2 个 60s 窗口");

        // 窗口 0 [t0, t0+60s): 仅 obs1 [0,59) 比例 0.7 → videoScore = 70
        // 语音规则分：coverage=1, 0 问, 6 字/分钟 → 100×(0.5 + 0.2×0.04) = 50.8
        LessonWindowFeatureEntity w0 = alignedWindows.get(0);
        assertEquals(0, w0.getWindowIndex());
        assertEquals(70.0, w0.getVideoScore(), 1e-9);
        assertNotNull(w0.getSpeechScore(), "窗口 0 应有语音分");
        assertEquals(50.8, w0.getSpeechScore(), 1e-9);
        // 窗口 1 [t0+60s, t0+120s): 仅 obs2 [61,120) 比例 0.5 → videoScore = 50；
        // 转写存在但该窗口无语音，属于合法 0 分观测。
        LessonWindowFeatureEntity w1 = alignedWindows.get(1);
        assertEquals(1, w1.getWindowIndex());
        assertEquals(50.0, w1.getVideoScore(), 1e-9);
        assertEquals(0.0, w1.getSpeechScore(), 1e-9, "静音窗口应为 0 分而非缺失");
        // 融合分非空
        assertNotNull(w0.getFusedScore());
        assertNotNull(w1.getFusedScore());

        // =========================================================
        // 阶段 2: 窗口特征 → ReportService.generateReport REAL 分
        // =========================================================
        // 注入窗口特征（模拟 alignLesson 已落库的产物）
        when(windowFeatureRepository.findByLessonSessionIdOrderByWindowIndexAsc("pipeline-ls-1"))
                .thenReturn(alignedWindows);

        // 课堂会话
        LessonSessionEntity session = session("pipeline-ls-1", "course-1", "class-1", "teacher-1");
        when(lessonSessionRepository.findByIdForUpdate("pipeline-ls-1")).thenReturn(Optional.of(session));
        when(reportRepository.findByLessonSessionIdForUpdate("pipeline-ls-1")).thenReturn(Optional.empty());
        when(courseRepository.findById("course-1")).thenReturn(Optional.of(course("course-1", "课程")));
        when(classGroupRepository.findById("class-1")).thenReturn(Optional.of(group("class-1", "班级")));

        // 业务数据：出勤 30 人全勤 → 40 分，正确率 1/2=50% → 15 分，反馈 1 条积极 → 30 分
        when(studentRepository.count()).thenReturn(30L);
        when(attendanceRepository.countByLessonSessionIdAndStatus(eq("pipeline-ls-1"),
                eq(AttendanceRecordEntity.AttendanceStatus.PRESENT))).thenReturn(30L);
        when(attendanceRepository.countByLessonSessionIdAndStatus(eq("pipeline-ls-1"),
                eq(AttendanceRecordEntity.AttendanceStatus.LATE))).thenReturn(0L);
        when(attendanceRepository.countByLessonSessionIdAndStatus(eq("pipeline-ls-1"),
                eq(AttendanceRecordEntity.AttendanceStatus.ABSENT))).thenReturn(0L);

        // 执行报告生成
        ReportDetailDTO detail = reportService.generateReport("pipeline-ls-1");

        // =========================================================
        // 断言：阶段 2 结果
        // =========================================================
        // 数据来源
        assertEquals("REAL", detail.dataSource(), "管线模式应产出 REAL 数据源");

        // 三维分数：
        // engagement = 视频分均值 (70+50)/2 = 60
        assertNotNull(detail.engagementScore());
        assertEquals(60.0, detail.engagementScore(), 1e-9);
        // teaching = 语音分均值（w0=50.8, w1=0 → 均值 25.4）
        assertNotNull(detail.teachingScore());
        assertEquals(25.4, detail.teachingScore(), 1e-9);
        // climate = 窗口 fused 均值：w0=(70+50.8)/2=60.4，w1=(50+0)/2=25 → (60.4+25)/2=42.7
        // 业务数据（出勤/正确率/反馈）不再参与 climate 计算
        assertNotNull(detail.climateScore());
        assertEquals(42.7, detail.climateScore(), 1e-9);

        // 证据面板
        assertNotNull(detail.evidence());
        assertEquals(2, detail.evidence().windowCount(), "应有 2 个窗口");
        assertTrue(detail.evidence().featureCount() > 0, "应有特征计数");
        assertTrue(detail.evidence().coverage() > 0, "应有覆盖率");

        // 建议（三维齐全 → 生成建议）
        assertFalse(detail.suggestions().isEmpty(), "三维齐全应生成建议");
        assertTrue(detail.suggestions().stream().allMatch(x -> x.ruleId() != null));
    }

    @Test
    void fullPipelineVideoOnlyNoBusinessData() {
        // 仅有视频数据（无语音、无业务数据）——验证"部分数据"路径
        LocalDateTime t0 = LocalDateTime.of(2026, 8, 19, 10, 0, 0);
        when(behaviorRepository.findByLessonSessionIdOrderByWindowStartAsc("pipeline-ls-2"))
                .thenReturn(List.of(obs(t0, t0.plusSeconds(60), "0.8")));
        when(transcriptRepository.findFirstByLessonSessionIdOrderByCreatedAtDescIdDesc("pipeline-ls-2"))
                .thenReturn(Optional.empty());

        alignmentService.alignLesson("pipeline-ls-2");

        @SuppressWarnings("unchecked")
        org.mockito.ArgumentCaptor<List<LessonWindowFeatureEntity>> captor =
                org.mockito.ArgumentCaptor.forClass(List.class);
        verify(windowFeatureRepository).saveAll(captor.capture());
        List<LessonWindowFeatureEntity> windows = captor.getValue();
        assertEquals(1, windows.size());
        assertEquals(80.0, windows.get(0).getVideoScore(), 1e-9);
        assertNull(windows.get(0).getSpeechScore());

        // ReportService
        when(windowFeatureRepository.findByLessonSessionIdOrderByWindowIndexAsc("pipeline-ls-2"))
                .thenReturn(windows);
        LessonSessionEntity session = session("pipeline-ls-2", "course-1", "class-1", "teacher-1");
        when(lessonSessionRepository.findByIdForUpdate("pipeline-ls-2")).thenReturn(Optional.of(session));
        when(reportRepository.findByLessonSessionIdForUpdate("pipeline-ls-2")).thenReturn(Optional.empty());
        when(courseRepository.findById("course-1")).thenReturn(Optional.of(course("course-1", "课程")));
        when(classGroupRepository.findById("class-1")).thenReturn(Optional.of(group("class-1", "班级")));

        // 无业务数据
        when(studentRepository.count()).thenReturn(0L);
        when(attendanceRepository.findByLessonSessionId("pipeline-ls-2")).thenReturn(List.of());

        ReportDetailDTO detail = reportService.generateReport("pipeline-ls-2");

        // 验证：视频分存在，语音缺失但氛围用融合分兜底
        assertEquals("REAL", detail.dataSource());
        assertEquals(80.0, detail.engagementScore(), 1e-9);
        assertNull(detail.teachingScore(), "无语音分 → teaching 应为 null");
        // climate 由窗口融合分驱动（video=80, speech 中性50 → fused=65）
        assertNotNull(detail.climateScore(), "climate 应由窗口融合分驱动");
        assertEquals(65.0, detail.climateScore(), 1e-9);
        // 只有视频证据：生成视频参与度建议，不生成语音/融合结论
        assertTrue(detail.suggestions().stream()
                .anyMatch(x -> "学生参与度".equals(x.category())));
        assertTrue(detail.suggestions().stream()
                .noneMatch(x -> "教学质量".equals(x.category())));
    }

    @Test
    void fullPipelineNoWindowFeaturesReturnsEmpty() {
        // 无任何窗口特征 → EMPTY 数据源
        LessonSessionEntity session = session("pipeline-ls-3", "course-1", "class-1", "teacher-1");
        when(lessonSessionRepository.findByIdForUpdate("pipeline-ls-3")).thenReturn(Optional.of(session));
        when(reportRepository.findByLessonSessionIdForUpdate("pipeline-ls-3")).thenReturn(Optional.empty());
        when(courseRepository.findById("course-1")).thenReturn(Optional.of(course("course-1", "课程")));
        when(classGroupRepository.findById("class-1")).thenReturn(Optional.of(group("class-1", "班级")));
        when(windowFeatureRepository.findByLessonSessionIdOrderByWindowIndexAsc("pipeline-ls-3"))
                .thenReturn(List.of());

        // 仅有业务数据（出勤率）但无窗口特征
        when(studentRepository.count()).thenReturn(1L);
        when(attendanceRepository.countByLessonSessionIdAndStatus(anyString(), any())).thenReturn(0L);

        // 但行为观测存在（触发 evidence）
        when(behaviorRepository.findByLessonSessionIdOrderByWindowStartAsc("pipeline-ls-3")).thenReturn(
                List.of(obs(LocalDateTime.now(), LocalDateTime.now().plusSeconds(60), "0.5")));
        when(attendanceRepository.findByLessonSessionId("pipeline-ls-3")).thenReturn(List.of());

        ReportDetailDTO detail = reportService.generateReport("pipeline-ls-3");

        // 无窗口特征 → 三维分数 null
        assertNull(detail.engagementScore());
        assertNull(detail.teachingScore());
        assertNull(detail.climateScore());
        // 有行为观测 → dataSource 为 "REAL"（有 evidence 但无窗口分数）
        assertEquals("REAL", detail.dataSource());
        // 无可用规则分：只给出证据不足提示
        assertEquals(1, detail.suggestions().size());
        assertEquals("数据覆盖", detail.suggestions().getFirst().category());
    }

    // ---- Helper factory methods ----

    private BehaviorObservationEntity obs(LocalDateTime start, LocalDateTime end, String upright) {
        BehaviorObservationEntity e = new BehaviorObservationEntity();
        e.setLessonSessionId("pipeline-ls-1");
        e.setWindowStart(start);
        e.setWindowEnd(end);
        e.setAggregatedUprightProportion(upright);
        return e;
    }

    private LessonTranscriptEntity transcript(String segmentsJson) {
        LessonTranscriptEntity t = new LessonTranscriptEntity();
        t.setLessonSessionId("pipeline-ls-1");
        t.setSegments(segmentsJson);
        return t;
    }

    private LessonSessionEntity session(String id, String courseId, String classId, String teacherId) {
        LessonSessionEntity s = new LessonSessionEntity();
        s.setId(id);
        s.setCourseId(courseId);
        s.setClassId(classId);
        s.setTeacherId(teacherId);
        s.setTitle("管线集成测试课堂");
        s.setDate(LocalDateTime.now());
        s.setStartTime(LocalDateTime.now());
        s.setStatus(LessonSessionEntity.SessionStatus.COMPLETED);
        return s;
    }

    private com.macteval.model.entity.CourseEntity course(String id, String name) {
        com.macteval.model.entity.CourseEntity c = new com.macteval.model.entity.CourseEntity();
        c.setId(id);
        c.setName(name);
        return c;
    }

    private com.macteval.model.entity.ClassGroupEntity group(String id, String name) {
        com.macteval.model.entity.ClassGroupEntity g = new com.macteval.model.entity.ClassGroupEntity();
        g.setId(id);
        g.setName(name);
        return g;
    }
}