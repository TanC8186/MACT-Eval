package com.macteval.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.macteval.model.entity.AnalysisJobEntity;
import com.macteval.repository.AnalysisJobRepository;
import com.macteval.repository.TaskOutboxRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.data.redis.core.RedisTemplate;

import java.util.List;
import java.util.Map;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 业务落库失败与补偿（独立审查 A 的 F2 / F5）。
 *
 * <p>覆盖四件事：</p>
 * <ol>
 *   <li>ingest 抛异常 → PARTIAL + ingest_state=PENDING + next_recovery_at（可被补偿器扫描）；</li>
 *   <li>{@code reingestStoredResult} 只处理 ingest_state=PENDING 且原始结果 succeeded 的行，
 *       成功后推进为 SUCCEEDED 并清空错误；连续失败按上限转 FAILED；不适用状态不改任何字段；</li>
 *   <li>AI 合法返回的 {@code partial/NO_WINDOWS} 不欠重放：状态、错误原因、ingest_state 都不变
 *       （R01；此前 PARTIAL 一律被补偿成 SUCCEEDED）；</li>
 *   <li>完成回调缺 {@code status} 时不得默认成功（fail-open）。</li>
 * </ol>
 */
class IngestRecoveryTest {

    private AnalysisJobRepository jobRepository;
    private BehaviorService behaviorService;
    private TranscriptService transcriptService;
    private WindowAlignmentService windowAlignmentService;
    private AnalysisJobService service;

    @BeforeEach
    void setUp() {
        jobRepository = mock(AnalysisJobRepository.class);
        RedisTemplate<String, String> redisTemplate = mock(RedisTemplate.class);
        behaviorService = mock(BehaviorService.class);
        transcriptService = mock(TranscriptService.class);
        windowAlignmentService = mock(WindowAlignmentService.class);
        service = new AnalysisJobService(jobRepository, mock(TaskOutboxRepository.class),
                redisTemplate, new ObjectMapper(), behaviorService, transcriptService,
                windowAlignmentService);
    }

    private AnalysisJobEntity job(String id, AnalysisJobEntity.JobStatus status, int attempt) {
        AnalysisJobEntity job = new AnalysisJobEntity();
        job.setId(id);
        job.setType(AnalysisJobEntity.JobType.VIDEO_ANALYSIS);
        job.setStatus(status);
        job.setAttempt(attempt);
        job.setParams("{\"lessonSessionId\":\"lesson-1\"}");
        job.setResultMetadata("{\"status\":\"succeeded\",\"observations\":[]}");
        return job;
    }

    // ------------------------------------------------------------ F2: ingest 失败可补偿

    @Test
    void ingestFailureLeavesPartialWithRecoveryMarker() {
        AnalysisJobEntity job = job("job-ingest-fail", AnalysisJobEntity.JobStatus.RUNNING, 1);
        when(jobRepository.findByIdForUpdate("job-ingest-fail")).thenReturn(Optional.of(job));
        when(jobRepository.save(any())).thenAnswer(invocation -> invocation.getArgument(0));
        doThrow(new IllegalStateException("CannotAcquireLockException"))
                .when(behaviorService).ingestCompletedVideoJob(any());

        Map<String, Object> outcome = service.applyResult("job-ingest-fail", 1,
                Map.of("status", "succeeded", "observations", List.of()));

        assertEquals(true, outcome.get("applied"), "结果本身已持久化，回调应被视为已应用");
        assertEquals(AnalysisJobEntity.JobStatus.PARTIAL, job.getStatus());
        assertNotNull(job.getNextRecoveryAt(), "必须写入补偿时间，否则没有任何路径会重放 ingest");
        assertTrue(job.getErrorMessage().contains("结果落库失败"));
        // 对齐在 ingest 失败后不应发生（此时没有窗口数据可对齐）。
        verify(windowAlignmentService, never()).alignLesson(anyString(), any(), any());
    }

    /**
     * 原用例直接让 PARTIAL 行进入补偿；R01 之后 PARTIAL 不再等于「可补偿」，
     * 必须显式带 ingest_state=PENDING（即“原始 succeeded、业务落库失败”）。
     * 断言本身（成功后推进 SUCCEEDED、清空错误）不变，只是补上前置阶段标志。
     */
    @Test
    void reingestSuccessAdvancesToSucceededAndClearsError() {
        AnalysisJobEntity job = job("job-partial-ok", AnalysisJobEntity.JobStatus.PARTIAL, 1);
        job.setErrorMessage("结果落库失败: boom");
        job.setRecoveryCount(1);
        job.setIngestState(AnalysisJobService.INGEST_PENDING);
        when(jobRepository.findByIdForUpdate("job-partial-ok")).thenReturn(Optional.of(job));
        when(jobRepository.save(any())).thenAnswer(invocation -> invocation.getArgument(0));

        AnalysisJobService.ReingestOutcome outcome = service.reingestStoredResult("job-partial-ok", 3, 60);

        assertEquals(AnalysisJobService.ReingestOutcome.REINGESTED, outcome);
        assertEquals(AnalysisJobEntity.JobStatus.SUCCEEDED, job.getStatus());
        assertEquals(AnalysisJobService.INGEST_INGESTED, job.getIngestState());
        assertEquals(AnalysisJobService.ALIGNMENT_DONE, job.getAlignmentState());
        assertNull(job.getErrorMessage());
        assertNull(job.getNextRecoveryAt());
        verify(behaviorService, times(1)).ingestCompletedVideoJob(job);
        verify(windowAlignmentService, times(1)).alignLesson("lesson-1", job.getId(), null);
    }

    @Test
    void reingestFailureBacksOffThenExhausts() {
        AnalysisJobEntity job = job("job-partial-retry", AnalysisJobEntity.JobStatus.PARTIAL, 1);
        job.setRecoveryCount(1);
        job.setIngestState(AnalysisJobService.INGEST_PENDING);
        when(jobRepository.findByIdForUpdate("job-partial-retry")).thenReturn(Optional.of(job));
        when(jobRepository.save(any())).thenAnswer(invocation -> invocation.getArgument(0));
        doThrow(new IllegalStateException("still locked"))
                .when(behaviorService).ingestCompletedVideoJob(any());

        AnalysisJobService.ReingestOutcome first = service.reingestStoredResult("job-partial-retry", 3, 60);

        assertEquals(AnalysisJobService.ReingestOutcome.FAILED, first);
        assertEquals(AnalysisJobEntity.JobStatus.PARTIAL, job.getStatus());
        assertEquals(2, job.getRecoveryCount());
        assertNotNull(job.getNextRecoveryAt());

        job.setRecoveryCount(2);
        AnalysisJobService.ReingestOutcome second = service.reingestStoredResult("job-partial-retry", 3, 60);

        assertEquals(AnalysisJobService.ReingestOutcome.EXHAUSTED, second);
        assertEquals(AnalysisJobEntity.JobStatus.FAILED, job.getStatus());
        assertEquals(AnalysisJobService.INGEST_EXHAUSTED, job.getIngestState());
        assertNull(job.getNextRecoveryAt());
        assertTrue(job.getErrorMessage().contains("ingest recovery exhausted"));
    }

    @Test
    void reingestIsNotApplicableForOtherStates() {
        AnalysisJobEntity succeeded = job("job-succeeded", AnalysisJobEntity.JobStatus.SUCCEEDED, 1);
        when(jobRepository.findByIdForUpdate("job-succeeded")).thenReturn(Optional.of(succeeded));

        AnalysisJobEntity noResult = job("job-no-result", AnalysisJobEntity.JobStatus.PARTIAL, 1);
        noResult.setResultMetadata("  ");
        when(jobRepository.findByIdForUpdate("job-no-result")).thenReturn(Optional.of(noResult));

        assertEquals(AnalysisJobService.ReingestOutcome.NOT_APPLICABLE,
                service.reingestStoredResult("job-succeeded", 3, 60));
        assertEquals(AnalysisJobService.ReingestOutcome.NOT_APPLICABLE,
                service.reingestStoredResult("job-no-result", 3, 60));
        verify(behaviorService, never()).ingestCompletedVideoJob(any());
        verify(jobRepository, never()).save(any());
    }

    // -------------------------------------------------- R01: 合法 PARTIAL 不得被补偿

    @Test
    void legalPartialNoWindowsKeepsSemanticsAndIsNeverPromoted() {
        AnalysisJobEntity job = job("job-no-windows", AnalysisJobEntity.JobStatus.RUNNING, 1);
        job.setResultMetadata("{\"status\":\"partial\",\"error\":\"NO_WINDOWS\"}");
        when(jobRepository.findByIdForUpdate("job-no-windows")).thenReturn(Optional.of(job));
        when(jobRepository.save(any())).thenAnswer(invocation -> invocation.getArgument(0));

        Map<String, Object> outcome = service.applyResult("job-no-windows", 1,
                Map.of("status", "partial", "error", "NO_WINDOWS"));

        assertEquals(true, outcome.get("applied"));
        assertEquals(AnalysisJobEntity.JobStatus.PARTIAL, job.getStatus());
        assertEquals("NO_WINDOWS", job.getErrorMessage(), "NO_WINDOWS 原因必须原样保留");
        assertNull(job.getIngestState(), "合法 partial 不进入落库补偿阶段");
        assertNull(job.getNextRecoveryAt(), "合法 partial 不写补偿时间，否则会被扫描器捞起");
        // 既没有观测落库，也没有可对齐的数据。
        verify(behaviorService, never()).ingestCompletedVideoJob(any());
        verify(windowAlignmentService, never()).alignLesson(anyString(), any(), any());

        AnalysisJobService.ReingestOutcome reingest =
                service.reingestStoredResult("job-no-windows", 3, 60);
        assertEquals(AnalysisJobService.ReingestOutcome.NOT_APPLICABLE, reingest);
        assertEquals(AnalysisJobEntity.JobStatus.PARTIAL, job.getStatus(),
                "补偿路径绝不能把合法 partial 升成 SUCCEEDED");
        assertEquals("NO_WINDOWS", job.getErrorMessage());
    }

    @Test
    void partialWithoutIngestStateIsNotSelectedEvenWithSucceededMetadata() {
        // 历史/异常数据：状态 PARTIAL 且有 succeeded 结果，但没有 ingest_state。
        // 没有阶段标志就不能断定“欠落库”，一律不处理、不改字段。
        AnalysisJobEntity job = job("job-no-stage", AnalysisJobEntity.JobStatus.PARTIAL, 1);
        when(jobRepository.findByIdForUpdate("job-no-stage")).thenReturn(Optional.of(job));

        assertEquals(AnalysisJobService.ReingestOutcome.NOT_APPLICABLE,
                service.reingestStoredResult("job-no-stage", 3, 60));
        assertEquals(AnalysisJobEntity.JobStatus.PARTIAL, job.getStatus());
        assertNull(job.getIngestState());
        verify(behaviorService, never()).ingestCompletedVideoJob(any());
        verify(jobRepository, never()).save(any());
    }

    @Test
    void pendingIngestWithNonSucceededStoredResultIsRefused() {
        // 防御性校验：即使被标成 PENDING，结果本身是 partial 也不能重放。
        AnalysisJobEntity job = job("job-lying-stage", AnalysisJobEntity.JobStatus.PARTIAL, 1);
        job.setIngestState(AnalysisJobService.INGEST_PENDING);
        job.setResultMetadata("{\"status\":\"partial\",\"error\":\"NO_WINDOWS\"}");
        when(jobRepository.findByIdForUpdate("job-lying-stage")).thenReturn(Optional.of(job));

        assertEquals(AnalysisJobService.ReingestOutcome.NOT_APPLICABLE,
                service.reingestStoredResult("job-lying-stage", 3, 60));
        assertEquals(AnalysisJobEntity.JobStatus.PARTIAL, job.getStatus());
        assertEquals(AnalysisJobService.INGEST_PENDING, job.getIngestState());
        assertEquals("{\"status\":\"partial\",\"error\":\"NO_WINDOWS\"}", job.getResultMetadata());
        verify(behaviorService, never()).ingestCompletedVideoJob(any());
        verify(jobRepository, never()).save(any());
    }

    @Test
    void succeededWithIngestFailureBecomesPendingThenRecovers() {
        // 真正的「succeeded + ingest 失败」必须可补偿：PENDING → 重放 → SUCCEEDED/INGESTED。
        AnalysisJobEntity job = job("job-recoverable", AnalysisJobEntity.JobStatus.RUNNING, 1);
        when(jobRepository.findByIdForUpdate("job-recoverable")).thenReturn(Optional.of(job));
        when(jobRepository.save(any())).thenAnswer(invocation -> invocation.getArgument(0));
        doThrow(new IllegalStateException("lock timeout"))
                .doNothing()
                .when(behaviorService).ingestCompletedVideoJob(any());

        service.applyResult("job-recoverable", 1,
                Map.of("status", "succeeded", "observations", List.of()));

        assertEquals(AnalysisJobEntity.JobStatus.PARTIAL, job.getStatus());
        assertEquals(AnalysisJobService.INGEST_PENDING, job.getIngestState());
        assertNotNull(job.getNextRecoveryAt());

        AnalysisJobService.ReingestOutcome outcome =
                service.reingestStoredResult("job-recoverable", 3, 60);

        assertEquals(AnalysisJobService.ReingestOutcome.REINGESTED, outcome);
        assertEquals(AnalysisJobEntity.JobStatus.SUCCEEDED, job.getStatus());
        assertEquals(AnalysisJobService.INGEST_INGESTED, job.getIngestState());
        assertNull(job.getNextRecoveryAt());
    }

    // -------------------------------------------------- R03: 对齐失败可发现、可补偿

    @Test
    void alignmentFailureKeepsJobSucceededButMarksAlignmentPending() {
        AnalysisJobEntity job = job("job-align-pending", AnalysisJobEntity.JobStatus.RUNNING, 1);
        when(jobRepository.findByIdForUpdate("job-align-pending")).thenReturn(Optional.of(job));
        when(jobRepository.save(any())).thenAnswer(invocation -> invocation.getArgument(0));
        doThrow(new IllegalStateException("db deadlock on window insert"))
                .when(windowAlignmentService).alignLesson(anyString(), any(), any());

        service.applyResult("job-align-pending", 1, Map.of("status", "succeeded"));

        // 观测已落库 → 任务保持 SUCCEEDED（报告要引用它的 analysisVideoJobId）。
        assertEquals(AnalysisJobEntity.JobStatus.SUCCEEDED, job.getStatus());
        assertEquals(AnalysisJobService.INGEST_INGESTED, job.getIngestState(),
                "观测落库成功，不能因为对齐失败退回落库补偿");
        assertEquals(AnalysisJobService.ALIGNMENT_PENDING, job.getAlignmentState());
        assertNotNull(job.getNextRecoveryAt(), "必须写入对齐补偿时间，否则恢复器发现不了");
        assertTrue(job.getErrorMessage().contains("db deadlock on window insert"),
                "真实原因必须写入 errorMessage: " + job.getErrorMessage());
        verify(behaviorService, times(1)).ingestCompletedVideoJob(job);
    }

    @Test
    void realignAfterTransientFailureOnlyRebuildsWindows() {
        // 第一次对齐抛异常、第二次成功：中间只重建派生窗口，从不重新推理。
        AnalysisJobEntity job = job("job-realign", AnalysisJobEntity.JobStatus.SUCCEEDED, 1);
        job.setIngestState(AnalysisJobService.INGEST_INGESTED);
        job.setAlignmentState(AnalysisJobService.ALIGNMENT_PENDING);
        job.setAlignmentAttempts(0);
        when(jobRepository.findByIdForUpdate("job-realign")).thenReturn(Optional.of(job));
        when(jobRepository.save(any())).thenAnswer(invocation -> invocation.getArgument(0));
        doThrow(new IllegalStateException("transient"))
                .doNothing()
                .when(windowAlignmentService).alignLesson(anyString(), any(), any());

        AnalysisJobService.RealignOutcome first = service.realignStoredResult("job-realign", 3, 60);

        assertEquals(AnalysisJobService.RealignOutcome.FAILED, first);
        assertEquals(AnalysisJobService.ALIGNMENT_PENDING, job.getAlignmentState(),
                "首次失败仍应保持待补偿，下一轮继续");
        assertEquals(1, job.getAlignmentAttempts());
        assertNotNull(job.getNextRecoveryAt(), "失败后必须按退避设置下次重试时间");

        AnalysisJobService.RealignOutcome second = service.realignStoredResult("job-realign", 3, 60);

        assertEquals(AnalysisJobService.RealignOutcome.REALIGNED, second);
        assertEquals(AnalysisJobService.ALIGNMENT_DONE, job.getAlignmentState());
        assertNull(job.getNextRecoveryAt());
        assertEquals(2, job.getAlignmentAttempts());
        // 两次补偿都只重建派生窗口：不重新推理、不重放观测落库。
        verify(windowAlignmentService, times(2)).alignLesson("lesson-1", job.getId(), null);
        verify(behaviorService, never()).ingestCompletedVideoJob(any());
        verify(transcriptService, never()).ingestCompletedAudioJob(any());
    }

    @Test
    void realignExhaustionRecordsRealReason() {
        AnalysisJobEntity job = job("job-realign-dead", AnalysisJobEntity.JobStatus.SUCCEEDED, 1);
        job.setIngestState(AnalysisJobService.INGEST_INGESTED);
        job.setAlignmentState(AnalysisJobService.ALIGNMENT_PENDING);
        job.setAlignmentAttempts(2);
        when(jobRepository.findByIdForUpdate("job-realign-dead")).thenReturn(Optional.of(job));
        when(jobRepository.save(any())).thenAnswer(invocation -> invocation.getArgument(0));
        doThrow(new IllegalStateException("relation lesson_window_features is locked"))
                .when(windowAlignmentService).alignLesson(anyString(), any(), any());

        AnalysisJobService.RealignOutcome outcome = service.realignStoredResult("job-realign-dead", 3, 60);

        assertEquals(AnalysisJobService.RealignOutcome.EXHAUSTED, outcome);
        assertEquals(AnalysisJobService.ALIGNMENT_FAILED, job.getAlignmentState());
        assertNull(job.getNextRecoveryAt());
        assertEquals(3, job.getAlignmentAttempts());
        assertTrue(job.getErrorMessage().contains("relation lesson_window_features is locked"),
                "耗尽时必须写入真实原因: " + job.getErrorMessage());
        verify(behaviorService, never()).ingestCompletedVideoJob(any());
        verify(transcriptService, never()).ingestCompletedAudioJob(any());
    }

    @Test
    void realignIsNotApplicableUnlessAlignmentIsPending() {
        AnalysisJobEntity succeeded = job("job-align-done", AnalysisJobEntity.JobStatus.SUCCEEDED, 1);
        succeeded.setAlignmentState(AnalysisJobService.ALIGNMENT_DONE);
        when(jobRepository.findByIdForUpdate("job-align-done")).thenReturn(Optional.of(succeeded));

        assertEquals(AnalysisJobService.RealignOutcome.NOT_APPLICABLE,
                service.realignStoredResult("job-align-done", 3, 60));
        verify(windowAlignmentService, never()).alignLesson(anyString(), any(), any());
        verify(jobRepository, never()).save(any());
    }

    // ------------------------------------------------------------ F5: 缺 status 不得默认成功

    @Test
    void missingStatusWithoutErrorIsRejected() {
        AnalysisJobEntity job = job("job-no-status", AnalysisJobEntity.JobStatus.RUNNING, 1);
        when(jobRepository.findByIdForUpdate("job-no-status")).thenReturn(Optional.of(job));

        Map<String, Object> outcome = service.completeJob("job-no-status", Map.of());

        assertEquals(false, outcome.get("applied"));
        assertEquals("invalid_status", outcome.get("reason"));
        assertEquals(AnalysisJobEntity.JobStatus.RUNNING, job.getStatus(), "不得推进状态");
        verify(behaviorService, never()).ingestCompletedVideoJob(any());
        verify(jobRepository, never()).save(any());
    }

    @Test
    void missingStatusWithErrorMarksFailed() {
        AnalysisJobEntity job = job("job-error-only", AnalysisJobEntity.JobStatus.RUNNING, 1);
        when(jobRepository.findByIdForUpdate("job-error-only")).thenReturn(Optional.of(job));
        when(jobRepository.save(any())).thenAnswer(invocation -> invocation.getArgument(0));

        Map<String, Object> outcome = service.completeJob("job-error-only",
                Map.of("error", "worker crashed"));

        assertEquals(true, outcome.get("applied"));
        assertEquals(AnalysisJobEntity.JobStatus.FAILED, job.getStatus());
        assertEquals("worker crashed", job.getErrorMessage());
        verify(behaviorService, never()).ingestCompletedVideoJob(any());
    }
}
