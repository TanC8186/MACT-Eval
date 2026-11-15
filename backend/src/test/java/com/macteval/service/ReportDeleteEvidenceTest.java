package com.macteval.service;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.macteval.model.entity.BehaviorEvidenceFrameEntity;
import com.macteval.model.entity.EvaluationReportEntity;
import com.macteval.model.entity.EvaluationReportVersionEntity;
import com.macteval.model.entity.LessonSessionEntity;
import com.macteval.model.entity.SuggestionEntity;
import com.macteval.repository.AnalysisJobRepository;
import com.macteval.repository.AttendanceRecordRepository;
import com.macteval.repository.BehaviorEvidenceFrameRepository;
import com.macteval.repository.BehaviorObservationRepository;
import com.macteval.repository.ClassGroupRepository;
import com.macteval.repository.CourseRepository;
import com.macteval.repository.EvaluationReportRepository;
import com.macteval.repository.EvaluationReportVersionRepository;
import com.macteval.repository.LessonSessionRepository;
import com.macteval.repository.LessonWindowFeatureRepository;
import com.macteval.repository.StudentRepository;
import com.macteval.repository.SuggestionRepository;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.slf4j.LoggerFactory;

import java.util.List;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * SEC-11 回归：删除报告必须真的删掉课堂影像。
 *
 * <p>修复前 {@code deleteReportOn} 只删报告版本、待处理建议与报告行，证据帧与其
 * MinIO 对象原样留下，课堂截图最长滞留到 90 天保留期——这与 runbook 里对教师
 * 承诺的「删除即删除」直接冲突。本测试固定住四件容易在重构中丢掉的事：</p>
 *
 * <ol>
 *   <li>本课堂的帧被删：先删对象、再删行；</li>
 *   <li><b>对象删除失败时保留行</b>——删了行，夜间保留期清理就按 created_at
 *       再也选不到这条帧，孤儿对象会永久滞留；</li>
 *   <li>对象<b>明确不存在</b>（NOT_FOUND）时仍删行，否则留下一条指向空气的记录；</li>
 *   <li>别的课堂的帧一根手指都不能碰。</li>
 * </ol>
 */
class ReportDeleteEvidenceTest {

    private static final String LESSON = "ls-1";

    private EvaluationReportRepository reportRepository;
    private EvaluationReportVersionRepository versionRepository;
    private LessonSessionRepository lessonSessionRepository;
    private SuggestionRepository suggestionRepository;
    private BehaviorEvidenceFrameRepository frameRepository;
    private MinioService minioService;
    private ReportService service;

    private ListAppender<ILoggingEvent> logs;
    private Logger serviceLogger;

    @BeforeEach
    void setUp() {
        reportRepository = mock(EvaluationReportRepository.class);
        versionRepository = mock(EvaluationReportVersionRepository.class);
        lessonSessionRepository = mock(LessonSessionRepository.class);
        suggestionRepository = mock(SuggestionRepository.class);
        frameRepository = mock(BehaviorEvidenceFrameRepository.class);
        minioService = mock(MinioService.class);

        service = new ReportService(
                reportRepository, versionRepository, lessonSessionRepository,
                mock(CourseRepository.class), mock(ClassGroupRepository.class),
                mock(BehaviorObservationRepository.class),
                suggestionRepository, mock(AnalysisJobRepository.class),
                new SuggestionEngine(new ObjectMapper().findAndRegisterModules()),
                mock(StudentRepository.class), mock(LessonWindowFeatureRepository.class),
                new ObjectMapper().findAndRegisterModules());
        // 生产由 Spring 注入；这里显式注入，避免为两个依赖复制整份 13 参数构造器列表。
        service.setEvidenceDeletionDependencies(frameRepository, minioService);

        EvaluationReportEntity report = new EvaluationReportEntity();
        report.setId("report-1");
        report.setLessonSessionId(LESSON);
        report.setTeacherId("teacher-1");
        when(reportRepository.findByLessonSessionId(LESSON)).thenReturn(Optional.of(report));

        LessonSessionEntity session = new LessonSessionEntity();
        session.setId(LESSON);
        session.setTeacherId("teacher-1");
        when(lessonSessionRepository.findById(LESSON)).thenReturn(Optional.of(session));

        when(versionRepository.findByLessonSessionIdOrderByVersionDesc(LESSON)).thenReturn(List.of());
        when(suggestionRepository.findByLessonSessionIdAndStatus(LESSON, "pending")).thenReturn(List.of());
        when(frameRepository.findByLessonSessionIdOrderByWindowIndexAsc(LESSON)).thenReturn(List.of());

        serviceLogger = (Logger) LoggerFactory.getLogger(ReportService.class);
        logs = new ListAppender<>();
        logs.start();
        serviceLogger.addAppender(logs);
    }

    @AfterEach
    void tearDown() {
        serviceLogger.detachAppender(logs);
        logs.stop();
    }

    @Test
    void framesOfTheLessonAreDeletedTogetherWithTheirObjects() {
        BehaviorEvidenceFrameEntity frame = frame("frame-1", LESSON, "evidence/ls-1/w0.jpg");
        when(frameRepository.findByLessonSessionIdOrderByWindowIndexAsc(LESSON))
                .thenReturn(List.of(frame));
        when(minioService.removeObject("analysis-artifacts", "evidence/ls-1/w0.jpg"))
                .thenReturn(true);

        service.deleteReport(LESSON, "teacher-1");

        verify(minioService).removeObject("analysis-artifacts", "evidence/ls-1/w0.jpg");
        // 顺利删除时不该再 probe：那是每帧多付一次 stat，且样例里 147 帧会翻倍请求。
        verify(minioService, never()).probeObject(anyString(), anyString());
        verify(frameRepository).deleteById("frame-1");
        // 每帧一条结构化审计：运维要能从日志证明影像确实被删除。
        assertEquals(1, messagesWith("PRIVACY_DELETE_EVIDENCE_FRAME frameId=frame-1").size(),
                "每个被删的证据帧都要留一条 PRIVACY_DELETE 审计");
    }

    @Test
    void failedObjectRemovalKeepsRowSoRetentionCanRetry() {
        BehaviorEvidenceFrameEntity frame = frame("frame-1", LESSON, "evidence/ls-1/w0.jpg");
        when(frameRepository.findByLessonSessionIdOrderByWindowIndexAsc(LESSON))
                .thenReturn(List.of(frame));
        // 对象存在，但 removeObject 失败（权限/抖动）：行必须留下，下一轮再删。
        when(minioService.probeObject(anyString(), anyString()))
                .thenReturn(MinioService.ObjectProbe.found(1024, "etag"));
        when(minioService.removeObject("analysis-artifacts", "evidence/ls-1/w0.jpg"))
                .thenReturn(false);

        service.deleteReport(LESSON, "teacher-1");

        verify(frameRepository, never()).deleteById("frame-1");
        List<String> warnings = messagesWith("PRIVACY_DELETE_EVIDENCE_FRAME_FAILED frameId=frame-1");
        assertEquals(1, warnings.size(), "删除失败必须留下可重试的 WARN");
        assertTrue(warnings.getFirst().contains("retention retry"),
                "日志要说明这是留给保留期清理重试，而不是静默放弃");
    }

    @Test
    void objectThatIsExplicitlyMissingStillHasItsRowRemoved() {
        BehaviorEvidenceFrameEntity frame = frame("frame-1", LESSON, "evidence/ls-1/gone.jpg");
        when(frameRepository.findByLessonSessionIdOrderByWindowIndexAsc(LESSON))
                .thenReturn(List.of(frame));
        // removeObject 对不存在的对象也返回 false，所以这里补一次探测：存储明确回答
        // “不存在”（NoSuchKey），对象早已不在，行不应继续指向空气。
        when(minioService.removeObject("analysis-artifacts", "evidence/ls-1/gone.jpg"))
                .thenReturn(false);
        when(minioService.probeObject("analysis-artifacts", "evidence/ls-1/gone.jpg"))
                .thenReturn(MinioService.ObjectProbe.notFound("code=NoSuchKey"));

        service.deleteReport(LESSON, "teacher-1");

        verify(frameRepository).deleteById("frame-1");
        assertTrue(messagesWith("PRIVACY_DELETE_EVIDENCE_FRAME frameId=frame-1").getFirst()
                        .contains("alreadyMissing=true"),
                "审计要能区分“本来就没有”与“这次删掉了”");
    }

    @Test
    void probeErrorIsNotTreatedAsMissing() {
        BehaviorEvidenceFrameEntity frame = frame("frame-1", LESSON, "evidence/ls-1/w0.jpg");
        when(frameRepository.findByLessonSessionIdOrderByWindowIndexAsc(LESSON))
                .thenReturn(List.of(frame));
        // ERROR 只代表“无法判断”（403/超时/5xx）。把它当成不存在会删掉一行，
        // 而对象仍在桶里——那正是孤儿影像的来源。
        when(minioService.removeObject("analysis-artifacts", "evidence/ls-1/w0.jpg"))
                .thenReturn(false);
        when(minioService.probeObject("analysis-artifacts", "evidence/ls-1/w0.jpg"))
                .thenReturn(MinioService.ObjectProbe.error("AccessDenied", "stat failed: code=AccessDenied"));

        service.deleteReport(LESSON, "teacher-1");

        verify(frameRepository, never()).deleteById("frame-1");
        List<String> warnings = messagesWith("PRIVACY_DELETE_EVIDENCE_FRAME_FAILED frameId=frame-1");
        assertEquals(1, warnings.size());
        assertTrue(warnings.getFirst().contains("errorCode=AccessDenied"),
                "日志要带上错误码，运维才知道是权限还是网络");
    }

    @Test
    void framesOfADifferentLessonAreUntouched() {
        BehaviorEvidenceFrameEntity mine = frame("frame-1", LESSON, "evidence/ls-1/w0.jpg");
        BehaviorEvidenceFrameEntity other = frame("frame-2", "ls-2", "evidence/ls-2/w0.jpg");
        when(frameRepository.findByLessonSessionIdOrderByWindowIndexAsc(LESSON))
                .thenReturn(List.of(mine));
        when(frameRepository.findByLessonSessionIdOrderByWindowIndexAsc("ls-2"))
                .thenReturn(List.of(other));
        when(minioService.removeObject(anyString(), anyString())).thenReturn(true);

        service.deleteReport(LESSON, "teacher-1");

        verify(frameRepository).deleteById("frame-1");
        verify(frameRepository, never()).deleteById("frame-2");
        // 查询本身也必须按课堂收窄：跨课堂读会顺带泄露别的课堂的帧元数据。
        verify(frameRepository, never()).findByLessonSessionIdOrderByWindowIndexAsc("ls-2");
        verify(minioService, never()).removeObject("analysis-artifacts", "evidence/ls-2/w0.jpg");
    }

    @Test
    void reportVersionsSuggestionsAndReportRowAreStillDeleted() {
        // 无回归：新增的证据帧删除不能顺手改变原有的三步删除。
        EvaluationReportVersionEntity version = new EvaluationReportVersionEntity();
        version.setId("version-1");
        when(versionRepository.findByLessonSessionIdOrderByVersionDesc(LESSON))
                .thenReturn(List.of(version));
        SuggestionEntity suggestion = new SuggestionEntity();
        suggestion.setId("suggestion-1");
        when(suggestionRepository.findByLessonSessionIdAndStatus(LESSON, "pending"))
                .thenReturn(List.of(suggestion));

        service.deleteReport(LESSON, "teacher-1");

        verify(versionRepository).deleteAll(List.of(version));
        verify(suggestionRepository).deleteAll(List.of(suggestion));
        verify(reportRepository).delete(any(EvaluationReportEntity.class));
    }

    @Test
    void reportLevelPrivacyDeleteLineIsStillEmitted() {
        service.deleteReport(LESSON, "teacher-1");

        assertEquals(1, messagesWith("PRIVACY_DELETE reportId=report-1").size(),
                "报告本身的删除审计不能被新链路挤掉");
    }

    @Test
    void onlyFrameObjectKeysAreTouchedNeverAWholePrefix() {
        when(frameRepository.findByLessonSessionIdOrderByWindowIndexAsc(LESSON))
                .thenReturn(List.of(frame("frame-1", LESSON, "evidence/ls-1/w0.jpg")));
        when(minioService.removeObject(anyString(), anyString())).thenReturn(true);

        service.deleteReport(LESSON, "teacher-1");

        // analysis-results/ 的布局由 ai-service 掌握，Java 侧只能删证据帧登记过的确切对象；
        // 一旦退化成「按前缀列举再清空」，就会误删仍被结果恢复器依赖的 result/envelope。
        verify(minioService, never()).listObjects(anyString(), anyString());
    }

    private List<String> messagesWith(String prefix) {
        return logs.list.stream()
                .filter(e -> e.getLevel().isGreaterOrEqual(Level.INFO))
                .map(ILoggingEvent::getFormattedMessage)
                .filter(m -> m.startsWith(prefix))
                .toList();
    }

    private BehaviorEvidenceFrameEntity frame(String id, String lessonSessionId, String objectKey) {
        BehaviorEvidenceFrameEntity frame = new BehaviorEvidenceFrameEntity();
        frame.setId(id);
        frame.setLessonSessionId(lessonSessionId);
        frame.setBucket("analysis-artifacts");
        frame.setJobId("job-1");
        frame.setWindowIndex(0);
        frame.setObjectKey(objectKey);
        frame.setCode("panorama");
        return frame;
    }
}
