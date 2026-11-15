package com.macteval.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.macteval.model.entity.AnalysisJobEntity;
import com.macteval.model.entity.BehaviorObservationEntity;
import com.macteval.model.entity.EvaluationReportEntity;
import com.macteval.model.entity.EvaluationReportVersionEntity;
import com.macteval.model.entity.LessonSessionEntity;
import com.macteval.model.entity.LessonWindowFeatureEntity;
import com.macteval.repository.AnalysisJobRepository;
import com.macteval.repository.AttendanceRecordRepository;
import com.macteval.repository.BehaviorObservationRepository;
import com.macteval.repository.ClassGroupRepository;
import com.macteval.repository.CourseRepository;
import com.macteval.repository.EvaluationReportRepository;
import com.macteval.repository.EvaluationReportVersionRepository;
import com.macteval.repository.LessonSessionRepository;
import com.macteval.repository.LessonTranscriptRepository;
import com.macteval.repository.LessonWindowFeatureRepository;
import com.macteval.repository.StudentRepository;
import com.macteval.repository.SuggestionRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;

import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * R04 回归：窗口按分析版本保存，报告精确引用同一版本。
 *
 * <p>复现序列：J1 在 T0 只得到 PARTIAL，J2 在 T1 成功，J1 在 T2 才补偿对齐。
 * 旧实现下 J1 的对齐会整课堂删除再写入 J1 数据，而报告按“最新完成”选中 J2，
 * 结果是任务号/截图来自 J2、分数来自 J1。</p>
 */
class WindowVersionIsolationTest {

    private BehaviorObservationRepository behaviorRepository;
    private LessonTranscriptRepository transcriptRepository;
    private LessonWindowFeatureRepository windowRepository;
    private AnalysisJobRepository analysisJobRepository;
    private WindowAlignmentService alignmentService;

    private LessonSessionRepository lessonSessionRepository;
    private CourseRepository courseRepository;
    private ClassGroupRepository classGroupRepository;
    private AttendanceRecordRepository attendanceRepository;
    private EvaluationReportRepository reportRepository;
    private EvaluationReportVersionRepository versionRepository;
    private SuggestionRepository suggestionRepository;
    private StudentRepository studentRepository;
    private ReportService reportService;

    /** 快照序列化需要 JSR-310 模块，与生产 Jackson 配置一致。 */
    private final ObjectMapper objectMapper = new ObjectMapper().findAndRegisterModules();

    @BeforeEach
    void setUp() {
        behaviorRepository = mock(BehaviorObservationRepository.class);
        transcriptRepository = mock(LessonTranscriptRepository.class);
        windowRepository = mock(LessonWindowFeatureRepository.class);
        analysisJobRepository = mock(AnalysisJobRepository.class);

        alignmentService = new WindowAlignmentService(
                behaviorRepository, transcriptRepository, windowRepository,
                new FusionService(), new RuleBaselineService(), objectMapper,
                mock(LessonSessionRepository.class), mock(com.macteval.security.ResourceGuard.class),
                analysisJobRepository,
                mock(com.macteval.repository.BehaviorEvidenceFrameRepository.class),
                mock(MinioService.class));

        lessonSessionRepository = mock(LessonSessionRepository.class);
        courseRepository = mock(CourseRepository.class);
        classGroupRepository = mock(ClassGroupRepository.class);
        attendanceRepository = mock(AttendanceRecordRepository.class);
        reportRepository = mock(EvaluationReportRepository.class);
        versionRepository = mock(EvaluationReportVersionRepository.class);
        suggestionRepository = mock(SuggestionRepository.class);
        studentRepository = mock(StudentRepository.class);

        reportService = new ReportService(
                reportRepository, versionRepository, lessonSessionRepository,
                courseRepository, classGroupRepository,
                behaviorRepository, suggestionRepository, analysisJobRepository,
                new SuggestionEngine(objectMapper), studentRepository, windowRepository,
                objectMapper);
    }

    // ---------------------------------------------------------------- (a) 只删/写自己版本

    @Test
    void compensateAlignmentOnlyTouchesItsOwnVersion() {
        LocalDateTime t0 = LocalDateTime.of(2026, 10, 27, 9, 0, 0);
        when(behaviorRepository.findByLessonSessionIdAndJobIdOrderByWindowIndexAsc("ls-1", "J1"))
                .thenReturn(List.of(obs(t0, t0.plusSeconds(60), "0.4")));
        when(transcriptRepository.findFirstByLessonSessionIdOrderByCreatedAtDescIdDesc("ls-1"))
                .thenReturn(Optional.empty());

        // J1 在 T2 的补偿对齐：显式传入 J1 作为视频版本。
        alignmentService.alignLesson("ls-1", "J1", null);

        verify(windowRepository).deleteByLessonSessionIdAndJobIds("ls-1", "J1", "");
        verify(windowRepository, never()).deleteByLessonSessionId(anyString());
        verify(windowRepository, never())
                .deleteByLessonSessionIdAndJobIds(anyString(), eq("J2"), anyString());

        @SuppressWarnings("unchecked")
        ArgumentCaptor<List<LessonWindowFeatureEntity>> captor = ArgumentCaptor.forClass(List.class);
        verify(windowRepository).saveAll(captor.capture());
        List<LessonWindowFeatureEntity> written = captor.getValue();
        assertEquals(1, written.size());
        assertEquals("J1", written.get(0).getVideoJobId(), "写入行必须标注产生它的版本");
        assertEquals("", written.get(0).getAudioJobId(), "本版本没有音频任务时写空串");
    }

    // ------------------------------------------- (b) 报告读取的版本与记录的任务号一致

    @Test
    void reportReadsWindowsOfTheSameVersionItRecords() {
        when(lessonSessionRepository.findByIdForUpdate("ls-1")).thenReturn(Optional.of(session()));
        when(reportRepository.findByLessonSessionIdForUpdate("ls-1")).thenReturn(Optional.empty());
        when(courseRepository.findById("course-1")).thenReturn(Optional.empty());
        when(classGroupRepository.findById("class-1")).thenReturn(Optional.empty());

        when(analysisJobRepository.findFirstByTypeAndLessonSessionIdAndStatusOrderByCompletedAtDesc(
                AnalysisJobEntity.JobType.VIDEO_ANALYSIS, "ls-1", AnalysisJobEntity.JobStatus.SUCCEEDED))
                .thenReturn(Optional.of(job("J2", AnalysisJobEntity.JobType.VIDEO_ANALYSIS)));
        when(analysisJobRepository.findFirstByTypeAndLessonSessionIdAndStatusOrderByCompletedAtDesc(
                AnalysisJobEntity.JobType.AUDIO_ANALYSIS, "ls-1", AnalysisJobEntity.JobStatus.SUCCEEDED))
                .thenReturn(Optional.empty());

        // 课堂里同时存在 J1（旧补偿写出）与 J2（最新）两套窗口。
        when(windowRepository.findByLessonSessionIdAndJobIdsOrderByWindowIndexAsc("ls-1", "J2", ""))
                .thenReturn(List.of(window("w-J2", 0, 70.0)));
        when(windowRepository.findByLessonSessionIdOrderByWindowIndexAsc("ls-1"))
                .thenReturn(List.of(window("w-J1", 0, 20.0), window("w-J2", 0, 70.0)));

        var detail = reportService.generateReport("ls-1");

        assertEquals("J2", detail.analysisVideoJobId());
        // 分数必须来自 J2 的窗口（70），不能是 J1 的 20。
        assertEquals(70.0, detail.climateScore(), 1e-9);
        assertEquals(1, detail.evidence().windowCount());
        // 读取时请求的版本组合必须等于报告记录的任务组合（同一版本，不混用）。
        // 分数与数据覆盖两处读取都按同一组合，因此至少一次即可，但不得读别的版本。
        verify(windowRepository, org.mockito.Mockito.atLeastOnce())
                .findByLessonSessionIdAndJobIdsOrderByWindowIndexAsc("ls-1", "J2", "");
        verify(windowRepository, never())
                .findByLessonSessionIdAndJobIdsOrderByWindowIndexAsc("ls-1", "J1", "");
    }

    @Test
    void reportFallsBackToHistoricalRowsWhenVersionHasNoWindows() {
        LessonSessionEntity legacy = session();
        legacy.setId("ls-legacy");
        when(lessonSessionRepository.findByIdForUpdate("ls-legacy")).thenReturn(Optional.of(legacy));
        when(reportRepository.findByLessonSessionIdForUpdate("ls-legacy")).thenReturn(Optional.empty());
        when(courseRepository.findById("course-1")).thenReturn(Optional.empty());
        when(classGroupRepository.findById("class-1")).thenReturn(Optional.empty());

        when(analysisJobRepository.findFirstByTypeAndLessonSessionIdAndStatusOrderByCompletedAtDesc(
                AnalysisJobEntity.JobType.VIDEO_ANALYSIS, "ls-legacy", AnalysisJobEntity.JobStatus.SUCCEEDED))
                .thenReturn(Optional.empty());
        when(analysisJobRepository.findFirstByTypeAndLessonSessionIdAndStatusOrderByCompletedAtDesc(
                AnalysisJobEntity.JobType.AUDIO_ANALYSIS, "ls-legacy", AnalysisJobEntity.JobStatus.SUCCEEDED))
                .thenReturn(Optional.empty());
        when(windowRepository.findByLessonSessionIdAndJobIdsOrderByWindowIndexAsc("ls-legacy", "", ""))
                .thenReturn(List.of());
        // 历史行（V30 之前写入，两列为 NULL）仍可被读到。
        when(windowRepository.findByLessonSessionIdOrderByWindowIndexAsc("ls-legacy"))
                .thenReturn(List.of(window("w-legacy", 0, 55.0)));

        var detail = reportService.generateReport("ls-legacy");

        assertEquals(55.0, detail.climateScore(), 1e-9);
        assertEquals("windows_ready", detail.scoreValidation().get("windowState"),
                "无任务但有历史窗口 → 仍是 windows_ready（分数来自历史行）");
    }

    // ---------------------------------- (d) R11：选定任务但无窗口时禁止历史回退

    /**
     * R11 核心回归：J2 已成功但 J2 的窗口尚未生成（对齐 PENDING / 结果为空），
     * 课堂里仍存在历史行（两列皆空）。旧实现会回退到历史行，报告于是显示
     * “任务号来自 J2、分数来自历史行”。修复后必须返回无窗口（数据不足）。
     */
    @Test
    void succeededJobWithNoWindowsMustNotFallBackToHistoricalRows() {
        LessonSessionEntity legacy = session();
        legacy.setId("ls-r11");
        when(lessonSessionRepository.findByIdForUpdate("ls-r11")).thenReturn(Optional.of(legacy));
        when(reportRepository.findByLessonSessionIdForUpdate("ls-r11")).thenReturn(Optional.empty());
        when(courseRepository.findById("course-1")).thenReturn(Optional.empty());
        when(classGroupRepository.findById("class-1")).thenReturn(Optional.empty());

        when(analysisJobRepository.findFirstByTypeAndLessonSessionIdAndStatusOrderByCompletedAtDesc(
                AnalysisJobEntity.JobType.VIDEO_ANALYSIS, "ls-r11", AnalysisJobEntity.JobStatus.SUCCEEDED))
                .thenReturn(Optional.of(job("J2", AnalysisJobEntity.JobType.VIDEO_ANALYSIS)));
        when(analysisJobRepository.findFirstByTypeAndLessonSessionIdAndStatusOrderByCompletedAtDesc(
                AnalysisJobEntity.JobType.AUDIO_ANALYSIS, "ls-r11", AnalysisJobEntity.JobStatus.SUCCEEDED))
                .thenReturn(Optional.empty());

        // J2 组合没有任何窗口行。
        when(windowRepository.findByLessonSessionIdAndJobIdsOrderByWindowIndexAsc("ls-r11", "J2", ""))
                .thenReturn(List.of());
        // 但课堂里存在历史行（两列 NULL），分数是旧的 12 分。
        when(windowRepository.findByLessonSessionIdOrderByWindowIndexAsc("ls-r11"))
                .thenReturn(List.of(window("w-legacy", 0, 12.0)));

        var detail = reportService.generateReport("ls-r11");

        assertEquals("J2", detail.analysisVideoJobId(), "报告仍记录选定的任务");
        assertNull(detail.climateScore(), "禁止用历史行补分：氛围分必须为空（数据不足）");
        assertNull(detail.engagementScore(), "参与度不得来自历史行");
        assertNull(detail.teachingScore(), "教学分不得来自历史行");
        assertEquals(0, detail.evidence().windowCount(), "窗口数必须为 0，而不是历史行数");

        // 数据覆盖也不能把历史行的视频/语音分算成“有数据”。
        assertFalse(detail.dataCoverage().contains("\"video\":true"),
                "历史行不得让 dataCoverage.video 变真：" + detail.dataCoverage());
        assertFalse(detail.dataCoverage().contains("\"speech\":true"),
                "历史行不得让 dataCoverage.speech 变真：" + detail.dataCoverage());

        // 摘要必须表达数据不足/窗口未完成，而不是给出一个分数评价。
        assertTrue(detail.summary().contains("统一时间窗尚未生成")
                        || detail.summary().contains("数据不足"),
                "摘要应说明窗口未完成/数据不足：" + detail.summary());
        // R11：机器可读状态必须是 windows_pending，而不是 legacy_no_job。
        assertEquals("windows_pending", detail.scoreValidation().get("windowState"),
                "选定了任务但没有窗口 → windows_pending，不能伪装成纯 legacy 报告");

        // 根本不应读课堂历史行。
        verify(windowRepository, never()).findByLessonSessionIdOrderByWindowIndexAsc("ls-r11");
    }

    /**
     * R11 加固：windowState 不能被“版本盲的行为观测行”骗成 windows_ready。
     *
     * <p>报告的 {@code windowCount} 在没有窗口特征时会退回课堂内<b>全部</b>行为观测
     * 行数（不按 jobId 过滤）。若用 windowCount 判定窗口状态，“J2 已成功但窗口未
     * 生成、却存在 J1 时代行为观测”的报告会被错标为 windows_ready，前端于是既隐藏
     * 数据不足提示、又把旧观测数当成“窗口 N”展示。本测试锁定判据必须版本精确。</p>
     */
    @Test
    void windowStateIsNotFooledByVersionBlindBehaviorRows() {
        LessonSessionEntity legacy = session();
        legacy.setId("ls-blind");
        when(lessonSessionRepository.findByIdForUpdate("ls-blind")).thenReturn(Optional.of(legacy));
        when(reportRepository.findByLessonSessionIdForUpdate("ls-blind")).thenReturn(Optional.empty());
        when(courseRepository.findById("course-1")).thenReturn(Optional.empty());
        when(classGroupRepository.findById("class-1")).thenReturn(Optional.empty());

        // J2 成功，但 J2 的窗口尚未生成。
        when(analysisJobRepository.findFirstByTypeAndLessonSessionIdAndStatusOrderByCompletedAtDesc(
                AnalysisJobEntity.JobType.VIDEO_ANALYSIS, "ls-blind", AnalysisJobEntity.JobStatus.SUCCEEDED))
                .thenReturn(Optional.of(job("J2", AnalysisJobEntity.JobType.VIDEO_ANALYSIS)));
        when(analysisJobRepository.findFirstByTypeAndLessonSessionIdAndStatusOrderByCompletedAtDesc(
                AnalysisJobEntity.JobType.AUDIO_ANALYSIS, "ls-blind", AnalysisJobEntity.JobStatus.SUCCEEDED))
                .thenReturn(Optional.empty());
        when(windowRepository.findByLessonSessionIdAndJobIdsOrderByWindowIndexAsc("ls-blind", "J2", ""))
                .thenReturn(List.of());

        // 课堂里残留 3 条旧行为观测（版本盲查询会返回它们）。
        when(behaviorRepository.findByLessonSessionIdOrderByWindowStartAsc("ls-blind"))
                .thenReturn(List.of(obsFor("ls-blind", "J1"), obsFor("ls-blind", "J1"),
                        obsFor("ls-blind", "J1")));

        var detail = reportService.generateReport("ls-blind");

        assertEquals("windows_pending", detail.scoreValidation().get("windowState"),
                "有任务但没有版本精确窗口 → windows_pending，不能被旧行为观测行骗成 windows_ready");
        assertNull(detail.climateScore(), "数据不足时不得出分");
        // 窗口数只能来自版本精确的窗口行；旧实现会退回课堂内全部行为观测行数（3）。
        assertEquals(0, detail.evidence().windowCount(),
                "窗口数不得退回课堂级行为观测行数");
        // 数据覆盖里的 behavior 也必须按所选任务统计，不能把 J1 的观测算进 J2 的报告。
        assertFalse(detail.dataCoverage().contains("\"behavior\":true"),
                "旧任务的行为观测不得让本报告的 dataCoverage.behavior 变真："
                        + detail.dataCoverage());
        verify(behaviorRepository, never())
                .findByLessonSessionIdOrderByWindowStartAsc("ls-blind");
    }

    private BehaviorObservationEntity obsFor(String lessonId, String jobId) {
        BehaviorObservationEntity o = new BehaviorObservationEntity();
        o.setLessonSessionId(lessonId);
        o.setJobId(jobId);
        o.setWindowStart(java.time.LocalDateTime.of(2026, 10, 27, 9, 0));
        o.setWindowEnd(java.time.LocalDateTime.of(2026, 10, 27, 9, 1));
        o.setAggregatedUprightProportion("0.6");
        return o;
    }

    /** 视频 J2 + 音频 A2 的组合必须精确读取该组合，不读无任务组合（空串+空串）。 */
    @Test
    void exactVideoAndAudioCombinationIsRead() {
        LessonSessionEntity legacy = session();
        legacy.setId("ls-combo");
        when(lessonSessionRepository.findByIdForUpdate("ls-combo")).thenReturn(Optional.of(legacy));
        when(reportRepository.findByLessonSessionIdForUpdate("ls-combo")).thenReturn(Optional.empty());
        when(courseRepository.findById("course-1")).thenReturn(Optional.empty());
        when(classGroupRepository.findById("class-1")).thenReturn(Optional.empty());

        when(analysisJobRepository.findFirstByTypeAndLessonSessionIdAndStatusOrderByCompletedAtDesc(
                AnalysisJobEntity.JobType.VIDEO_ANALYSIS, "ls-combo", AnalysisJobEntity.JobStatus.SUCCEEDED))
                .thenReturn(Optional.of(job("J2", AnalysisJobEntity.JobType.VIDEO_ANALYSIS)));
        when(analysisJobRepository.findFirstByTypeAndLessonSessionIdAndStatusOrderByCompletedAtDesc(
                AnalysisJobEntity.JobType.AUDIO_ANALYSIS, "ls-combo", AnalysisJobEntity.JobStatus.SUCCEEDED))
                .thenReturn(Optional.of(job("A2", AnalysisJobEntity.JobType.AUDIO_ANALYSIS)));

        when(windowRepository.findByLessonSessionIdAndJobIdsOrderByWindowIndexAsc("ls-combo", "J2", "A2"))
                .thenReturn(List.of(window("w-combo", 0, 81.0)));

        var detail = reportService.generateReport("ls-combo");

        assertEquals("J2", detail.analysisVideoJobId());
        assertEquals("A2", detail.analysisAudioJobId());
        assertEquals(81.0, detail.climateScore(), 1e-9);
        verify(windowRepository, never())
                .findByLessonSessionIdAndJobIdsOrderByWindowIndexAsc("ls-combo", "J2", "");
        verify(windowRepository, never())
                .findByLessonSessionIdAndJobIdsOrderByWindowIndexAsc("ls-combo", "", "");
    }

    /**
     * 只有视频任务、没有音频任务时，精确组合是 (J2, "")；该组合无窗口就必须是
     * 空，不能退化为“无任务组合”（"",""）去捡历史行。
     */
    @Test
    void missingAudioJobDoesNotDegradeToNoJobCombination() {
        LessonSessionEntity legacy = session();
        legacy.setId("ls-video-only");
        when(lessonSessionRepository.findByIdForUpdate("ls-video-only")).thenReturn(Optional.of(legacy));
        when(reportRepository.findByLessonSessionIdForUpdate("ls-video-only")).thenReturn(Optional.empty());
        when(courseRepository.findById("course-1")).thenReturn(Optional.empty());
        when(classGroupRepository.findById("class-1")).thenReturn(Optional.empty());

        when(analysisJobRepository.findFirstByTypeAndLessonSessionIdAndStatusOrderByCompletedAtDesc(
                AnalysisJobEntity.JobType.VIDEO_ANALYSIS, "ls-video-only", AnalysisJobEntity.JobStatus.SUCCEEDED))
                .thenReturn(Optional.of(job("J2", AnalysisJobEntity.JobType.VIDEO_ANALYSIS)));
        when(analysisJobRepository.findFirstByTypeAndLessonSessionIdAndStatusOrderByCompletedAtDesc(
                AnalysisJobEntity.JobType.AUDIO_ANALYSIS, "ls-video-only", AnalysisJobEntity.JobStatus.SUCCEEDED))
                .thenReturn(Optional.empty());

        when(windowRepository.findByLessonSessionIdAndJobIdsOrderByWindowIndexAsc("ls-video-only", "J2", ""))
                .thenReturn(List.of());
        when(windowRepository.findByLessonSessionIdAndJobIdsOrderByWindowIndexAsc("ls-video-only", "", ""))
                .thenReturn(List.of(window("w-history", 0, 33.0)));
        when(windowRepository.findByLessonSessionIdOrderByWindowIndexAsc("ls-video-only"))
                .thenReturn(List.of(window("w-history", 0, 33.0)));

        var detail = reportService.generateReport("ls-video-only");

        assertNull(detail.climateScore(), "缺一模态不得退回无任务组合去捡历史分数");
        verify(windowRepository, never())
                .findByLessonSessionIdAndJobIdsOrderByWindowIndexAsc("ls-video-only", "", "");
    }

    // ------------------------------------------------------- (c) 旧发布快照不被改写

    @Test
    void realignDoesNotRewritePreviouslyPublishedSnapshot() {
        LessonSessionEntity session = session();
        when(lessonSessionRepository.findByIdForUpdate("ls-1")).thenReturn(Optional.of(session));
        when(courseRepository.findById("course-1")).thenReturn(Optional.empty());
        when(classGroupRepository.findById("class-1")).thenReturn(Optional.empty());
        when(analysisJobRepository.findFirstByTypeAndLessonSessionIdAndStatusOrderByCompletedAtDesc(
                any(), anyString(), any()))
                .thenReturn(Optional.empty());
        when(windowRepository.findByLessonSessionIdAndJobIdsOrderByWindowIndexAsc(anyString(), anyString(), anyString()))
                .thenReturn(List.of(window("w-J2", 0, 66.0)));

        EvaluationReportEntity report = new EvaluationReportEntity();
        report.setId("report-1");
        report.setLessonSessionId("ls-1");
        report.setCourseId("course-1");
        report.setClassId("class-1");
        report.setTeacherId("teacher-1");
        report.setTitle("课堂评价报告");
        report.setVersion(1);
        report.setStatus("draft");
        report.setClimateScore(66.0);
        report.setDataSource("REAL");
        when(reportRepository.findByLessonSessionIdForUpdate("ls-1")).thenReturn(Optional.of(report));

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

        var published = reportService.publishReport("ls-1", "teacher-1");
        assertEquals("published", published.status());
        assertEquals(1, snapshots.size());
        String frozenHash = snapshots.getFirst().getContentHash();
        String frozenJson = snapshots.getFirst().getSnapshotJson();
        assertNotNull(frozenHash);

        // J1 的补偿对齐在发布之后发生：只重建自己版本的派生窗口。
        LocalDateTime t0 = LocalDateTime.of(2026, 10, 27, 9, 0, 0);
        when(behaviorRepository.findByLessonSessionIdAndJobIdOrderByWindowIndexAsc("ls-1", "J1"))
                .thenReturn(List.of(obs(t0, t0.plusSeconds(60), "0.4")));
        when(transcriptRepository.findFirstByLessonSessionIdOrderByCreatedAtDescIdDesc("ls-1"))
                .thenReturn(Optional.empty());
        alignmentService.alignLesson("ls-1", "J1", null);

        assertEquals(1, snapshots.size(), "补偿不得新增/替换发布快照");
        assertEquals(frozenHash, snapshots.getFirst().getContentHash(), "发布快照内容哈希必须不变");
        assertEquals(frozenJson, snapshots.getFirst().getSnapshotJson(), "发布快照正文必须不变");
        assertNotEquals("", frozenJson);
    }

    // ------------------------------------------------------------------------ helpers

    private BehaviorObservationEntity obs(LocalDateTime start, LocalDateTime end, String upright) {
        BehaviorObservationEntity o = new BehaviorObservationEntity();
        o.setLessonSessionId("ls-1");
        o.setJobId("J1");
        o.setWindowStart(start);
        o.setWindowEnd(end);
        o.setAggregatedUprightProportion(upright);
        return o;
    }

    private AnalysisJobEntity job(String id, AnalysisJobEntity.JobType type) {
        AnalysisJobEntity job = new AnalysisJobEntity();
        job.setId(id);
        job.setType(type);
        job.setStatus(AnalysisJobEntity.JobStatus.SUCCEEDED);
        job.setAttempt(1);
        return job;
    }

    private LessonWindowFeatureEntity window(String id, int index, double fused) {
        LessonWindowFeatureEntity w = new LessonWindowFeatureEntity();
        w.setId(id);
        w.setLessonSessionId("ls-1");
        w.setWindowIndex(index);
        w.setWindowStart(LocalDateTime.now());
        w.setWindowEnd(LocalDateTime.now().plusMinutes(1));
        w.setVideoScore(fused);
        w.setSpeechScore(fused);
        w.setFusedScore(fused);
        w.setVideoFeatures("{\"rule_protocol\":\"v1-system-proxy\"}");
        w.setSpeechFeatures("{\"rule_protocol\":\"v1\"}");
        return w;
    }

    private LessonSessionEntity session() {
        LessonSessionEntity s = new LessonSessionEntity();
        s.setId("ls-1");
        s.setCourseId("course-1");
        s.setClassId("class-1");
        s.setTeacherId("teacher-1");
        s.setTitle("版本隔离测试课堂");
        s.setDate(LocalDateTime.now());
        s.setStartTime(LocalDateTime.now());
        s.setStatus(LessonSessionEntity.SessionStatus.COMPLETED);
        return s;
    }
}
