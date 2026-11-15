package com.macteval.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.macteval.dto.ReportDetailDTO;
import com.macteval.model.entity.EvaluationReportEntity;
import com.macteval.model.entity.EvaluationReportVersionEntity;
import com.macteval.model.entity.LessonSessionEntity;
import com.macteval.model.entity.LessonWindowFeatureEntity;
import com.macteval.model.entity.SuggestionEntity;
import com.macteval.repository.*;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.Mockito.*;

/**
 * BE06/BE07：发布快照、重新生成版本与建议持久化闭环。
 */
class ReportLifecycleTest {

    private EvaluationReportRepository reportRepository;
    private EvaluationReportVersionRepository versionRepository;
    private LessonSessionRepository lessonSessionRepository;
    private CourseRepository courseRepository;
    private ClassGroupRepository classGroupRepository;
    private AttendanceRecordRepository attendanceRepository;
    private BehaviorObservationRepository behaviorRepository;
    private SuggestionRepository suggestionRepository;
    private AnalysisJobRepository analysisJobRepository;
    private LessonWindowFeatureRepository windowFeatureRepository;
    private StudentRepository studentRepository;
    private ReportService service;

    @BeforeEach
    void setUp() {
        reportRepository = mock(EvaluationReportRepository.class);
        versionRepository = mock(EvaluationReportVersionRepository.class);
        lessonSessionRepository = mock(LessonSessionRepository.class);
        courseRepository = mock(CourseRepository.class);
        classGroupRepository = mock(ClassGroupRepository.class);
        attendanceRepository = mock(AttendanceRecordRepository.class);
        behaviorRepository = mock(BehaviorObservationRepository.class);
        suggestionRepository = mock(SuggestionRepository.class);
        analysisJobRepository = mock(AnalysisJobRepository.class);
        windowFeatureRepository = mock(LessonWindowFeatureRepository.class);
        studentRepository = mock(StudentRepository.class);
        service = new ReportService(
                reportRepository, versionRepository, lessonSessionRepository,
                courseRepository, classGroupRepository,
                behaviorRepository, suggestionRepository, analysisJobRepository,
                new SuggestionEngine(new ObjectMapper().findAndRegisterModules()), studentRepository,
                windowFeatureRepository, new ObjectMapper().findAndRegisterModules());
    }

    @Test
    void publishFreezesSnapshotAndRegenerationCreatesNewDraft() {
        LessonSessionEntity session = session();
        EvaluationReportEntity report = report("draft", 1, 70.0);
        when(lessonSessionRepository.findByIdForUpdate("ls-1")).thenReturn(Optional.of(session));
        when(reportRepository.findByLessonSessionIdForUpdate("ls-1")).thenReturn(Optional.of(report));
        when(courseRepository.findById("course-1")).thenReturn(Optional.empty());
        when(classGroupRepository.findById("class-1")).thenReturn(Optional.empty());

        List<EvaluationReportVersionEntity> snapshots = new ArrayList<>();
        when(versionRepository.findByLessonSessionIdAndVersion(eq("ls-1"), anyInt()))
                .thenAnswer(inv -> snapshots.stream()
                        .filter(v -> v.getVersion().equals(inv.getArgument(1)))
                        .findFirst());
        when(versionRepository.save(any(EvaluationReportVersionEntity.class))).thenAnswer(inv -> {
            EvaluationReportVersionEntity v = inv.getArgument(0);
            snapshots.add(v);
            return v;
        });

        ReportDetailDTO published = service.publishReport("ls-1", "teacher-1");

        assertEquals("published", published.status());
        assertEquals(1, published.version());
        assertNotNull(published.publishedBy());
        assertNotNull(published.evidence());
        assertEquals(1, snapshots.size(), "发布必须固化一份版本快照");
        assertNotNull(snapshots.getFirst().getContentHash());

        // 重新生成：旧发布内容保留在快照，工作行进入 version 2 draft
        when(windowFeatureRepository.findByLessonSessionIdOrderByWindowIndexAsc("ls-1"))
                .thenReturn(List.of(window()));
        when(suggestionRepository.findByLessonSessionIdAndStatus("ls-1", "pending"))
                .thenReturn(List.of());

        ReportDetailDTO regenerated = service.generateReport("ls-1", "teacher-1");

        assertEquals("draft", regenerated.status());
        assertEquals(2, regenerated.version());
        assertNull(regenerated.publishedAt());
        assertEquals(1, snapshots.size(), "已有快照不得被覆盖或重复创建");
        assertEquals("published", snapshots.getFirst().getStatus());
    }

    @Test
    void studentSeesLastPublishedSnapshotWhileWorkingRowIsDraft() {
        LessonSessionEntity session = session();
        EvaluationReportEntity draft = report("draft", 3, 88.0);
        when(reportRepository.findByLessonSessionId("ls-1")).thenReturn(Optional.of(draft));
        when(lessonSessionRepository.findById("ls-1")).thenReturn(Optional.of(session));
        var student = new com.macteval.model.entity.StudentEntity();
        student.setId("student-1");
        student.setClassId("class-1");
        when(studentRepository.findById("student-1")).thenReturn(Optional.of(student));

        EvaluationReportVersionEntity version = new EvaluationReportVersionEntity();
        version.setId("v-2");
        version.setReportId("report-1");
        version.setLessonSessionId("ls-1");
        version.setVersion(2);
        version.setStatus("published");
        version.setPublishedAt(LocalDateTime.now().minusDays(1));
        version.setCreatedAt(LocalDateTime.now().minusDays(1));
        version.setSnapshotJson("{\"detail\":{\"id\":\"report-1\",\"lessonSessionId\":\"ls-1\","
                + "\"status\":\"published\",\"version\":2,\"climateScore\":70.0}}");
        when(versionRepository.findFirstByLessonSessionIdAndStatusOrderByVersionDesc("ls-1", "published"))
                .thenReturn(Optional.of(version));

        var detail = service.getReportForStudent("ls-1", "student-1");

        assertEquals("published", detail.status());
        assertEquals(2, detail.version());
        assertEquals(70.0, detail.climateScore(), 1e-9);
    }

    @Test
    void publishRejectsEmptyReport() {
        LessonSessionEntity session = session();
        EvaluationReportEntity report = report("draft", 1, null);
        report.setDataSource("EMPTY");
        when(lessonSessionRepository.findByIdForUpdate("ls-1")).thenReturn(Optional.of(session));
        when(reportRepository.findByLessonSessionIdForUpdate("ls-1")).thenReturn(Optional.of(report));

        assertThrows(com.macteval.exception.BusinessException.class,
                () -> service.publishReport("ls-1", "teacher-1"));
        verify(reportRepository, never()).save(any());
        verify(versionRepository, never()).save(any());
    }

    @Test
    void generatedSuggestionsArePersistedWithStableIdsAndEvidence() {
        LessonSessionEntity session = session();
        when(lessonSessionRepository.findByIdForUpdate("ls-1")).thenReturn(Optional.of(session));
        when(reportRepository.findByLessonSessionIdForUpdate("ls-1")).thenReturn(Optional.empty());
        when(courseRepository.findById("course-1")).thenReturn(Optional.empty());
        when(classGroupRepository.findById("class-1")).thenReturn(Optional.empty());
        when(windowFeatureRepository.findByLessonSessionIdOrderByWindowIndexAsc("ls-1"))
                .thenReturn(List.of(window()));
        when(suggestionRepository.findByLessonSessionIdAndStatus("ls-1", "pending"))
                .thenReturn(List.of());

        List<SuggestionEntity> persisted = new ArrayList<>();
        when(suggestionRepository.saveAll(any())).thenAnswer(inv -> {
            @SuppressWarnings("unchecked")
            List<SuggestionEntity> rows = (List<SuggestionEntity>) inv.getArgument(0);
            persisted.addAll(rows);
            return rows;
        });

        ReportDetailDTO detail = service.generateReport("ls-1", "teacher-1");

        assertFalse(persisted.isEmpty());
        assertFalse(detail.suggestions().isEmpty());
        var persistedIds = persisted.stream().map(SuggestionEntity::getId).toList();
        var dtoIds = detail.suggestions().stream().map(d -> d.id()).toList();
        assertEquals(persistedIds, dtoIds, "报告 JSON 与 suggestions 表必须使用同一 ID");
        assertTrue(persisted.stream().allMatch(s -> s.getRuleId() != null
                && s.getRuleVersion() != null && s.getEvidenceRefs() != null));
        assertTrue(persisted.stream().allMatch(s -> Integer.valueOf(1).equals(s.getReportVersion())));

        // 采纳状态从数据库回读到报告详情（ID 稳定）
        persisted.getFirst().setStatus("adopted");
        persisted.getFirst().setAdoptedAt(LocalDateTime.now());
        when(reportRepository.findByLessonSessionId("ls-1"))
                .thenReturn(Optional.of(report("draft", 1, 70.0)));
        when(lessonSessionRepository.findById("ls-1")).thenReturn(Optional.of(session));
        when(suggestionRepository.findByLessonSessionId("ls-1")).thenReturn(persisted);

        ReportDetailDTO reread = service.getReport("ls-1", "teacher-1");
        assertEquals("adopted", reread.suggestions().getFirst().status());
        assertNotNull(reread.suggestions().getFirst().resolvedAt());
    }

    private LessonSessionEntity session() {
        LessonSessionEntity session = new LessonSessionEntity();
        session.setId("ls-1");
        session.setCourseId("course-1");
        session.setClassId("class-1");
        session.setTeacherId("teacher-1");
        session.setTitle("测试课堂");
        session.setDate(LocalDateTime.now());
        session.setStartTime(LocalDateTime.now());
        session.setStatus(LessonSessionEntity.SessionStatus.COMPLETED);
        return session;
    }

    private EvaluationReportEntity report(String status, int version, Double climate) {
        EvaluationReportEntity report = new EvaluationReportEntity();
        report.setId("report-1");
        report.setLessonSessionId("ls-1");
        report.setCourseId("course-1");
        report.setClassId("class-1");
        report.setTeacherId("teacher-1");
        report.setTitle("课堂评价报告");
        report.setStatus(status);
        report.setVersion(version);
        report.setDataSource(climate == null ? "EMPTY" : "REAL");
        report.setClimateScore(climate);
        report.setEngagementScore(climate);
        report.setTeachingScore(climate);
        report.setWindowCount(climate == null ? 0 : 1);
        report.setFeatureCount(1);
        report.setCoverage(0.5);
        return report;
    }

    private LessonWindowFeatureEntity window() {
        LessonWindowFeatureEntity w = new LessonWindowFeatureEntity();
        w.setId("w-1");
        w.setLessonSessionId("ls-1");
        w.setWindowIndex(0);
        w.setVideoScore(55.0);
        w.setSpeechScore(70.0);
        w.setFusedScore(62.5);
        w.setWindowStart(LocalDateTime.now());
        w.setWindowEnd(LocalDateTime.now().plusMinutes(1));
        w.setVideoFeatures("{\"rule_protocol\":\"v1-system-proxy\",\"upright_proportion\":0.55,"
                + "\"data_quality\":\"usable\"}");
        w.setSpeechFeatures("{\"rule_protocol\":\"v1\",\"speech_coverage\":0.6,"
                + "\"question_count\":1,\"speech_rate\":180}");
        return w;
    }
}
