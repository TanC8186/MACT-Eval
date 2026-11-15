package com.macteval.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.macteval.model.entity.AnalysisJobEntity;
import com.macteval.model.entity.TaskOutboxEntity;
import com.macteval.repository.AnalysisJobRepository;
import com.macteval.repository.TaskOutboxRepository;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.springframework.data.domain.Pageable;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.TransactionStatus;
import org.springframework.transaction.support.SimpleTransactionStatus;
import org.springframework.transaction.support.TransactionSynchronizationManager;

import java.time.Clock;
import java.time.Instant;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

/**
 * 数据库驱动的 RUNNING 回收器（协议 §5）。
 *
 * <p>覆盖：FOUND 交付、NOT_FOUND 重排与上限、ERROR 延期不重排、INVALID 转 FAILED、
 * 条件更新竞争（0 行）、有效租约不进候选、关闭开关、告警去重；
 * 以及“外部 I/O 不在写事务内”的事务边界断言。</p>
 */
class JobRecoveryServiceTest {

    private static final Instant FIXED_INSTANT = Instant.parse("2026-10-26T10:00:00Z");
    private static final Clock FIXED_CLOCK = Clock.fixed(FIXED_INSTANT, ZoneOffset.UTC);
    private static final LocalDateTime NOW = LocalDateTime.ofInstant(FIXED_INSTANT, ZoneOffset.UTC);
    private static final long GRACE = 60;
    private static final long BACKOFF = 120;
    private static final long ALERT_AFTER = 900;

    private AnalysisJobRepository jobRepository;
    private TaskOutboxRepository outboxRepository;
    private JobResultStore resultStore;
    private AnalysisJobService analysisJobService;
    private RecordingTransactionManager txManager;
    private JobRecoveryService service;

    /**
     * 记录事务边界，并在事务活动期间设置同步标志。
     *
     * <p>有了它，测试可以在 MinIO 读取（外部 I/O）里断言
     * {@link TransactionSynchronizationManager#isActualTransactionActive()} 为 false，
     * 在条件状态更新里断言为 true——即“外部 I/O 在事务外”的真实断言。</p>
     */
    static final class RecordingTransactionManager implements PlatformTransactionManager {
        final List<String> events = new ArrayList<>();

        @Override
        public TransactionStatus getTransaction(TransactionDefinition definition) {
            events.add("begin");
            TransactionSynchronizationManager.setActualTransactionActive(true);
            return new SimpleTransactionStatus();
        }

        @Override
        public void commit(TransactionStatus status) {
            events.add("commit");
            TransactionSynchronizationManager.setActualTransactionActive(false);
        }

        @Override
        public void rollback(TransactionStatus status) {
            events.add("rollback");
            TransactionSynchronizationManager.setActualTransactionActive(false);
        }
    }

    @BeforeEach
    void setUp() {
        jobRepository = mock(AnalysisJobRepository.class);
        outboxRepository = mock(TaskOutboxRepository.class);
        resultStore = mock(JobResultStore.class);
        analysisJobService = mock(AnalysisJobService.class);
        txManager = new RecordingTransactionManager();
        service = new JobRecoveryService(jobRepository, outboxRepository, resultStore,
                analysisJobService, new ObjectMapper(), txManager, FIXED_CLOCK);
        service.configureForTest(true, 20, GRACE, 3, BACKOFF, ALERT_AFTER);
    }

    @AfterEach
    void tearDown() {
        TransactionSynchronizationManager.setActualTransactionActive(false);
    }

    private AnalysisJobEntity runningJob(String id, int attempt, int recoveryCount,
                                         LocalDateTime leaseUntil) {
        AnalysisJobEntity job = new AnalysisJobEntity();
        job.setId(id);
        job.setType(AnalysisJobEntity.JobType.VIDEO_ANALYSIS);
        job.setPriority(AnalysisJobEntity.JobPriority.NORMAL);
        job.setStatus(AnalysisJobEntity.JobStatus.RUNNING);
        job.setAttempt(attempt);
        job.setRecoveryCount(recoveryCount);
        job.setLeaseUntil(leaseUntil);
        job.setStartedAt(NOW.minusHours(1));
        job.setParams("{\"lessonSessionId\":\"lesson-1\",\"objectName\":\"v.mp4\"}");
        return job;
    }

    private void givenCandidates(AnalysisJobEntity... jobs) {
        when(jobRepository.findRecoverable(any(), any(), any(Pageable.class)))
                .thenReturn(List.of(jobs));
    }

    /** 读取发生在任何事务之外；返回给定的判定。 */
    private void givenReadResult(int attempt, JobResultStore.Status status, String jobId) {
        JobResultStore.ResultRead read;
        switch (status) {
            case FOUND -> {
                Map<String, Object> result = new LinkedHashMap<>();
                result.put("status", "succeeded");
                JobResultStore.ResultRef ref = new JobResultStore.ResultRef(jobId, attempt,
                        "analysis-artifacts", "analysis-results/" + jobId + "/" + attempt + "/result.json",
                        "analysis-results/" + jobId + "/" + attempt + "/envelope.json",
                        "a".repeat(64), 42);
                read = new JobResultStore.ResultRead(JobResultStore.Status.FOUND, result, ref, "ok");
            }
            case NOT_FOUND -> read = new JobResultStore.ResultRead(
                    JobResultStore.Status.NOT_FOUND, null, null, "no result object");
            case INVALID -> read = new JobResultStore.ResultRead(
                    JobResultStore.Status.INVALID, null, null, "result sha256 mismatch");
            default -> read = new JobResultStore.ResultRead(
                    JobResultStore.Status.ERROR, null, null, "envelope probe failed: AccessDenied");
        }
        when(resultStore.read(anyString(), anyInt())).thenAnswer(invocation -> {
            assertFalse(TransactionSynchronizationManager.isActualTransactionActive(),
                    "结果读取（外部 I/O）必须在事务之外");
            assertTrue(txManager.events.isEmpty(),
                    "结果读取必须先于任何写事务开始: " + txManager.events);
            return read;
        });
    }

    // ---------------------------------------------------------------- FOUND

    @Test
    void foundResultIsDeliveredThroughApplyResultWithoutRequeue() {
        String jobId = "job-found";
        AnalysisJobEntity job = runningJob(jobId, 2, 0, NOW.minusMinutes(10));
        givenCandidates(job);
        givenReadResult(2, JobResultStore.Status.FOUND, jobId);
        when(analysisJobService.applyResult(eq(jobId), eq(2), anyMap())).thenAnswer(invocation -> {
            // applyResult 自带 @Transactional（AnalysisJobService），但调用点本身
            // 不能在回收器的写事务里：这里断言调用时没有回收器事务在活动。
            assertFalse(TransactionSynchronizationManager.isActualTransactionActive(),
                    "applyResult 不得被包在回收器的写事务里");
            return Map.of("applied", true, "status", "SUCCEEDED", "attempt", 2);
        });

        AnalysisJobEntity persisted = runningJob(jobId, 2, 0, null);
        persisted.setStatus(AnalysisJobEntity.JobStatus.SUCCEEDED);
        when(jobRepository.findByIdForUpdate(jobId)).thenReturn(Optional.of(persisted));

        JobRecoveryService.RecoveryStats stats = service.runOnce();

        assertEquals(1, stats.completed(), stats.details().toString());
        assertEquals(0, stats.requeued());
        verify(analysisJobService, times(1)).applyResult(eq(jobId), eq(2), anyMap());
        verify(jobRepository, never()).requeueExpired(anyString(), anyInt(), any(), any());
        verify(outboxRepository, never()).save(any());
        // 审计字段只记录引用，不含签名 URL。
        assertNotNull(persisted.getLastResultRef());
        assertFalse(persisted.getLastResultRef().contains("http"));
        assertTrue(persisted.getLastResultRef().contains("a".repeat(64)));
    }

    @Test
    void foundResultRejectedAsStaleAttemptIsSkippedWithoutStateChange() {
        String jobId = "job-stale";
        AnalysisJobEntity job = runningJob(jobId, 1, 0, NOW.minusMinutes(10));
        givenCandidates(job);
        givenReadResult(1, JobResultStore.Status.FOUND, jobId);
        when(analysisJobService.applyResult(anyString(), anyInt(), anyMap()))
                .thenReturn(Map.of("applied", false, "reason", "stale_attempt"));

        JobRecoveryService.RecoveryStats stats = service.runOnce();

        assertEquals(1, stats.skipped());
        assertEquals(0, stats.completed());
        verify(jobRepository, never()).save(any());
        verify(jobRepository, never()).requeueExpired(anyString(), anyInt(), any(), any());
    }

    // ------------------------------------------------------------ NOT_FOUND

    @Test
    void missingResultIsRequeuedWithOutboxResetAndAttemptUnchanged() {
        String jobId = "job-requeue";
        AnalysisJobEntity job = runningJob(jobId, 4, 1, NOW.minusMinutes(10));
        givenCandidates(job);
        givenReadResult(4, JobResultStore.Status.NOT_FOUND, jobId);

        TaskOutboxEntity event = new TaskOutboxEntity();
        event.setId("outbox-1");
        event.setAggregateId(jobId);
        event.setEventType("ANALYSIS_JOB_QUEUED");
        event.setPayload("{\"id\":\"" + jobId + "\"}");
        event.setAttempt(2);
        event.setPublishedAt(NOW.minusMinutes(30));
        when(outboxRepository.findByAggregateIdOrderByCreatedAtAsc(jobId)).thenReturn(List.of(event));

        AnalysisJobEntity requeued = runningJob(jobId, 4, 1, null);
        requeued.setStatus(AnalysisJobEntity.JobStatus.QUEUED);
        when(jobRepository.requeueExpired(eq(jobId), eq(4), any(), any())).thenReturn(1);
        when(jobRepository.findByIdForUpdate(jobId)).thenReturn(Optional.of(requeued));
        when(jobRepository.save(any(AnalysisJobEntity.class))).thenAnswer(i -> i.getArgument(0));
        when(outboxRepository.save(any(TaskOutboxEntity.class))).thenAnswer(i -> i.getArgument(0));

        JobRecoveryService.RecoveryStats stats = service.runOnce();

        assertEquals(1, stats.requeued(), stats.details().toString());
        verify(jobRepository, times(1)).requeueExpired(eq(jobId), eq(4), any(), any());
        // 重排不改变 attempt（协议 §1：只有 claim 才 +1）。
        assertEquals(4, requeued.getAttempt());
        verify(jobRepository, never()).save(argThat(j -> j != null && j.getAttempt() != 4));
        // 同一事务内重置 outbox。
        assertNull(event.getPublishedAt());
        assertEquals(0, event.getAttempt());
        assertEquals(NOW, event.getNextRetryAt());
        verify(outboxRepository, times(1)).save(event);
        // recovery_count 累加。
        assertEquals(2, requeued.getRecoveryCount());
        // 事务边界：读结果在事务外（上面的断言），随后才有 begin/commit。
        assertEquals(List.of("begin", "commit"), txManager.events);
    }

    /**
     * V33 park 守卫：被消息修复迁移判为"不可发送"的事件不得被自动重排重新武装。
     *
     * <p>否则一次租约过期就把坏 payload 复位成 attempt=0 / next_retry_at=now，
     * 重新进入投递集合 —— V33 想消灭的无限退避循环会原样复活。</p>
     */
    @Test
    void parkedOutboxEventIsNotRearmedByRecovery() {
        String jobId = "job-parked";
        AnalysisJobEntity job = runningJob(jobId, 2, 1, NOW.minusMinutes(10));
        givenCandidates(job);
        givenReadResult(2, JobResultStore.Status.NOT_FOUND, jobId);

        TaskOutboxEntity parked = new TaskOutboxEntity();
        parked.setId("outbox-parked");
        parked.setAggregateId(jobId);
        parked.setEventType("ANALYSIS_JOB_QUEUED");
        parked.setPayload("[1,2]");
        parked.setAttempt(0);
        parked.setPublishedAt(null);
        parked.setNextRetryAt(NOW.plusYears(100));
        parked.setLastError("repair_failed: params is array");
        when(outboxRepository.findByAggregateIdOrderByCreatedAtAsc(jobId))
                .thenReturn(List.of(parked));

        AnalysisJobEntity requeued = runningJob(jobId, 2, 1, null);
        requeued.setStatus(AnalysisJobEntity.JobStatus.QUEUED);
        when(jobRepository.requeueExpired(eq(jobId), eq(2), any(), any())).thenReturn(1);
        when(jobRepository.findByIdForUpdate(jobId)).thenReturn(Optional.of(requeued));
        when(jobRepository.save(any(AnalysisJobEntity.class))).thenAnswer(i -> i.getArgument(0));

        JobRecoveryService.RecoveryStats stats = service.runOnce();

        assertEquals(1, stats.requeued(), stats.details().toString());
        // 事件必须原封不动：仍被 park，且没有被重新打开投递。
        assertEquals("repair_failed: params is array", parked.getLastError(),
                "parked 事件的 last_error 不得被改写成 requeued by recovery");
        assertNull(parked.getPublishedAt());
        assertTrue(parked.getNextRetryAt().isAfter(NOW.plusYears(50)),
                "next_retry_at 必须仍在极远未来：" + parked.getNextRetryAt());
        // 也不得因为"没有可重发事件"而补插一条新事件（那等于重新投递坏任务）。
        verify(outboxRepository, never()).save(any(TaskOutboxEntity.class));
    }

    @Test
    void parkedEventDetectionUsesTheRepairPrefix() {
        TaskOutboxEntity parked = new TaskOutboxEntity();
        parked.setLastError("repair_failed: params is number");
        assertTrue(JobRecoveryService.isParkedByRepair(parked));

        TaskOutboxEntity requeued = new TaskOutboxEntity();
        requeued.setLastError("requeued by recovery");
        assertFalse(JobRecoveryService.isParkedByRepair(requeued));

        TaskOutboxEntity untouched = new TaskOutboxEntity();
        untouched.setLastError(null);
        assertFalse(JobRecoveryService.isParkedByRepair(untouched));

        assertFalse(JobRecoveryService.isParkedByRepair(null));
    }

    @Test
    void missingResultInsertsOutboxEventWhenNoneExists() {
        String jobId = "job-no-outbox";
        AnalysisJobEntity job = runningJob(jobId, 1, 0, NOW.minusMinutes(10));
        givenCandidates(job);
        givenReadResult(1, JobResultStore.Status.NOT_FOUND, jobId);
        when(outboxRepository.findByAggregateIdOrderByCreatedAtAsc(jobId)).thenReturn(List.of());

        AnalysisJobEntity requeued = runningJob(jobId, 1, 0, null);
        requeued.setStatus(AnalysisJobEntity.JobStatus.QUEUED);
        when(jobRepository.requeueExpired(eq(jobId), eq(1), any(), any())).thenReturn(1);
        when(jobRepository.findByIdForUpdate(jobId)).thenReturn(Optional.of(requeued));
        when(jobRepository.save(any(AnalysisJobEntity.class))).thenAnswer(i -> i.getArgument(0));
        ArgumentCaptor<TaskOutboxEntity> captor = ArgumentCaptor.forClass(TaskOutboxEntity.class);
        when(outboxRepository.save(captor.capture())).thenAnswer(i -> i.getArgument(0));

        service.runOnce();

        assertEquals(1, captor.getAllValues().size());
        TaskOutboxEntity created = captor.getValue();
        assertEquals("ANALYSIS_JOB_QUEUED", created.getEventType());
        assertEquals(jobId, created.getAggregateId());
        assertTrue(created.getPayload().contains("\"id\":\"" + jobId + "\""));
        assertTrue(created.getPayload().contains("VIDEO_ANALYSIS"));
        assertTrue(created.getPayload().contains("lesson-1"), created.getPayload());
        assertEquals(0, created.getAttempt());
        assertNull(created.getPublishedAt());
    }

    @Test
    void exhaustedRecoveryAttemptsMarksFailedWithoutRequeue() {
        String jobId = "job-exhausted";
        AnalysisJobEntity job = runningJob(jobId, 3, 2, NOW.minusMinutes(10));
        givenCandidates(job);
        givenReadResult(3, JobResultStore.Status.NOT_FOUND, jobId);
        when(jobRepository.findByIdForUpdate(jobId)).thenReturn(Optional.of(job));
        when(jobRepository.save(any(AnalysisJobEntity.class))).thenAnswer(i -> i.getArgument(0));

        JobRecoveryService.RecoveryStats stats = service.runOnce();

        assertEquals(1, stats.failed(), stats.details().toString());
        verify(jobRepository, never()).requeueExpired(anyString(), anyInt(), any(), any());
        verify(outboxRepository, never()).save(any());
        assertEquals(AnalysisJobEntity.JobStatus.FAILED, job.getStatus());
        assertTrue(job.getErrorMessage().contains("recovery attempts exhausted (3)"),
                job.getErrorMessage());
        assertNotNull(job.getCompletedAt());
    }

    // ---------------------------------------------------------------- ERROR

    @Test
    void storeErrorDefersWithoutRequeue() {
        String jobId = "job-store-error";
        AnalysisJobEntity job = runningJob(jobId, 2, 0, NOW.minusMinutes(10));
        givenCandidates(job);
        givenReadResult(2, JobResultStore.Status.ERROR, jobId);
        when(jobRepository.findByIdForUpdate(jobId)).thenReturn(Optional.of(job));
        when(jobRepository.save(any(AnalysisJobEntity.class))).thenAnswer(i -> i.getArgument(0));

        JobRecoveryService.RecoveryStats stats = service.runOnce();

        assertEquals(1, stats.deferred(), stats.details().toString());
        verify(jobRepository, never()).requeueExpired(anyString(), anyInt(), any(), any());
        verify(outboxRepository, never()).save(any());
        verify(analysisJobService, never()).applyResult(anyString(), anyInt(), anyMap());
        assertEquals(NOW.plusSeconds(BACKOFF), job.getNextRecoveryAt());
        assertEquals(NOW, job.getRecoveryCheckedAt());
        assertEquals(AnalysisJobEntity.JobStatus.RUNNING, job.getStatus());
    }

    // -------------------------------------------------------------- INVALID

    @Test
    void invalidStoredResultMarksFailedInsteadOfRequeue() {
        String jobId = "job-invalid";
        AnalysisJobEntity job = runningJob(jobId, 1, 0, NOW.minusMinutes(10));
        givenCandidates(job);
        givenReadResult(1, JobResultStore.Status.INVALID, jobId);
        when(jobRepository.findByIdForUpdate(jobId)).thenReturn(Optional.of(job));
        when(jobRepository.save(any(AnalysisJobEntity.class))).thenAnswer(i -> i.getArgument(0));

        JobRecoveryService.RecoveryStats stats = service.runOnce();

        assertEquals(1, stats.failed(), stats.details().toString());
        verify(jobRepository, never()).requeueExpired(anyString(), anyInt(), any(), any());
        verify(outboxRepository, never()).save(any());
        assertEquals(AnalysisJobEntity.JobStatus.FAILED, job.getStatus());
        assertTrue(job.getErrorMessage().startsWith("result invalid: "), job.getErrorMessage());
    }

    // ------------------------------------------------------------- 竞争/候选

    @Test
    void conditionalRequeueMatchingZeroRowsSkipsOutboxReset() {
        String jobId = "job-race";
        AnalysisJobEntity job = runningJob(jobId, 1, 0, NOW.minusMinutes(10));
        givenCandidates(job);
        givenReadResult(1, JobResultStore.Status.NOT_FOUND, jobId);
        // 期间被心跳续租/完成/其它扫描器处理：条件更新命中 0 行。
        when(jobRepository.requeueExpired(eq(jobId), eq(1), any(), any())).thenReturn(0);

        JobRecoveryService.RecoveryStats stats = service.runOnce();

        assertEquals(1, stats.skipped(), stats.details().toString());
        assertEquals(0, stats.requeued());
        verify(outboxRepository, never()).save(any());
        verify(outboxRepository, never()).findByAggregateIdOrderByCreatedAtAsc(anyString());
        verify(jobRepository, never()).save(any());
    }

    @Test
    void scanUsesLeaseGraceCutoffAndLeavesValidLeasesToTheRepository() {
        // 租约仍在有效期内：条件查询本身不会返回该行。
        when(jobRepository.findRecoverable(any(), any(), any(Pageable.class))).thenReturn(List.of());

        JobRecoveryService.RecoveryStats stats = service.runOnce();

        ArgumentCaptor<LocalDateTime> cutoff = ArgumentCaptor.forClass(LocalDateTime.class);
        ArgumentCaptor<LocalDateTime> now = ArgumentCaptor.forClass(LocalDateTime.class);
        ArgumentCaptor<Pageable> pageable = ArgumentCaptor.forClass(Pageable.class);
        verify(jobRepository).findRecoverable(cutoff.capture(), now.capture(), pageable.capture());
        assertEquals(NOW, now.getValue());
        assertEquals(NOW.minusSeconds(GRACE), cutoff.getValue());
        assertEquals(20, pageable.getValue().getPageSize());
        assertEquals(0, stats.candidates());
        verifyNoInteractions(resultStore, outboxRepository, analysisJobService);
        verify(jobRepository, never()).save(any());
    }

    @Test
    void historicalRunningRowWithoutLeaseIsRecoverable() {
        // 回归：claim 对 lease_until IS NULL 的 RUNNING 行返回 recovery_pending，
        // 若回收器也跳过它，任务会永久停在 RUNNING（两端都不处理 = 死锁）。
        AnalysisJobEntity legacy = runningJob("job-no-lease", 1, 0, null);
        legacy.setHeartbeatAt(NOW.minusHours(2));
        givenCandidates(legacy);
        givenReadResult(1, JobResultStore.Status.NOT_FOUND, "job-no-lease");
        when(jobRepository.requeueExpired(anyString(), anyInt(), any(), any())).thenReturn(1);
        when(jobRepository.findByIdForUpdate("job-no-lease")).thenReturn(Optional.of(legacy));
        when(jobRepository.save(any())).thenAnswer(invocation -> invocation.getArgument(0));

        JobRecoveryService.RecoveryStats stats = service.runOnce();

        assertEquals(1, stats.candidates());
        assertEquals(1, stats.requeued(), "无租约的历史 RUNNING 行也必须被回收器处理");
        // 状态变更由 @Modifying 批量 UPDATE 完成（此处是 mock，不会改实体），
        // 因此断言调用参数与重排计数，而不是断言实体 status。
        verify(jobRepository).requeueExpired(eq("job-no-lease"), eq(1), any(), any());
        assertEquals(1, legacy.getRecoveryCount());
        assertEquals(1, legacy.getAttempt(), "回收不得改变 attempt");
    }

    @Test
    void deliveryFailureBacksOffInsteadOfRepeatingForever() {
        // 审查 A 的 F1：存储正常但 applyResult 持续抛异常时必须写 next_recovery_at，
        // 否则同一批 20 行每轮都占满扫描页，第 21 条之后的过期任务永远不被回收。
        String jobId = "job-deliver-fail";
        AnalysisJobEntity job = runningJob(jobId, 1, 0, NOW.minusMinutes(10));
        givenCandidates(job);
        givenReadResult(1, JobResultStore.Status.FOUND, jobId);
        when(analysisJobService.applyResult(anyString(), anyInt(), anyMap()))
                .thenThrow(new IllegalStateException("FK violation on lesson_sessions"));

        AnalysisJobEntity persisted = runningJob(jobId, 1, 0, NOW.minusMinutes(10));
        when(jobRepository.findByIdForUpdate(jobId)).thenReturn(Optional.of(persisted));
        when(jobRepository.save(any())).thenAnswer(invocation -> invocation.getArgument(0));

        JobRecoveryService.RecoveryStats stats = service.runOnce();

        assertEquals(1, stats.deferred(), stats.details().toString());
        assertEquals(1, persisted.getRecoveryCount());
        assertEquals(NOW.plusSeconds(BACKOFF), persisted.getNextRecoveryAt());
        assertEquals(AnalysisJobEntity.JobStatus.RUNNING, persisted.getStatus(),
                "单次交付失败不应立刻判 FAILED");
        verify(jobRepository, never()).requeueExpired(anyString(), anyInt(), any(), any());
    }

    @Test
    void deliveryFailureExhaustsAfterMaxAttempts() {
        String jobId = "job-deliver-exhaust";
        AnalysisJobEntity job = runningJob(jobId, 1, 2, NOW.minusMinutes(10));
        givenCandidates(job);
        givenReadResult(1, JobResultStore.Status.FOUND, jobId);
        when(analysisJobService.applyResult(anyString(), anyInt(), anyMap()))
                .thenThrow(new IllegalStateException("persistent failure"));

        AnalysisJobEntity persisted = runningJob(jobId, 1, 2, NOW.minusMinutes(10));
        when(jobRepository.findByIdForUpdate(jobId)).thenReturn(Optional.of(persisted));
        when(jobRepository.save(any())).thenAnswer(invocation -> invocation.getArgument(0));

        JobRecoveryService.RecoveryStats stats = service.runOnce();

        assertEquals(1, stats.failed());
        assertEquals(AnalysisJobEntity.JobStatus.FAILED, persisted.getStatus());
        assertEquals(3, persisted.getRecoveryCount());
        assertTrue(persisted.getErrorMessage().contains("result delivery failed"));
        assertNull(persisted.getNextRecoveryAt());
    }

    @Test
    void partialJobsAreReingestedThroughTheSharedPath() {
        String jobId = "job-partial";
        AnalysisJobEntity partial = runningJob(jobId, 1, 0, null);
        partial.setStatus(AnalysisJobEntity.JobStatus.PARTIAL);
        // RUNNING 扫描为空，只有 PARTIAL 补偿命中。
        when(jobRepository.findRecoverable(any(), any(), any(Pageable.class))).thenReturn(List.of());
        when(jobRepository.findReingestCandidates(any(), any(Pageable.class)))
                .thenReturn(List.of(partial));
        when(analysisJobService.reingestStoredResult(jobId, 3, BACKOFF))
                .thenReturn(AnalysisJobService.ReingestOutcome.REINGESTED);

        JobRecoveryService.RecoveryStats stats = service.runOnce();

        assertEquals(1, stats.reingested(), stats.details().toString());
        assertEquals(0, stats.reingestFailed());
        verify(analysisJobService).reingestStoredResult(jobId, 3, BACKOFF);
        verify(jobRepository, never()).requeueExpired(anyString(), anyInt(), any(), any());
    }

    @Test
    void partialReingestFailureIsCounted() {
        String jobId = "job-partial-fail";
        AnalysisJobEntity partial = runningJob(jobId, 1, 0, null);
        partial.setStatus(AnalysisJobEntity.JobStatus.PARTIAL);
        when(jobRepository.findRecoverable(any(), any(), any(Pageable.class))).thenReturn(List.of());
        when(jobRepository.findReingestCandidates(any(), any(Pageable.class)))
                .thenReturn(List.of(partial));
        when(analysisJobService.reingestStoredResult(anyString(), anyInt(), anyLong()))
                .thenReturn(AnalysisJobService.ReingestOutcome.FAILED);

        JobRecoveryService.RecoveryStats stats = service.runOnce();

        assertEquals(0, stats.reingested());
        assertEquals(1, stats.reingestFailed());
    }

    // ------------------------------------------------------ R03: 对齐待补偿扫描

    @Test
    void alignmentPendingJobsAreRealignedInTheirOwnRound() {
        String jobId = "job-align-pending";
        AnalysisJobEntity succeeded = runningJob(jobId, 1, 0, null);
        succeeded.setStatus(AnalysisJobEntity.JobStatus.SUCCEEDED);
        succeeded.setIngestState(AnalysisJobService.INGEST_INGESTED);
        succeeded.setAlignmentState(AnalysisJobService.ALIGNMENT_PENDING);
        when(jobRepository.findRecoverable(any(), any(), any(Pageable.class))).thenReturn(List.of());
        when(jobRepository.findRealignCandidates(any(), any(Pageable.class)))
                .thenReturn(List.of(succeeded));
        when(analysisJobService.realignStoredResult(jobId, 3, BACKOFF))
                .thenReturn(AnalysisJobService.RealignOutcome.REALIGNED);

        JobRecoveryService.RecoveryStats stats = service.runOnce();

        assertEquals(1, stats.realigned(), stats.details().toString());
        assertEquals(0, stats.realignFailed());
        assertTrue(stats.summaryLine().contains("realigned=1"), stats.summaryLine());
        verify(analysisJobService).realignStoredResult(jobId, 3, BACKOFF);
        // 对齐补偿不是重排、不是重放 ingest。
        verify(jobRepository, never()).requeueExpired(anyString(), anyInt(), any(), any());
        verify(analysisJobService, never()).reingestStoredResult(anyString(), anyInt(), anyLong());
        verify(outboxRepository, never()).save(any());
    }

    @Test
    void realignFailureIsCountedSeparately() {
        String jobId = "job-align-stuck";
        AnalysisJobEntity succeeded = runningJob(jobId, 1, 0, null);
        succeeded.setStatus(AnalysisJobEntity.JobStatus.SUCCEEDED);
        succeeded.setAlignmentState(AnalysisJobService.ALIGNMENT_PENDING);
        when(jobRepository.findRecoverable(any(), any(), any(Pageable.class))).thenReturn(List.of());
        when(jobRepository.findRealignCandidates(any(), any(Pageable.class)))
                .thenReturn(List.of(succeeded));
        when(analysisJobService.realignStoredResult(anyString(), anyInt(), anyLong()))
                .thenReturn(AnalysisJobService.RealignOutcome.EXHAUSTED);

        JobRecoveryService.RecoveryStats stats = service.runOnce();

        assertEquals(0, stats.realigned());
        assertEquals(1, stats.realignFailed());
        assertTrue(stats.summaryLine().contains("realignFailed=1"), stats.summaryLine());
    }

    @Test
    void realignExceptionDoesNotAbortTheRound() {
        AnalysisJobEntity first = runningJob("job-align-boom", 1, 0, null);
        first.setStatus(AnalysisJobEntity.JobStatus.SUCCEEDED);
        first.setAlignmentState(AnalysisJobService.ALIGNMENT_PENDING);
        AnalysisJobEntity second = runningJob("job-align-ok", 1, 0, null);
        second.setStatus(AnalysisJobEntity.JobStatus.SUCCEEDED);
        second.setAlignmentState(AnalysisJobService.ALIGNMENT_PENDING);
        when(jobRepository.findRecoverable(any(), any(), any(Pageable.class))).thenReturn(List.of());
        when(jobRepository.findRealignCandidates(any(), any(Pageable.class)))
                .thenReturn(List.of(first, second));
        when(analysisJobService.realignStoredResult(eq("job-align-boom"), anyInt(), anyLong()))
                .thenThrow(new IllegalStateException("boom"));
        when(analysisJobService.realignStoredResult(eq("job-align-ok"), anyInt(), anyLong()))
                .thenReturn(AnalysisJobService.RealignOutcome.REALIGNED);

        JobRecoveryService.RecoveryStats stats = service.runOnce();

        assertEquals(1, stats.realignFailed());
        assertEquals(1, stats.realigned());
    }

    @Test
    void disabledRecoveryDoesNothing() {
        service.configureForTest(false, 20, GRACE, 3, BACKOFF, ALERT_AFTER);

        JobRecoveryService.RecoveryStats stats = service.runOnce();

        assertEquals(0, stats.candidates());
        assertEquals(0, stats.alerts());
        verifyNoInteractions(jobRepository, resultStore, outboxRepository, analysisJobService);
    }

    // ---------------------------------------------------------------- 告警

    @Test
    void staleRunningAlertIsEmittedOncePerJobAndAttempt() {
        AnalysisJobEntity stale = runningJob("job-stuck", 5, 0, NOW.minusMinutes(60));
        when(jobRepository.findRunningWithoutProgress(any(), any(Pageable.class)))
                .thenReturn(List.of(stale));
        when(jobRepository.findRecoverable(any(), any(), any(Pageable.class))).thenReturn(List.of());

        JobRecoveryService.RecoveryStats first = service.runOnce();
        JobRecoveryService.RecoveryStats second = service.runOnce();

        assertEquals(1, first.alerts(), "首次扫描应告警");
        assertEquals(0, second.alerts(), "同一 (jobId, attempt) 不应重复告警");
        verify(jobRepository, times(2)).findRunningWithoutProgress(any(), any(Pageable.class));
        // 告警不改状态。
        verify(jobRepository, never()).save(any());

        ArgumentCaptor<LocalDateTime> startedBefore = ArgumentCaptor.forClass(LocalDateTime.class);
        verify(jobRepository, times(2)).findRunningWithoutProgress(startedBefore.capture(), any());
        assertEquals(NOW.minusSeconds(ALERT_AFTER), startedBefore.getValue());
    }
}
