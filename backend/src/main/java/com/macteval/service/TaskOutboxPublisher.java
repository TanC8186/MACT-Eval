package com.macteval.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.macteval.model.entity.TaskOutboxEntity;
import com.macteval.repository.TaskOutboxRepository;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.data.domain.PageRequest;
import org.springframework.data.redis.core.RedisTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * 事务性 outbox 发布器。
 *
 * <p>任务行与 outbox 事件在同一数据库事务写入；本发布器独立地把事件投递到
 * Redis Stream，失败时指数退避重试。投递是“至少一次”语义，消费者通过
 * claim + 幂等回调保证重复消息不会重复产生业务结果。</p>
 */
@Slf4j
@Component
@RequiredArgsConstructor
public class TaskOutboxPublisher {

    static final String JOB_STREAM = "analysis:jobs";
    private static final int BATCH_SIZE = 20;
    private static final int MAX_BACKOFF_SECONDS = 60;
    private static final int RECONCILE_BATCH_SIZE = 20;

    private final TaskOutboxRepository outboxRepository;
    private final RedisTemplate<String, String> redisTemplate;
    private final ObjectMapper objectMapper;

    @Value("${mact.outbox.stale-queued-seconds:300}")
    private long staleQueuedSeconds = 300;

    /**
     * Republish published events whose job is still QUEUED after a grace
     * period. A Redis restart without its stream/group (or a FLUSHALL) makes
     * ``published_at`` a lie: the database has a job, but no consumer can ever
     * receive it. Resetting the same event keeps at most one retry per grace
     * window instead of accumulating duplicates.
     */
    @Scheduled(fixedDelayString = "${mact.outbox.reconcile-interval-ms:60000}")
    @Transactional
    public void requeueStaleQueuedJobs() {
        long graceSeconds = Math.max(30, staleQueuedSeconds);
        LocalDateTime cutoff = LocalDateTime.now().minusSeconds(graceSeconds);
        List<TaskOutboxEntity> stale = outboxRepository.findStalePublishedForQueuedJobs(
                cutoff, PageRequest.of(0, RECONCILE_BATCH_SIZE));
        for (TaskOutboxEntity event : stale) {
            // V33 park 守卫：被消息修复迁移判定为“不可发送”的事件不得被重新武装。
            // 迁移把它们放到 next_retry_at = now + 100 年，但本查询只看 published_at，
            // 所以这里必须显式跳过，否则坏 payload 每 300 秒复活一次。
            if (JobRecoveryService.isParkedByRepair(event)) {
                log.warn("Outbox event {} for job {} is parked by outbox repair "
                                + "(last_error={}); not republishing",
                        event.getId(), event.getAggregateId(), event.getLastError());
                continue;
            }
            event.setPublishedAt(null);
            event.setAttempt(0);
            event.setNextRetryAt(LocalDateTime.now());
            event.setLastError("requeued: job still QUEUED after " + graceSeconds + "s");
            outboxRepository.save(event);
            log.warn("Outbox event {} for job {} was published but the job is still QUEUED; "
                            + "republishing after {}s", event.getId(), event.getAggregateId(), graceSeconds);
        }
    }

    @Scheduled(fixedDelayString = "${mact.outbox.poll-interval-ms:2000}")
    @Transactional
    public void publishPending() {
        List<TaskOutboxEntity> pending = outboxRepository.findPending(
                LocalDateTime.now(), PageRequest.of(0, BATCH_SIZE));
        for (TaskOutboxEntity event : pending) {
            try {
                Map<String, Object> parsed = objectMapper.readValue(
                        event.getPayload(), new TypeReference<Map<String, Object>>() {});
                Map<String, String> message = new HashMap<>();
                parsed.forEach((k, v) -> message.put(String.valueOf(k), v == null ? "" : String.valueOf(v)));
                redisTemplate.opsForStream().add(JOB_STREAM, message);
                event.setPublishedAt(LocalDateTime.now());
                event.setLastError(null);
                log.info("Outbox published {} for aggregate {}", event.getEventType(), event.getAggregateId());
            } catch (Exception e) {
                int attempt = (event.getAttempt() == null ? 0 : event.getAttempt()) + 1;
                event.setAttempt(attempt);
                long backoff = Math.min(MAX_BACKOFF_SECONDS, 1L << Math.min(attempt, 6));
                event.setNextRetryAt(LocalDateTime.now().plusSeconds(backoff));
                event.setLastError(e.getMessage());
                log.warn("Outbox publish failed for {} (attempt {}), retry in {}s: {}",
                        event.getAggregateId(), attempt, backoff, e.getMessage());
            }
            outboxRepository.save(event);
        }
    }
}
