package com.macteval.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.macteval.model.entity.AnalysisJobEntity;
import com.macteval.model.entity.TaskOutboxEntity;
import com.macteval.repository.AnalysisJobRepository;
import com.macteval.repository.TaskOutboxRepository;
import org.junit.jupiter.api.Test;
import org.springframework.data.domain.Pageable;
import org.springframework.data.redis.core.RedisTemplate;
import org.springframework.data.redis.core.StreamOperations;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyMap;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.*;

/**
 * AI03/AI04：outbox 投递、任务租约、心跳与迟到回调。
 */
class TaskReliabilityTest {

    @Test
    void outboxPublisherMarksPublishedOnSuccess() throws Exception {
        TaskOutboxRepository repo = mock(TaskOutboxRepository.class);
        RedisTemplate<String, String> redis = mock(RedisTemplate.class);
        @SuppressWarnings("unchecked")
        StreamOperations<String, Object, Object> stream = mock(StreamOperations.class);
        when(redis.opsForStream()).thenReturn(stream);

        TaskOutboxEntity event = new TaskOutboxEntity();
        event.setId("o-1");
        event.setAggregateId("job-1");
        event.setEventType("ANALYSIS_JOB_QUEUED");
        event.setPayload("{\"id\":\"job-1\",\"type\":\"VIDEO_ANALYSIS\"}");
        when(repo.findPending(any(LocalDateTime.class), any(Pageable.class))).thenReturn(List.of(event));
        when(repo.save(event)).thenReturn(event);

        new TaskOutboxPublisher(repo, redis, new ObjectMapper()).publishPending();

        verify(stream).add(eq("analysis:jobs"), anyMap());
        assertNotNull(event.getPublishedAt());
        assertNull(event.getLastError());
    }

    @Test
    void outboxPublisherSchedulesRetryOnRedisFailure() throws Exception {
        TaskOutboxRepository repo = mock(TaskOutboxRepository.class);
        RedisTemplate<String, String> redis = mock(RedisTemplate.class);
        @SuppressWarnings("unchecked")
        StreamOperations<String, Object, Object> stream = mock(StreamOperations.class);
        when(redis.opsForStream()).thenReturn(stream);
        when(stream.add(anyString(), anyMap())).thenThrow(new RuntimeException("redis down"));

        TaskOutboxEntity event = new TaskOutboxEntity();
        event.setId("o-2");
        event.setAggregateId("job-2");
        event.setEventType("ANALYSIS_JOB_QUEUED");
        event.setPayload("{\"id\":\"job-2\"}");
        event.setAttempt(0);
        LocalDateTime before = LocalDateTime.now();
        when(repo.findPending(any(LocalDateTime.class), any(Pageable.class))).thenReturn(List.of(event));
        when(repo.save(event)).thenReturn(event);

        new TaskOutboxPublisher(repo, redis, new ObjectMapper()).publishPending();

        assertNull(event.getPublishedAt(), "Redis 失败不能标记已发布");
        assertEquals(1, event.getAttempt());
        assertNotNull(event.getNextRetryAt());
        assertTrue(event.getNextRetryAt().isAfter(before));
        assertTrue(event.getLastError().contains("redis down"));
    }

    @Test
    void stalePublishedOutboxEventIsRequeuedWhenJobIsStillQueued() {
        TaskOutboxRepository repo = mock(TaskOutboxRepository.class);
        RedisTemplate<String, String> redis = mock(RedisTemplate.class);
        TaskOutboxEntity event = new TaskOutboxEntity();
        event.setId("o-stale");
        event.setAggregateId("job-stale-queue");
        event.setPublishedAt(LocalDateTime.now().minusMinutes(6));
        event.setAttempt(2);
        when(repo.findStalePublishedForQueuedJobs(any(LocalDateTime.class), any(Pageable.class)))
                .thenReturn(List.of(event));
        when(repo.save(event)).thenReturn(event);

        new TaskOutboxPublisher(repo, redis, new ObjectMapper()).requeueStaleQueuedJobs();

        assertNull(event.getPublishedAt());
        assertEquals(0, event.getAttempt());
        assertNotNull(event.getNextRetryAt());
        assertTrue(event.getLastError().contains("requeued"));
    }

    /**
     * V33 park 守卫：被消息修复迁移判为"不可发送"的事件，reconciler 不得重新武装。
     *
     * <p>{@code findStalePublishedForQueuedJobs} 只看 {@code published_at}，
     * 完全不看 {@code next_retry_at}；若不给显式守卫，V33 park 的坏 payload 会每
     * ~300 秒被复位成 attempt=0 / next_retry_at=now，重新进入投递集合。</p>
     */
    @Test
    void reconcilerLeavesRepairParkedEventsAlone() {
        TaskOutboxRepository repo = mock(TaskOutboxRepository.class);
        RedisTemplate<String, String> redis = mock(RedisTemplate.class);
        TaskOutboxEntity parked = new TaskOutboxEntity();
        parked.setId("o-parked");
        parked.setAggregateId("job-parked");
        parked.setPayload("[1,2]");
        parked.setPublishedAt(LocalDateTime.now().minusMinutes(6));
        parked.setAttempt(2);
        parked.setNextRetryAt(LocalDateTime.now().plusYears(100));
        parked.setLastError("repair_failed: params is array");
        when(repo.findStalePublishedForQueuedJobs(any(LocalDateTime.class), any(Pageable.class)))
                .thenReturn(List.of(parked));

        new TaskOutboxPublisher(repo, redis, new ObjectMapper()).requeueStaleQueuedJobs();

        assertEquals("repair_failed: params is array", parked.getLastError(),
                "parked 事件的 last_error 不得被改写成 requeued");
        assertNotNull(parked.getPublishedAt(), "published_at 不得被清空");
        assertEquals(2, parked.getAttempt(), "attempt 不得被归零");
        assertTrue(parked.getNextRetryAt().isAfter(LocalDateTime.now().plusYears(50)),
                "next_retry_at 必须仍在极远未来");
        verify(repo, never()).save(any(TaskOutboxEntity.class));
    }

    @Test
    void claimIsExclusiveAndHeartbeatExtendsLease() {
        AnalysisJobRepository jobRepo = mock(AnalysisJobRepository.class);
        AnalysisJobService service = jobService(jobRepo);

        AnalysisJobEntity job = new AnalysisJobEntity();
        job.setId("job-claim");
        job.setStatus(AnalysisJobEntity.JobStatus.QUEUED);
        job.setAttempt(0);
        when(jobRepo.findByIdForUpdate("job-claim")).thenReturn(Optional.of(job));
        when(jobRepo.save(job)).thenReturn(job);

        var first = service.claimJob("job-claim", 120);
        assertEquals(true, first.get("claimed"));
        assertEquals(1, first.get("attempt"));
        assertEquals(AnalysisJobEntity.JobStatus.RUNNING, job.getStatus());
        assertNotNull(job.getLeaseUntil());

        var second = service.claimJob("job-claim", 120);
        assertEquals(false, second.get("claimed"), "有效租约内不能重复领取");

        assertTrue(service.heartbeat("job-claim", 300));
        assertNotNull(job.getHeartbeatAt());
    }

    @Test
    void heartbeatRejectsStaleAttemptAfterTakeover() {
        AnalysisJobRepository jobRepo = mock(AnalysisJobRepository.class);
        AnalysisJobService service = jobService(jobRepo);
        AnalysisJobEntity job = new AnalysisJobEntity();
        job.setId("job-heartbeat");
        job.setStatus(AnalysisJobEntity.JobStatus.RUNNING);
        job.setAttempt(2);
        when(jobRepo.findByIdForUpdate("job-heartbeat")).thenReturn(Optional.of(job));
        when(jobRepo.save(job)).thenReturn(job);

        assertFalse(service.heartbeat("job-heartbeat", 120, 1));
        assertTrue(service.heartbeat("job-heartbeat", 120, 2));
        assertFalse(service.heartbeat("job-heartbeat", 120),
                "attempt-less heartbeat must not extend a takeover lease");
    }

    @Test
    void completedJobCannotBeClaimedAgain() {
        AnalysisJobRepository jobRepo = mock(AnalysisJobRepository.class);
        AnalysisJobService service = jobService(jobRepo);
        AnalysisJobEntity job = new AnalysisJobEntity();
        job.setId("job-done");
        job.setStatus(AnalysisJobEntity.JobStatus.SUCCEEDED);
        job.setAttempt(1);
        when(jobRepo.findByIdForUpdate("job-done")).thenReturn(Optional.of(job));

        assertEquals(false, service.claimJob("job-done", 120).get("claimed"));
    }

    @Test
    void staleCallbackAttemptIsIgnored() {
        AnalysisJobRepository jobRepo = mock(AnalysisJobRepository.class);
        BehaviorService behavior = mock(BehaviorService.class);
        AnalysisJobService service = new AnalysisJobService(
                jobRepo, mock(TaskOutboxRepository.class), mock(RedisTemplate.class),
                new ObjectMapper(), behavior, mock(TranscriptService.class),
                mock(WindowAlignmentService.class));

        AnalysisJobEntity job = new AnalysisJobEntity();
        job.setId("job-stale");
        job.setType(AnalysisJobEntity.JobType.VIDEO_ANALYSIS);
        job.setStatus(AnalysisJobEntity.JobStatus.RUNNING);
        job.setAttempt(2);
        job.setParams("{\"lessonSessionId\":\"lesson-1\"}");
        when(jobRepo.findByIdForUpdate("job-stale")).thenReturn(Optional.of(job));

        service.completeJob("job-stale", Map.of(
                "status", "succeeded", "attempt", 1, "observations", List.of()));

        verify(behavior, never()).ingestCompletedVideoJob(any());
        assertEquals(AnalysisJobEntity.JobStatus.RUNNING, job.getStatus());
    }

    @Test
    void attemptLessCallbackCannotOverwriteTakeover() {
        AnalysisJobRepository jobRepo = mock(AnalysisJobRepository.class);
        BehaviorService behavior = mock(BehaviorService.class);
        AnalysisJobService service = new AnalysisJobService(
                jobRepo, mock(TaskOutboxRepository.class), mock(RedisTemplate.class),
                new ObjectMapper(), behavior, mock(TranscriptService.class),
                mock(WindowAlignmentService.class));

        AnalysisJobEntity job = new AnalysisJobEntity();
        job.setId("job-attemptless");
        job.setType(AnalysisJobEntity.JobType.VIDEO_ANALYSIS);
        job.setStatus(AnalysisJobEntity.JobStatus.RUNNING);
        job.setAttempt(2);
        job.setParams("{\"lessonSessionId\":\"lesson-1\"}");
        when(jobRepo.findByIdForUpdate("job-attemptless")).thenReturn(Optional.of(job));

        var outcome = service.completeJob("job-attemptless", Map.of(
                "status", "succeeded", "observations", List.of()));

        verify(behavior, never()).ingestCompletedVideoJob(any());
        assertEquals(AnalysisJobEntity.JobStatus.RUNNING, job.getStatus());
        assertEquals(false, outcome.get("applied"));
        assertEquals("stale_attempt", outcome.get("reason"));
    }

    @Test
    void stringAttemptCannotBypassStaleCallbackGuard() {
        AnalysisJobRepository jobRepo = mock(AnalysisJobRepository.class);
        BehaviorService behavior = mock(BehaviorService.class);
        AnalysisJobService service = new AnalysisJobService(
                jobRepo, mock(TaskOutboxRepository.class), mock(RedisTemplate.class),
                new ObjectMapper(), behavior, mock(TranscriptService.class),
                mock(WindowAlignmentService.class));

        AnalysisJobEntity job = new AnalysisJobEntity();
        job.setId("job-string-attempt");
        job.setType(AnalysisJobEntity.JobType.VIDEO_ANALYSIS);
        job.setStatus(AnalysisJobEntity.JobStatus.RUNNING);
        job.setAttempt(2);
        job.setParams("{\"lessonSessionId\":\"lesson-1\"}");
        when(jobRepo.findByIdForUpdate("job-string-attempt")).thenReturn(Optional.of(job));

        service.completeJob("job-string-attempt", Map.of(
                "status", "succeeded", "attempt", "1", "observations", List.of()));

        verify(behavior, never()).ingestCompletedVideoJob(any());
        assertEquals(AnalysisJobEntity.JobStatus.RUNNING, job.getStatus());
    }

    private AnalysisJobService jobService(AnalysisJobRepository jobRepo) {
        return new AnalysisJobService(
                jobRepo, mock(TaskOutboxRepository.class), mock(RedisTemplate.class),
                new ObjectMapper(), mock(BehaviorService.class), mock(TranscriptService.class),
                mock(WindowAlignmentService.class));
    }
}
