package com.macteval.service;

import com.macteval.model.entity.*;
import com.macteval.repository.*;
import com.macteval.dto.*;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

class ReportServiceRealScoreTest {

    private LessonSessionRepository lessonSessionRepository;
    private CourseRepository courseRepository;
    private ClassGroupRepository classGroupRepository;
    private AttendanceRecordRepository attendanceRepository;
    private BehaviorObservationRepository behaviorRepository;
    private LessonWindowFeatureRepository windowFeatureRepository;
    private EvaluationReportRepository reportRepository;
    private EvaluationReportVersionRepository reportVersionRepository;
    private SuggestionRepository suggestionRepository;
    private AnalysisJobRepository analysisJobRepository;
    private SuggestionEngine suggestionEngine;
    private StudentRepository studentRepository;
    private ReportService service;

    @BeforeEach
    @SuppressWarnings("unchecked")
    void setUp() {
        lessonSessionRepository = mock(LessonSessionRepository.class);
        courseRepository = mock(CourseRepository.class);
        classGroupRepository = mock(ClassGroupRepository.class);
        attendanceRepository = mock(AttendanceRecordRepository.class);
        behaviorRepository = mock(BehaviorObservationRepository.class);
        windowFeatureRepository = mock(LessonWindowFeatureRepository.class);
        reportRepository = mock(EvaluationReportRepository.class);
        reportVersionRepository = mock(EvaluationReportVersionRepository.class);
        suggestionRepository = mock(SuggestionRepository.class);
        analysisJobRepository = mock(AnalysisJobRepository.class);
        suggestionEngine = new SuggestionEngine(new ObjectMapper());
        studentRepository = mock(StudentRepository.class);
        service = new ReportService(
                reportRepository, reportVersionRepository, lessonSessionRepository,
                courseRepository, classGroupRepository,
                behaviorRepository, suggestionRepository, analysisJobRepository,
                suggestionEngine, studentRepository, windowFeatureRepository,
                new ObjectMapper());
    }

    @Test
    void realModeGeneratesScoresFromWindowFeatures() {
        // 模拟有窗口特征的课堂
        LessonSessionEntity session = session("ls-1", "course-1", "class-1", "teacher-1");
        when(lessonSessionRepository.findByIdForUpdate("ls-1")).thenReturn(Optional.of(session));
        when(reportRepository.findByLessonSessionIdForUpdate("ls-1")).thenReturn(Optional.empty());
        when(courseRepository.findById("course-1")).thenReturn(Optional.of(course("course-1", "课程")));
        when(classGroupRepository.findById("class-1")).thenReturn(Optional.of(group("class-1", "班级")));

        // 3 个 60s 窗口：video_score 80, 70, 60；speech_score 70, 65, 55；fused 75, 67.5, 57.5
        when(windowFeatureRepository.findByLessonSessionIdOrderByWindowIndexAsc("ls-1"))
                .thenReturn(List.of(
                        window("w-1", "ls-1", 0, 80.0, 70.0, 75.0),
                        window("w-2", "ls-1", 1, 70.0, 65.0, 67.5),
                        window("w-3", "ls-1", 2, 60.0, 55.0, 57.5)));

        // 业务数据存在，但课堂氛围分只由窗口 fused 分驱动
        when(studentRepository.count()).thenReturn(30L);
        when(attendanceRepository.countByLessonSessionIdAndStatus(eq("ls-1"),
                eq(AttendanceRecordEntity.AttendanceStatus.PRESENT))).thenReturn(24L);
        when(attendanceRepository.countByLessonSessionIdAndStatus(eq("ls-1"),
                eq(AttendanceRecordEntity.AttendanceStatus.LATE))).thenReturn(2L);
        when(attendanceRepository.countByLessonSessionIdAndStatus(eq("ls-1"),
                eq(AttendanceRecordEntity.AttendanceStatus.ABSENT))).thenReturn(1L);

        var detail = service.generateReport("ls-1");

        // 验证分数非 null
        assertNotNull(detail.engagementScore(), "REAL 模式应有参与度分数");
        assertNotNull(detail.teachingScore(), "REAL 模式应有教学质量分数");
        assertNotNull(detail.climateScore(), "REAL 模式应有课堂氛围分数");
        // 参与度 = 视频分均值 (80+70+60)/3 = 70
        assertEquals(70.0, detail.engagementScore(), 1e-9);
        // 教学质量 = 语音分均值 (70+65+55)/3 = 63.333...
        assertEquals(190.0 / 3.0, detail.teachingScore(), 1e-9);
        // 课堂氛围 = 窗口 fused 分均值 (75+67.5+57.5)/3 = 66.666...
        assertEquals(200.0 / 3.0, detail.climateScore(), 1e-9);
        // 验证数据来源为 REAL
        assertEquals("REAL", detail.dataSource());
        // E3–E6 完成前，REAL 课堂氛围只能是“待验证”，不能写成已验证
        assertEquals("pending_validation", detail.scoreValidation().get("climate"));
        assertEquals("engineering_mapping_not_validated", detail.scoreValidation().get("engagement"));
        assertEquals("engineering_mapping_not_validated", detail.scoreValidation().get("teaching"));
        // 验证证据面板有真实窗口数
        assertNotNull(detail.evidence());
        assertEquals(3, detail.evidence().windowCount());
        // 验证建议非空（REAL 模式生成了建议）
        assertFalse(detail.suggestions().isEmpty(), "REAL 模式应生成建议");
        // 每条建议都能追溯到规则与观测字段
        assertTrue(detail.suggestions().stream().allMatch(x -> x.ruleId() != null));
    }

    @Test
    void behaviorSummaryUsesOnlyTheReportsSelectedVideoJob() {
        when(lessonSessionRepository.findByIdForUpdate("ls-jobs"))
                .thenReturn(Optional.of(session("ls-jobs", "course-1", "class-1", "teacher-1")));
        var job = new AnalysisJobEntity();
        job.setId("new-video-job");
        when(analysisJobRepository.findFirstByTypeAndLessonSessionIdAndStatusOrderByCompletedAtDesc(
                AnalysisJobEntity.JobType.VIDEO_ANALYSIS, "ls-jobs", AnalysisJobEntity.JobStatus.SUCCEEDED))
                .thenReturn(Optional.of(job));
        var oldObservation = new BehaviorObservationEntity();
        oldObservation.setAggregatedUprightProportion("0.10");
        var newObservation = new BehaviorObservationEntity();
        newObservation.setAggregatedUprightProportion("0.90");
        when(behaviorRepository.findByLessonSessionIdOrderByWindowStartAsc("ls-jobs"))
                .thenReturn(List.of(oldObservation, newObservation));
        when(behaviorRepository.findByLessonSessionIdAndJobIdOrderByWindowIndexAsc("ls-jobs", "new-video-job"))
                .thenReturn(List.of(newObservation));

        var detail = service.generateReport("ls-jobs");

        assertEquals("new-video-job", detail.analysisVideoJobId());
        assertTrue(detail.behaviorSummary().contains("90%"));
        assertFalse(detail.behaviorSummary().contains("50%"));
        assertFalse(detail.behaviorSummary().contains("占位"));
        assertTrue(detail.behaviorSummary().contains("举手次数暂未统计"));
    }

    @Test
    void businessDataAloneDoesNotProduceClimateScore() {
        // 无窗口特征、仅有业务数据：三维分数仍为 null，业务数据只进入证据面板
        LessonSessionEntity session = session("ls-2", "course-1", "class-1", "teacher-1");
        when(lessonSessionRepository.findByIdForUpdate("ls-2")).thenReturn(Optional.of(session));
        when(reportRepository.findByLessonSessionIdForUpdate("ls-2")).thenReturn(Optional.empty());
        when(courseRepository.findById("course-1")).thenReturn(Optional.of(course("course-1", "课程")));
        when(classGroupRepository.findById("class-1")).thenReturn(Optional.of(group("class-1", "班级")));
        when(windowFeatureRepository.findByLessonSessionIdOrderByWindowIndexAsc("ls-2")).thenReturn(List.of());

        // 业务数据齐全（旧实现会据此算出 climate）
        when(studentRepository.count()).thenReturn(30L);
        when(attendanceRepository.countByLessonSessionIdAndStatus(eq("ls-2"),
                eq(AttendanceRecordEntity.AttendanceStatus.PRESENT))).thenReturn(30L);
        when(attendanceRepository.findByLessonSessionId("ls-2"))
                .thenReturn(List.of(new AttendanceRecordEntity()));

        var detail = service.generateReport("ls-2");

        // 已取消的考勤数据不再计入证据覆盖或数据来源。
        assertNull(detail.engagementScore());
        assertNull(detail.teachingScore());
        assertNull(detail.climateScore(), "业务数据不得驱动课堂氛围分");
        assertEquals("EMPTY", detail.dataSource());
        assertNull(detail.attendanceSummary());
        assertFalse(detail.dataCoverage().contains("attendance"));
        assertEquals(0.0, detail.evidence().coverage());
        verifyNoInteractions(attendanceRepository);
        // 无可用规则分时只给出“证据不足”提示，不伪造教学结论
        assertEquals(1, detail.suggestions().size());
        assertEquals("数据覆盖", detail.suggestions().getFirst().category());
    }

    @Test
    void realModeEvidencePanelShowsRealWindowCount() {
        LessonSessionEntity session = session("ls-3", "course-1", "class-1", "teacher-1");
        when(lessonSessionRepository.findByIdForUpdate("ls-3")).thenReturn(Optional.of(session));
        when(reportRepository.findByLessonSessionIdForUpdate("ls-3")).thenReturn(Optional.empty());
        when(courseRepository.findById("course-1")).thenReturn(Optional.of(course("course-1", "课程")));
        when(classGroupRepository.findById("class-1")).thenReturn(Optional.of(group("class-1", "班级")));
        when(windowFeatureRepository.findByLessonSessionIdOrderByWindowIndexAsc("ls-3"))
                .thenReturn(List.of(
                        window("w-1", "ls-3", 0, 80.0, 70.0, 75.0),
                        window("w-2", "ls-3", 1, 70.0, 65.0, 67.5)));

        when(studentRepository.count()).thenReturn(1L);
        when(attendanceRepository.countByLessonSessionIdAndStatus(anyString(), any())).thenReturn(0L);

        var detail = service.generateReport("ls-3");

        assertEquals("REAL", detail.dataSource());
        assertEquals(2, detail.evidence().windowCount());
        assertTrue(detail.evidence().coverage() > 0);
        assertTrue(detail.evidence().featureCount() > 0);
    }

    @Test
    void realModeVideoOnlyProducesEngagementButNullClimate() {
        // 只有视频模态（无语音，fused 缺失）；即使业务数据存在，climate 仍为 null
        LessonSessionEntity session = session("ls-4", "course-1", "class-1", "teacher-1");
        when(lessonSessionRepository.findByIdForUpdate("ls-4")).thenReturn(Optional.of(session));
        when(reportRepository.findByLessonSessionIdForUpdate("ls-4")).thenReturn(Optional.empty());
        when(courseRepository.findById("course-1")).thenReturn(Optional.of(course("course-1", "课程")));
        when(classGroupRepository.findById("class-1")).thenReturn(Optional.of(group("class-1", "班级")));
        // 2 个窗口，仅有视频分，无语音分与 fused 分
        when(windowFeatureRepository.findByLessonSessionIdOrderByWindowIndexAsc("ls-4"))
                .thenReturn(List.of(
                        window("w-1", "ls-4", 0, 80.0, null, null),
                        window("w-2", "ls-4", 1, 70.0, null, null)));
        // 业务数据存在
        when(studentRepository.count()).thenReturn(30L);
        when(attendanceRepository.findByLessonSessionId("ls-4"))
                .thenReturn(List.of(new AttendanceRecordEntity()));

        var detail = service.generateReport("ls-4");

        assertEquals("REAL", detail.dataSource());
        // 参与度 = 视频分均值 (80+70)/2 = 75
        assertNotNull(detail.engagementScore());
        assertEquals(75.0, detail.engagementScore(), 1e-9);
        // 无语音分 → teaching null
        assertNull(detail.teachingScore());
        // 无 fused 分 → climate null，业务数据不得兜底
        assertNull(detail.climateScore());
        // 只有视频分：至少生成视频参与度建议，不生成语音/融合结论
        assertTrue(detail.suggestions().stream()
                .anyMatch(x -> "学生参与度".equals(x.category())));
        assertTrue(detail.suggestions().stream()
                .noneMatch(x -> "教学质量".equals(x.category())));
    }

    @Test
    void realModeFusedScoreDrivesClimate() {
        // 窗口 fused 分决定 climate，业务数据不参与
        LessonSessionEntity session = session("ls-6", "course-1", "class-1", "teacher-1");
        when(lessonSessionRepository.findByIdForUpdate("ls-6")).thenReturn(Optional.of(session));
        when(reportRepository.findByLessonSessionIdForUpdate("ls-6")).thenReturn(Optional.empty());
        when(courseRepository.findById("course-1")).thenReturn(Optional.of(course("course-1", "课程")));
        when(classGroupRepository.findById("class-1")).thenReturn(Optional.of(group("class-1", "班级")));
        // 2 窗口：video+speech+fused 都有值
        when(windowFeatureRepository.findByLessonSessionIdOrderByWindowIndexAsc("ls-6"))
                .thenReturn(List.of(
                        window("w-1", "ls-6", 0, 80.0, 70.0, 75.0),
                        window("w-2", "ls-6", 1, 70.0, 60.0, 65.0)));
        // 业务数据存在（若参与计算会得到不同分数）
        when(studentRepository.count()).thenReturn(30L);
        when(attendanceRepository.countByLessonSessionIdAndStatus(eq("ls-6"),
                eq(AttendanceRecordEntity.AttendanceStatus.PRESENT))).thenReturn(30L);
        when(attendanceRepository.findByLessonSessionId("ls-6"))
                .thenReturn(List.of(new AttendanceRecordEntity()));

        var detail = service.generateReport("ls-6");

        // 三维全部非 null
        assertNotNull(detail.engagementScore());
        assertNotNull(detail.teachingScore());
        assertNotNull(detail.climateScore());
        // climate = fused 均值 (75+65)/2 = 70
        assertEquals(70.0, detail.climateScore(), 1e-9);
        // 三维完整 → 生成建议
        assertFalse(detail.suggestions().isEmpty(), "三维完整时应生成建议");
    }

    @Test
    void businessDataDoesNotOverrideFusedClimate() {
        // 窗口 fused = 100，业务数据（若参与会得 85）不得覆盖
        LessonSessionEntity session = session("ls-5", "course-1", "class-1", "teacher-1");
        when(lessonSessionRepository.findByIdForUpdate("ls-5")).thenReturn(Optional.of(session));
        when(reportRepository.findByLessonSessionIdForUpdate("ls-5")).thenReturn(Optional.empty());
        when(courseRepository.findById("course-1")).thenReturn(Optional.of(course("course-1", "课程")));
        when(classGroupRepository.findById("class-1")).thenReturn(Optional.of(group("class-1", "班级")));
        when(windowFeatureRepository.findByLessonSessionIdOrderByWindowIndexAsc("ls-5")).thenReturn(
                List.of(window("w-1", "ls-5", 0, 100.0, 100.0, 100.0)));
        // 业务数据：出勤率 100%（40 分）、正确率 50%（15 分）、反馈 1 条积极（30 分）
        when(studentRepository.count()).thenReturn(30L);
        when(attendanceRepository.countByLessonSessionIdAndStatus(eq("ls-5"),
                eq(AttendanceRecordEntity.AttendanceStatus.PRESENT))).thenReturn(30L);
        when(attendanceRepository.countByLessonSessionIdAndStatus(eq("ls-5"),
                eq(AttendanceRecordEntity.AttendanceStatus.LATE))).thenReturn(0L);
        when(attendanceRepository.countByLessonSessionIdAndStatus(eq("ls-5"),
                eq(AttendanceRecordEntity.AttendanceStatus.ABSENT))).thenReturn(0L);
        var detail = service.generateReport("ls-5");

        assertEquals("REAL", detail.dataSource());
        assertNotNull(detail.engagementScore());
        assertEquals(100.0, detail.engagementScore(), 1e-9);
        // climate = fused 均值 = 100，不是业务数据的 85
        assertNotNull(detail.climateScore());
        assertEquals(100.0, detail.climateScore(), 1e-9);
    }

    @Test
    void audioOnlyReportStillHasClimateScore() {
        // 音频-only：视频维度缺失，但语音分与 fused 分存在，课堂氛围必须仍然可见
        LessonSessionEntity session = session("ls-audio", "course-1", "class-1", "teacher-1");
        when(lessonSessionRepository.findByIdForUpdate("ls-audio")).thenReturn(Optional.of(session));
        when(reportRepository.findByLessonSessionIdForUpdate("ls-audio")).thenReturn(Optional.empty());
        when(courseRepository.findById("course-1")).thenReturn(Optional.of(course("course-1", "课程")));
        when(classGroupRepository.findById("class-1")).thenReturn(Optional.of(group("class-1", "班级")));
        when(windowFeatureRepository.findByLessonSessionIdOrderByWindowIndexAsc("ls-audio"))
                .thenReturn(List.of(window("w-1", "ls-audio", 0, null, 70.0, 50.0)));
        when(studentRepository.countByClassId("class-1")).thenReturn(30L);
        when(attendanceRepository.countByLessonSessionIdAndStatus(anyString(), any())).thenReturn(0L);

        var detail = service.generateReport("ls-audio");

        assertNull(detail.engagementScore(), "音频-only 时视频维度缺失");
        assertNotNull(detail.teachingScore(), "音频-only 时语音维度应存在");
        assertNotNull(detail.climateScore(), "音频-only 时课堂氛围仍应可见");
        assertEquals(50.0, detail.climateScore(), 1e-9);
        assertEquals("pending_validation", detail.scoreValidation().get("climate"));
        assertEquals("unavailable", detail.scoreValidation().get("engagement"));
    }

    @Test
    void listReportsIncludesClimateScore() {
        LessonSessionEntity session = session("ls-list", "course-1", "class-1", "teacher-1");
        EvaluationReportEntity report = new EvaluationReportEntity();
        report.setLessonSessionId("ls-list");
        report.setStatus("draft");
        report.setEngagementScore(70.0);
        report.setTeachingScore(60.0);
        report.setClimateScore(66.0);
        report.setDataSource("REAL");
        when(lessonSessionRepository.findByTeacherId("teacher-1")).thenReturn(List.of(session));
        when(reportRepository.findByLessonSessionIdIn(any())).thenReturn(List.of(report));
        when(courseRepository.findAllById(any())).thenReturn(List.of(course("course-1", "课程")));
        when(classGroupRepository.findAllById(any())).thenReturn(List.of(group("class-1", "班级")));

        var summaries = service.listReports("teacher-1", "teacher");

        assertEquals(1, summaries.size());
        assertEquals(66.0, summaries.get(0).climateScore(), 1e-9);
        assertEquals("REAL", summaries.get(0).dataSource());
    }

    private LessonSessionEntity session(String id, String courseId, String classId, String teacherId) {
        LessonSessionEntity s = new LessonSessionEntity();
        s.setId(id); s.setCourseId(courseId); s.setClassId(classId);
        s.setTeacherId(teacherId); s.setTitle("测试课堂"); s.setDate(LocalDateTime.now());
        s.setStartTime(LocalDateTime.now()); s.setStatus(LessonSessionEntity.SessionStatus.COMPLETED);
        return s;
    }

    private com.macteval.model.entity.CourseEntity course(String id, String name) {
        com.macteval.model.entity.CourseEntity c = new com.macteval.model.entity.CourseEntity();
        c.setId(id); c.setName(name); return c;
    }

    private com.macteval.model.entity.ClassGroupEntity group(String id, String name) {
        com.macteval.model.entity.ClassGroupEntity g = new com.macteval.model.entity.ClassGroupEntity();
        g.setId(id); g.setName(name); return g;
    }

    @Test
    void legacyWindowsWithoutRuleProtocolAreExcluded() {
        LessonSessionEntity session = session("ls-old", "course-1", "class-1", "teacher-1");
        when(lessonSessionRepository.findByIdForUpdate("ls-old")).thenReturn(Optional.of(session));
        when(reportRepository.findByLessonSessionIdForUpdate("ls-old")).thenReturn(Optional.empty());
        when(courseRepository.findById("course-1")).thenReturn(Optional.of(course("course-1", "课程")));
        when(classGroupRepository.findById("class-1")).thenReturn(Optional.of(group("class-1", "班级")));
        LessonWindowFeatureEntity legacy = new LessonWindowFeatureEntity();
        legacy.setId("w-old");
        legacy.setLessonSessionId("ls-old");
        legacy.setWindowIndex(0);
        legacy.setVideoScore(80.0);
        legacy.setSpeechScore(70.0);
        legacy.setFusedScore(75.0);
        when(windowFeatureRepository.findByLessonSessionIdOrderByWindowIndexAsc("ls-old"))
                .thenReturn(List.of(legacy));
        when(studentRepository.count()).thenReturn(0L);
        when(attendanceRepository.findByLessonSessionId("ls-old")).thenReturn(List.of());

        var detail = service.generateReport("ls-old");

        assertNull(detail.climateScore(), "旧口径窗口不得混入新报告");
        assertNull(detail.engagementScore());
        assertNull(detail.teachingScore());
    }

    private LessonWindowFeatureEntity window(String id, String lessonId, int idx, Double video, Double speech, Double fused) {
        LessonWindowFeatureEntity w = new LessonWindowFeatureEntity();
        w.setId(id); w.setLessonSessionId(lessonId); w.setWindowIndex(idx);
        w.setVideoScore(video); w.setSpeechScore(speech); w.setFusedScore(fused);
        w.setWindowStart(LocalDateTime.now()); w.setWindowEnd(LocalDateTime.now().plusMinutes(1));
        w.setVideoFeatures("{\"rule_protocol\":\"v1-system-proxy\"}");
        w.setSpeechFeatures("{\"rule_protocol\":\"v1\"}");
        return w;
    }
}
