package com.macteval.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.macteval.model.entity.AnalysisJobEntity;
import com.macteval.repository.AnalysisJobRepository;
import com.macteval.repository.TaskOutboxRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.data.redis.core.RedisTemplate;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 协议 §1：claim 决策表、完成决策表与 attempt 严格解析的围栏测试。
 *
 * <p>纯 Mockito，不启动 Spring 上下文（与 {@link TaskReliabilityTest} 一致）。
 */
class JobFencingTest {

    private AnalysisJobRepository jobRepository;
    private BehaviorService behaviorService;
    private WindowAlignmentService windowAlignmentService;
    private AnalysisJobService service;

    @BeforeEach
    void setUp() {
        jobRepository = mock(AnalysisJobRepository.class);
        behaviorService = mock(BehaviorService.class);
        windowAlignmentService = mock(WindowAlignmentService.class);
        service = new AnalysisJobService(
                jobRepository, mock(TaskOutboxRepository.class), mock(RedisTemplate.class),
                new ObjectMapper(), behaviorService, mock(TranscriptService.class),
                windowAlignmentService);
    }

    // ------------------------------------------------------------------
    // claim 决策表（协议 §1.1）
    // ------------------------------------------------------------------

    @Test
    void expiredRunningClaimReturnsRecoveryPendingWithoutAdvancingAttempt() {
        AnalysisJobEntity job = job("job-expired", AnalysisJobEntity.JobStatus.RUNNING, 3);
        job.setLeaseUntil(LocalDateTime.now().minusSeconds(30));
        when(jobRepository.findByIdForUpdate("job-expired")).thenReturn(Optional.of(job));

        Map<String, Object> outcome = service.claimJob("job-expired", 120);

        assertEquals(false, outcome.get("claimed"));
        assertEquals("recovery_pending", outcome.get("reason"));
        assertEquals("RUNNING", outcome.get("status"));
        assertEquals(3, outcome.get("attempt"));
        // attempt 不变、状态不变、不续租、不落库
        assertEquals(3, job.getAttempt());
        assertEquals(AnalysisJobEntity.JobStatus.RUNNING, job.getStatus());
        assertTrue(job.getLeaseUntil().isBefore(LocalDateTime.now()));
        verify(jobRepository, never()).save(any(AnalysisJobEntity.class));
    }

    @Test
    void runningJobWithNullLeaseIsRecoveryPending() {
        AnalysisJobEntity job = job("job-null-lease", AnalysisJobEntity.JobStatus.RUNNING, 1);
        job.setLeaseUntil(null);
        when(jobRepository.findByIdForUpdate("job-null-lease")).thenReturn(Optional.of(job));

        Map<String, Object> outcome = service.claimJob("job-null-lease", 120);

        assertEquals(false, outcome.get("claimed"));
        assertEquals("recovery_pending", outcome.get("reason"));
        assertEquals(1, job.getAttempt());
        verify(jobRepository, never()).save(any(AnalysisJobEntity.class));
    }

    @Test
    void validLeaseIsNotClaimedTwice() {
        AnalysisJobEntity job = job("job-leased", AnalysisJobEntity.JobStatus.RUNNING, 2);
        job.setLeaseUntil(LocalDateTime.now().plusSeconds(60));
        when(jobRepository.findByIdForUpdate("job-leased")).thenReturn(Optional.of(job));

        Map<String, Object> outcome = service.claimJob("job-leased", 120);

        assertEquals(false, outcome.get("claimed"));
        assertEquals("leased", outcome.get("reason"));
        assertEquals(2, outcome.get("attempt"));
        assertEquals(2, job.getAttempt());
        verify(jobRepository, never()).save(any(AnalysisJobEntity.class));
    }

    @Test
    void terminalJobClaimIsRejectedAsTerminal() {
        AnalysisJobEntity job = job("job-terminal", AnalysisJobEntity.JobStatus.CANCELLED, 1);
        when(jobRepository.findByIdForUpdate("job-terminal")).thenReturn(Optional.of(job));

        Map<String, Object> outcome = service.claimJob("job-terminal", 120);

        assertEquals(false, outcome.get("claimed"));
        assertEquals("terminal", outcome.get("reason"));
        assertEquals(1, outcome.get("attempt"));
    }

    @Test
    void queuedClaimAdvancesAttemptAndSetsLease() {
        AnalysisJobEntity job = job("job-queued", AnalysisJobEntity.JobStatus.QUEUED, 0);
        when(jobRepository.findByIdForUpdate("job-queued")).thenReturn(Optional.of(job));
        when(jobRepository.save(job)).thenReturn(job);

        Map<String, Object> outcome = service.claimJob("job-queued", 120);

        assertEquals(true, outcome.get("claimed"));
        assertEquals(1, outcome.get("attempt"));
        assertEquals(1, job.getAttempt());
        assertEquals(AnalysisJobEntity.JobStatus.RUNNING, job.getStatus());
        assertNotNull(job.getHeartbeatAt());
        assertNotNull(job.getLeaseUntil());
    }

    // ------------------------------------------------------------------
    // attempt 严格解析（协议 §1.3）
    // ------------------------------------------------------------------

    @Test
    void parserRejectsNonIntegerAttempts() {
        assertInvalid(1.0d);
        assertInvalid(2.0f);
        assertInvalid(new java.math.BigDecimal("1"));
        assertInvalid(Boolean.TRUE);
        assertInvalid(-1);
        assertInvalid(-1L);
        assertInvalid("abc");
        assertInvalid("1.0");
        assertInvalid("1e3");
        assertInvalid(String.valueOf((long) Integer.MAX_VALUE + 1));
        assertInvalid((long) Integer.MAX_VALUE + 1);
        assertInvalid(null);
    }

    @Test
    void parserAcceptsJsonIntegersAndDigitStrings() {
        AnalysisJobService.ParsedAttempt integer = AnalysisJobService.parseAttempt(1);
        assertTrue(integer.present());
        assertTrue(integer.valid());
        assertEquals(1, integer.value());

        AnalysisJobService.ParsedAttempt longValue = AnalysisJobService.parseAttempt(2L);
        assertTrue(longValue.valid());
        assertEquals(2, longValue.value());

        AnalysisJobService.ParsedAttempt text = AnalysisJobService.parseAttempt("2");
        assertTrue(text.valid());
        assertEquals(2, text.value());

        AnalysisJobService.ParsedAttempt padded = AnalysisJobService.parseAttempt(" 7 ");
        assertTrue(padded.valid());
        assertEquals(7, padded.value());
    }

    @Test
    void missingAttemptKeyIsPresentFalseAndNullValueIsInvalid() {
        AnalysisJobService.ParsedAttempt absent = AnalysisJobService.parseAttemptField(
                Map.of("status", "succeeded"));
        assertFalse(absent.present());
        assertFalse(absent.valid());

        java.util.HashMap<String, Object> withNull = new java.util.HashMap<>();
        withNull.put("attempt", null);
        AnalysisJobService.ParsedAttempt explicitNull = AnalysisJobService.parseAttemptField(withNull);
        assertTrue(explicitNull.present());
        assertFalse(explicitNull.valid());
    }

    @Test
    void completeWithInvalidAttemptReturnsInvalidAttemptAndDoesNotIngest() {
        AnalysisJobEntity job = job("job-invalid", AnalysisJobEntity.JobStatus.RUNNING, 1);
        when(jobRepository.findByIdForUpdate("job-invalid")).thenReturn(Optional.of(job));

        Map<String, Object> outcome = service.completeJob("job-invalid", Map.of(
                "status", "succeeded", "attempt", 1.0d, "observations", List.of()));

        assertEquals(false, outcome.get("applied"));
        assertEquals("invalid_attempt", outcome.get("reason"));
        verify(behaviorService, never()).ingestCompletedVideoJob(any());
        verify(jobRepository, never()).save(any(AnalysisJobEntity.class));
        assertEquals(AnalysisJobEntity.JobStatus.RUNNING, job.getStatus());
    }

    // ------------------------------------------------------------------
    // 完成决策表（协议 §1.2）
    // ------------------------------------------------------------------

    @Test
    void queuedJobResultIsRejectedAsNotRunning() {
        AnalysisJobEntity job = job("job-queued-cb", AnalysisJobEntity.JobStatus.QUEUED, 0);
        when(jobRepository.findByIdForUpdate("job-queued-cb")).thenReturn(Optional.of(job));

        Map<String, Object> outcome = service.completeJob("job-queued-cb", Map.of(
                "status", "succeeded", "attempt", 0, "observations", List.of()));

        assertEquals(false, outcome.get("applied"));
        assertEquals("not_running", outcome.get("reason"));
        verify(behaviorService, never()).ingestCompletedVideoJob(any());
        verify(jobRepository, never()).save(any(AnalysisJobEntity.class));
    }

    @Test
    void cancelledJobResultIsRejectedWithoutIngest() {
        AnalysisJobEntity job = job("job-cancelled-cb", AnalysisJobEntity.JobStatus.CANCELLED, 1);
        when(jobRepository.findByIdForUpdate("job-cancelled-cb")).thenReturn(Optional.of(job));

        Map<String, Object> outcome = service.completeJob("job-cancelled-cb", Map.of(
                "status", "succeeded", "attempt", 1, "observations", List.of()));

        // CANCELLED 是终态（协议 §1 终态定义），因此按 §1.2 顺序先命中 already_terminal。
        assertEquals(false, outcome.get("applied"));
        assertEquals("already_terminal", outcome.get("reason"));
        assertEquals("CANCELLED", outcome.get("status"));
        verify(behaviorService, never()).ingestCompletedVideoJob(any());
        verify(jobRepository, never()).save(any(AnalysisJobEntity.class));
    }

    @Test
    void duplicateCallbackOnTerminalJobDoesNotSaveOrIngest() {
        AnalysisJobEntity job = job("job-dup", AnalysisJobEntity.JobStatus.SUCCEEDED, 1);
        job.setResultMetadata("{\"status\":\"succeeded\"}");
        when(jobRepository.findByIdForUpdate("job-dup")).thenReturn(Optional.of(job));

        Map<String, Object> outcome = service.applyResult("job-dup", 1, Map.of(
                "status", "succeeded", "observations", List.of()));

        assertEquals(false, outcome.get("applied"));
        assertEquals("already_terminal", outcome.get("reason"));
        assertEquals(1, outcome.get("attempt"));
        verify(behaviorService, never()).ingestCompletedVideoJob(any());
        verify(jobRepository, never()).save(any(AnalysisJobEntity.class));
    }

    @Test
    void applyResultWithStaleAttemptDoesNotIngest() {
        AnalysisJobEntity job = job("job-stale-apply", AnalysisJobEntity.JobStatus.RUNNING, 2);
        when(jobRepository.findByIdForUpdate("job-stale-apply")).thenReturn(Optional.of(job));

        Map<String, Object> outcome = service.applyResult("job-stale-apply", 1, Map.of(
                "status", "succeeded", "observations", List.of()));

        assertEquals(false, outcome.get("applied"));
        assertEquals("stale_attempt", outcome.get("reason"));
        assertEquals(2, outcome.get("attempt"));
        verify(behaviorService, never()).ingestCompletedVideoJob(any());
        verify(jobRepository, never()).save(any(AnalysisJobEntity.class));
    }

    @Test
    void applyResultSucceedsAndIngestsOnce() {
        AnalysisJobEntity job = job("job-apply-ok", AnalysisJobEntity.JobStatus.RUNNING, 1);
        job.setParams("{\"lessonSessionId\":\"lesson-1\"}");
        when(jobRepository.findByIdForUpdate("job-apply-ok")).thenReturn(Optional.of(job));
        when(jobRepository.save(job)).thenReturn(job);

        Map<String, Object> outcome = service.applyResult("job-apply-ok", 1, Map.of(
                "status", "succeeded", "observations", List.of()));

        assertEquals(true, outcome.get("applied"));
        assertEquals("SUCCEEDED", outcome.get("status"));
        assertEquals(1, outcome.get("attempt"));
        verify(behaviorService, times(1)).ingestCompletedVideoJob(job);
        verify(jobRepository, times(1)).save(job);
    }

    @Test
    void attemptLessCallbackOnFirstClaimIsAccepted() {
        AnalysisJobEntity job = job("job-legacy-ok", AnalysisJobEntity.JobStatus.RUNNING, 1);
        job.setParams("{}");
        when(jobRepository.findByIdForUpdate("job-legacy-ok")).thenReturn(Optional.of(job));
        when(jobRepository.save(job)).thenReturn(job);

        Map<String, Object> outcome = service.completeJob("job-legacy-ok", Map.of(
                "status", "succeeded", "observations", List.of()));

        assertEquals(true, outcome.get("applied"));
        assertEquals(1, outcome.get("attempt"));
    }

    @Test
    void attemptLessCallbackAfterTakeoverIsStale() {
        AnalysisJobEntity job = job("job-legacy-stale", AnalysisJobEntity.JobStatus.RUNNING, 2);
        when(jobRepository.findByIdForUpdate("job-legacy-stale")).thenReturn(Optional.of(job));

        Map<String, Object> outcome = service.completeJob("job-legacy-stale", Map.of(
                "status", "succeeded", "observations", List.of()));

        assertEquals(false, outcome.get("applied"));
        assertEquals("stale_attempt", outcome.get("reason"));
        verify(behaviorService, never()).ingestCompletedVideoJob(any());
    }

    // ------------------------------------------------------------------
    // 心跳原因区分
    // ------------------------------------------------------------------

    @Test
    void heartbeatReasonDistinguishesNotRunningFromAttemptMismatch() {
        AnalysisJobEntity queued = job("job-hb-queued", AnalysisJobEntity.JobStatus.QUEUED, 1);
        when(jobRepository.findByIdForUpdate("job-hb-queued")).thenReturn(Optional.of(queued));
        Map<String, Object> notRunning = service.heartbeatWithReason("job-hb-queued", 120, 1);
        assertEquals(false, notRunning.get("extended"));
        assertEquals("not_running", notRunning.get("reason"));

        AnalysisJobEntity running = job("job-hb-mismatch", AnalysisJobEntity.JobStatus.RUNNING, 2);
        when(jobRepository.findByIdForUpdate("job-hb-mismatch")).thenReturn(Optional.of(running));
        Map<String, Object> mismatch = service.heartbeatWithReason("job-hb-mismatch", 120, 1);
        assertEquals(false, mismatch.get("extended"));
        assertEquals("attempt_mismatch", mismatch.get("reason"));
        verify(jobRepository, never()).save(any(AnalysisJobEntity.class));
    }

    @Test
    void heartbeatWithCurrentAttemptExtendsLease() {
        AnalysisJobEntity job = job("job-hb-ok", AnalysisJobEntity.JobStatus.RUNNING, 2);
        when(jobRepository.findByIdForUpdate("job-hb-ok")).thenReturn(Optional.of(job));
        when(jobRepository.save(job)).thenReturn(job);

        Map<String, Object> outcome = service.heartbeatWithReason("job-hb-ok", 120, 2);

        assertEquals(true, outcome.get("extended"));
        assertNotNull(job.getHeartbeatAt());
        assertTrue(job.getLeaseUntil().isAfter(LocalDateTime.now()));
    }

    // ------------------------------------------------------------------

    private static AnalysisJobEntity job(String id, AnalysisJobEntity.JobStatus status, int attempt) {
        AnalysisJobEntity job = new AnalysisJobEntity();
        job.setId(id);
        job.setType(AnalysisJobEntity.JobType.VIDEO_ANALYSIS);
        job.setStatus(status);
        job.setAttempt(attempt);
        return job;
    }

    private static void assertInvalid(Object raw) {
        AnalysisJobService.ParsedAttempt parsed = AnalysisJobService.parseAttempt(raw);
        assertTrue(parsed.present(), "raw=" + raw);
        assertFalse(parsed.valid(), "should reject: " + raw);
    }
}
