package com.macteval.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.macteval.model.entity.AnalysisJobEntity;
import com.macteval.model.entity.TaskOutboxEntity;
import com.macteval.repository.AnalysisJobRepository;
import com.macteval.repository.TaskOutboxRepository;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.data.domain.PageRequest;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;

import java.time.Clock;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.NoSuchElementException;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

/**
 * 数据库驱动的 RUNNING 任务回收器（协议 §5）。
 *
 * <p>覆盖的窗口：worker 已把结果写入 MinIO 并 ACK，但 HTTP 回调未发出就退出，
 * 任务停在 RUNNING 直到租约过期。回收器以数据库为唯一真相，先从结果存储尝试
 * 交付；只有存储<b>明确确认</b>没有结果时才重排。</p>
 *
 * <h2>事务边界（本类的核心约束）</h2>
 * <ul>
 *   <li>扫描候选：只读查询，不持有行锁，不在扫描期间做任何外部 I/O。</li>
 *   <li>外部 I/O（MinIO 探测/读取、{@code applyResult} 落库）：<b>在事务外</b>
 *       执行。MinIO 超时可以是秒级，把它放进写事务会长时间占用数据库连接与行锁。</li>
 *   <li>状态变更：短事务 + 条件更新（校验 status/attempt/lease 后仅一次生效）。
 *       这里显式使用 {@link TransactionTemplate} 而不是 {@code @Transactional}
 *       私有方法：同类自调用不会经过 Spring 代理，注解会静默失效。</li>
 * </ul>
 *
 * <h2>判定（协议 §5.2）</h2>
 * <table>
 *   <tr><td>FOUND</td><td>调用 {@link AnalysisJobService#applyResult}（与 HTTP 回调同一入口）</td></tr>
 *   <tr><td>NOT_FOUND</td><td>条件重排；达到 maxAttempts 上限则 FAILED</td></tr>
 *   <tr><td>INVALID</td><td>FAILED（结果不可信，需人工），不重排</td></tr>
 *   <tr><td>ERROR</td><td>只写 next_recovery_at 退避，绝不重排</td></tr>
 * </table>
 */
@Slf4j
@Service
public class JobRecoveryService {

    /** 告警扫描的固定上限，避免异常积压时告警本身占满数据库。 */
    private static final int ALERT_BATCH_SIZE = 50;

    /**
     * 消息修复迁移（V33）给“不可发送”事件写入的 last_error 前缀。
     * 与 V33__harden_v29_outbox_payload_repair.sql 中的字面量必须保持一致。
     */
    static final String PARKED_LAST_ERROR_PREFIX = "repair_failed:";

    private final AnalysisJobRepository jobRepository;
    private final TaskOutboxRepository outboxRepository;
    private final JobResultStore jobResultStore;
    private final AnalysisJobService analysisJobService;
    private final ObjectMapper objectMapper;
    private final TransactionTemplate transactionTemplate;
    private final Clock clock;

    /** 已告警的 (jobId, attempt)，防止每个扫描周期重复刷屏；任务推进时移除。 */
    private final Set<String> alertedKeys = ConcurrentHashMap.newKeySet();

    @Value("${mact.recovery.enabled:true}")
    private boolean enabled = true;

    @Value("${mact.recovery.batch-size:20}")
    private int batchSize = 20;

    @Value("${mact.recovery.lease-grace-seconds:60}")
    private long leaseGraceSeconds = 60;

    @Value("${mact.recovery.max-attempts:3}")
    private int maxAttempts = 3;

    @Value("${mact.recovery.retry-backoff-seconds:120}")
    private long retryBackoffSeconds = 120;

    @Value("${mact.recovery.alert-after-seconds:900}")
    private long alertAfterSeconds = 900;

    @Autowired
    public JobRecoveryService(AnalysisJobRepository jobRepository,
                              TaskOutboxRepository outboxRepository,
                              JobResultStore jobResultStore,
                              AnalysisJobService analysisJobService,
                              ObjectMapper objectMapper,
                              PlatformTransactionManager transactionManager,
                              ObjectProvider<Clock> clockProvider) {
        this(jobRepository, outboxRepository, jobResultStore, analysisJobService, objectMapper,
                transactionManager, clockProvider.getIfAvailable(Clock::systemDefaultZone));
    }

    /** 测试入口：显式注入固定时钟。 */
    JobRecoveryService(AnalysisJobRepository jobRepository,
                       TaskOutboxRepository outboxRepository,
                       JobResultStore jobResultStore,
                       AnalysisJobService analysisJobService,
                       ObjectMapper objectMapper,
                       PlatformTransactionManager transactionManager,
                       Clock clock) {
        this.jobRepository = jobRepository;
        this.outboxRepository = outboxRepository;
        this.jobResultStore = jobResultStore;
        this.analysisJobService = analysisJobService;
        this.objectMapper = objectMapper;
        this.clock = clock;
        this.transactionTemplate = new TransactionTemplate(transactionManager);
        this.transactionTemplate.setPropagationBehavior(
                org.springframework.transaction.TransactionDefinition.PROPAGATION_REQUIRES_NEW);
    }

    /** 供测试调整配置（@Value 只在 Spring 上下文里生效）。 */
    void configureForTest(boolean enabled, int batchSize, long leaseGraceSeconds,
                          int maxAttempts, long retryBackoffSeconds, long alertAfterSeconds) {
        this.enabled = enabled;
        this.batchSize = batchSize;
        this.leaseGraceSeconds = leaseGraceSeconds;
        this.maxAttempts = maxAttempts;
        this.retryBackoffSeconds = retryBackoffSeconds;
        this.alertAfterSeconds = alertAfterSeconds;
    }

    /**
     * 统计摘要。{@code details} 只含 jobId/attempt/原因，不含签名 URL、凭据或结果内容。
     */
    public record RecoveryStats(int candidates, int completed, int requeued, int failed,
                                int deferred, int skipped, int alerts, int reingested,
                                int reingestFailed, int realigned, int realignFailed,
                                List<String> details) {

        static RecoveryStats empty() {
            return new RecoveryStats(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, List.of());
        }

        public String summaryLine() {
            return "recovery candidates=" + candidates + " completed=" + completed
                    + " requeued=" + requeued + " failed=" + failed + " deferred=" + deferred
                    + " skipped=" + skipped + " alerts=" + alerts
                    + " reingested=" + reingested + " reingestFailed=" + reingestFailed
                    + " realigned=" + realigned + " realignFailed=" + realignFailed;
        }
    }

    private enum Outcome { COMPLETED, REQUEUED, FAILED, DEFERRED, SKIPPED }

    private record CandidateResult(Outcome outcome, String detail) {}

    @Scheduled(fixedDelayString = "${mact.recovery.interval-ms:30000}")
    public void recoverExpiredRunning() {
        try {
            runOnce();
        } catch (Exception e) {
            // 定时任务不能因为单次异常而停止后续周期。
            log.error("Job recovery scan failed: {}", e.getMessage(), e);
        }
    }

    /**
     * 执行一轮回收（测试与手动触发入口）。
     *
     * <p>候选查询在事务外完成：只读取 id/attempt/recoveryCount 快照，随后对每个任务
     * 先做外部 I/O，再在独立短事务里做条件状态变更。</p>
     */
    public RecoveryStats runOnce() {
        if (!enabled) {
            log.info("Job recovery is disabled (mact.recovery.enabled=false)");
            return RecoveryStats.empty();
        }
        LocalDateTime now = now();
        LocalDateTime cutoff = now.minusSeconds(Math.max(0, leaseGraceSeconds));

        // 只读扫描：不持有行锁，不做外部 I/O。
        List<AnalysisJobEntity> candidates = jobRepository.findRecoverable(
                cutoff, now, PageRequest.of(0, Math.max(1, batchSize)));

        int completed = 0;
        int requeued = 0;
        int failed = 0;
        int deferred = 0;
        int skipped = 0;
        List<String> details = new ArrayList<>();

        for (AnalysisJobEntity candidate : candidates) {
            String jobId = candidate.getId();
            int attempt = attemptOf(candidate);
            try {
                CandidateResult result = processCandidate(jobId, attempt,
                        recoveryCountOf(candidate), cutoff, now);
                switch (result.outcome()) {
                    case COMPLETED -> completed++;
                    case REQUEUED -> requeued++;
                    case FAILED -> failed++;
                    case DEFERRED -> deferred++;
                    case SKIPPED -> skipped++;
                }
                if (result.detail() != null) {
                    details.add(jobId + "#" + attempt + " " + result.outcome().name().toLowerCase()
                            + " " + result.detail());
                }
            } catch (Exception e) {
                // 单个任务失败不影响本轮其它任务；下一轮会重新判断。
                skipped++;
                details.add(jobId + "#" + attempt + " error " + e.getClass().getSimpleName());
                log.warn("Recovery of job {} attempt {} failed: {}", jobId, attempt, e.getMessage());
            }
        }

        int reingested = 0;
        int reingestFailed = 0;
        // PARTIAL 补偿：结果已在 resultMetadata 中但 ingest 曾失败。
        // 与 RUNNING 回收分开计数，避免把「补齐业务数据」混成「重新推理」。
        List<AnalysisJobEntity> partials = jobRepository.findReingestCandidates(
                now, PageRequest.of(0, Math.max(1, batchSize)));
        if (partials == null) {
            partials = List.of();
        }
        for (AnalysisJobEntity partial : partials) {
            String jobId = partial.getId();
            int attempt = attemptOf(partial);
            try {
                AnalysisJobService.ReingestOutcome outcome = analysisJobService
                        .reingestStoredResult(jobId, maxAttempts, retryBackoffSeconds);
                switch (outcome) {
                    case REINGESTED -> {
                        reingested++;
                        details.add(jobId + "#" + attempt + " reingested");
                        log.info("Reingested PARTIAL job {} attempt {}", jobId, attempt);
                    }
                    case FAILED, EXHAUSTED -> {
                        reingestFailed++;
                        details.add(jobId + "#" + attempt + " reingest-"
                                + outcome.name().toLowerCase());
                    }
                    case NOT_APPLICABLE -> { }
                }
            } catch (Exception e) {
                reingestFailed++;
                details.add(jobId + "#" + attempt + " reingest-error "
                        + e.getClass().getSimpleName());
                log.warn("Reingest of PARTIAL job {} attempt {} failed: {}",
                        jobId, attempt, e.getMessage());
            }
        }
        if (!partials.isEmpty()) {
            log.info("Ingest compensation: candidates={} reingested={} failed={}",
                    partials.size(), reingested, reingestFailed);
        }

        int realigned = 0;
        int realignFailed = 0;
        // 窗口对齐补偿（R03）：观测已落库但对齐失败的任务仍是 SUCCEEDED，
        // 只有这一轮扫描能找到它们；重试只重建派生窗口，不重新推理。
        List<AnalysisJobEntity> pendingAlignments = jobRepository.findRealignCandidates(
                now, PageRequest.of(0, Math.max(1, batchSize)));
        if (pendingAlignments == null) {
            pendingAlignments = List.of();
        }
        for (AnalysisJobEntity pending : pendingAlignments) {
            String jobId = pending.getId();
            int attempt = attemptOf(pending);
            try {
                AnalysisJobService.RealignOutcome outcome = analysisJobService
                        .realignStoredResult(jobId, maxAttempts, retryBackoffSeconds);
                switch (outcome) {
                    case REALIGNED -> {
                        realigned++;
                        details.add(jobId + "#" + attempt + " realigned");
                        log.info("Realigned windows for job {} attempt {}", jobId, attempt);
                    }
                    case FAILED, EXHAUSTED -> {
                        realignFailed++;
                        details.add(jobId + "#" + attempt + " realign-"
                                + outcome.name().toLowerCase());
                    }
                    case NOT_APPLICABLE -> { }
                }
            } catch (Exception e) {
                realignFailed++;
                details.add(jobId + "#" + attempt + " realign-error "
                        + e.getClass().getSimpleName());
                log.warn("Realign of job {} attempt {} failed: {}", jobId, attempt, e.getMessage());
            }
        }
        if (!pendingAlignments.isEmpty()) {
            log.info("Alignment compensation: candidates={} realigned={} failed={}",
                    pendingAlignments.size(), realigned, realignFailed);
        }

        int alerts = emitStaleRunningAlerts(now);

        RecoveryStats stats = new RecoveryStats(candidates.size(), completed, requeued,
                failed, deferred, skipped, alerts, reingested, reingestFailed,
                realigned, realignFailed, List.copyOf(details));
        log.info(stats.summaryLine());
        if (!details.isEmpty()) {
            log.debug("Job recovery round: {}", details);
        }
        return stats;
    }

    private CandidateResult processCandidate(String jobId, int attempt, int recoveryCount,
                                             LocalDateTime cutoff, LocalDateTime now) {
        if (attempt < 1) {
            // 协议 §5.1：结果对象按 attempt 存放，attempt=0 的行没有可读对象。
            return new CandidateResult(Outcome.SKIPPED, "attempt < 1");
        }

        // ==== 外部 I/O：必须在任何写事务之外 ====
        JobResultStore.ResultRead read = jobResultStore.read(jobId, attempt);
        // ======================================

        return switch (read.status()) {
            case FOUND -> deliverFound(jobId, attempt, read);
            case NOT_FOUND -> requeueOrFail(jobId, attempt, recoveryCount, cutoff, now);
            case INVALID -> failInvalid(jobId, attempt, cutoff, read.detail(), now);
            case ERROR -> deferForStoreError(jobId, attempt, cutoff, read.detail(), now);
        };
    }

    /**
     * 结果已持久化并通过字节校验：与 HTTP 回调共用 {@code applyResult}。
     * 不再自行落库/ingest，避免两条完成路径产生不一致的业务数据。
     */
    private CandidateResult deliverFound(String jobId, int attempt, JobResultStore.ResultRead read) {
        Map<String, Object> applied;
        try {
            applied = analysisJobService.applyResult(jobId, attempt, read.result());
        } catch (NoSuchElementException e) {
            return new CandidateResult(Outcome.SKIPPED, "job vanished");
        } catch (Exception e) {
            // 存储正常但交付失败（如 FK 违例、持续死锁）：必须退避，否则同一批行
            // 每轮都占满扫描页、把后面的过期任务永久饿死（审查 A 的 F1）。
            log.error("applyResult failed for job {} attempt {}: {}", jobId, attempt, e.getMessage(), e);
            return deferDeliveryFailure(jobId, attempt, "applyResult error "
                    + e.getClass().getSimpleName());
        }
        Object isApplied = applied == null ? null : applied.get("applied");
        if (Boolean.TRUE.equals(isApplied)) {
            alertedKeys.remove(alertKey(jobId, attempt));
            recordResultRef(jobId, attempt, read.ref());
            log.info("Recovered result for job {} attempt {} from {}", jobId, attempt,
                    read.ref() == null ? "store" : read.ref().resultKey());
            return new CandidateResult(Outcome.COMPLETED, "sha256=" + shortSha(read));
        }
        Object reason = applied == null ? null : applied.get("reason");
        log.info("Recovered result not applied for job {} attempt {}: {}", jobId, attempt, reason);
        return new CandidateResult(Outcome.SKIPPED, "not applied: " + reason);
    }

    /**
     * 存储确认没有结果：条件重排；重排次数达到上限则 FAILED（协议 §5.3/§5.4）。
     *
     * <p>{@code requeueExpired} 返回 0 表示期间被心跳续租/完成/取消/另一个扫描器
     * 处理过——此时<b>不能</b>重置 outbox，否则会给一个正在跑的任务多发一条消息。</p>
     *
     * <p>注意执行顺序：条件 UPDATE 之前<b>不</b>加载实体。JPA 的批量更新不会刷新
     * 一级缓存，先加载再批量更新会让后续 {@code save()} 把过期的 RUNNING 写回，
     * 反而抵消条件更新的效果。因此先做 UPDATE，再重新读取（此时缓存中没有旧实例）
     * 来累加 recovery_count。</p>
     */
    private CandidateResult requeueOrFail(String jobId, int attempt, int recoveryCountSnapshot,
                                          LocalDateTime cutoff, LocalDateTime now) {
        if (recoveryCountSnapshot + 1 >= Math.max(1, maxAttempts)) {
            return markExhausted(jobId, attempt, recoveryCountSnapshot, cutoff, now);
        }
        return transactionTemplate.execute(status -> {
            int updated = jobRepository.requeueExpired(jobId, attempt, cutoff, now);
            if (updated != 1) {
                // 条件更新未命中：别的路径已经推进了状态（心跳续租/完成/取消/其他扫描器）。
                log.debug("Requeue of job {} attempt {} lost the race ({} rows)", jobId, attempt, updated);
                return new CandidateResult(Outcome.SKIPPED,
                        "requeue conditional update matched 0 rows");
            }
            AnalysisJobEntity fresh = jobRepository.findByIdForUpdate(jobId).orElse(null);
            if (fresh == null) {
                return new CandidateResult(Outcome.SKIPPED, "job vanished after requeue");
            }
            int current = recoveryCountOf(fresh);
            if (current <= recoveryCountSnapshot) {
                // 条件查询未累加 recovery_count 时由这里补记；已累加则不重复计数。
                fresh.setRecoveryCount(current + 1);
            }
            // 协议 §5.3：重排后清空上次失败原因，避免新一次尝试带着旧错误。
            fresh.setErrorMessage(null);
            jobRepository.save(fresh);
            resetOutbox(fresh, now);
            alertedKeys.remove(alertKey(jobId, attempt));
            log.warn("Job {} attempt {} requeued after expired lease (recovery #{})",
                    jobId, attempt, recoveryCountOf(fresh));
            return new CandidateResult(Outcome.REQUEUED, "recovery #" + recoveryCountOf(fresh));
        });
    }

    /** 重排次数达到上限：条件置 FAILED 并写明原因，不再重排（协议 §5.4）。 */
    private CandidateResult markExhausted(String jobId, int attempt, int recoveryCount,
                                          LocalDateTime cutoff, LocalDateTime now) {
        int nextCount = recoveryCount + 1;
        return transactionTemplate.execute(status -> {
            AnalysisJobEntity job = jobRepository.findByIdForUpdate(jobId).orElse(null);
            if (job == null || !isSameRunningAttempt(job, attempt, cutoff)) {
                return new CandidateResult(Outcome.SKIPPED, "row changed before exhaustion");
            }
            job.setStatus(AnalysisJobEntity.JobStatus.FAILED);
            job.setLeaseUntil(null);
            job.setHeartbeatAt(null);
            job.setCompletedAt(now);
            job.setRecoveryCheckedAt(now);
            job.setNextRecoveryAt(null);
            job.setRecoveryCount(nextCount);
            job.setErrorMessage("recovery attempts exhausted (" + nextCount + ")");
            jobRepository.save(job);
            alertedKeys.remove(alertKey(jobId, attempt));
            log.error("Job {} attempt {} exhausted recovery attempts ({}); marked FAILED",
                    jobId, attempt, nextCount);
            return new CandidateResult(Outcome.FAILED, "attempts exhausted");
        });
    }

    /**
     * 结果不可信（envelope 缺失/摘要不符/字段不一致）：转 FAILED 等人工处理。
     * <b>绝不</b>当作“没有结果”去重新推理，那会覆盖已经写好的对象。
     */
    private CandidateResult failInvalid(String jobId, int attempt, LocalDateTime cutoff,
                                        String detail, LocalDateTime now) {
        String reason = "result invalid: " + safeDetail(detail);
        return transactionTemplate.execute(status -> {
            AnalysisJobEntity job = jobRepository.findByIdForUpdate(jobId).orElse(null);
            if (job == null || !isSameRunningAttempt(job, attempt, cutoff)) {
                return new CandidateResult(Outcome.SKIPPED, "row changed before invalid handling");
            }
            job.setStatus(AnalysisJobEntity.JobStatus.FAILED);
            job.setLeaseUntil(null);
            job.setHeartbeatAt(null);
            job.setCompletedAt(now);
            job.setRecoveryCheckedAt(now);
            job.setNextRecoveryAt(null);
            job.setErrorMessage(truncate(reason, 1000));
            jobRepository.save(job);
            alertedKeys.remove(alertKey(jobId, attempt));
            log.error("Job {} attempt {} has an invalid stored result ({}); marked FAILED",
                    jobId, attempt, safeDetail(detail));
            return new CandidateResult(Outcome.FAILED, reason);
        });
    }

    /**
     * 存储可读但交付失败：退避 + 计数，达到上限转 FAILED。
     *
     * <p>与 {@link #deferForStoreError} 的区别：那是「不知道」，不计次；这里是
     * 「知道但做不成」，重复失败说明需要人工介入，因此计 recovery_count 并可终止。
     * 两者都必须写 next_recovery_at，否则扫描页会被同一批行永久占满。</p>
     */
    private CandidateResult deferDeliveryFailure(String jobId, int attempt, String detail) {
        LocalDateTime now = now();
        LocalDateTime nextAt = now.plusSeconds(Math.max(1, retryBackoffSeconds));
        return transactionTemplate.execute(status -> {
            AnalysisJobEntity job = jobRepository.findByIdForUpdate(jobId).orElse(null);
            if (job == null || job.getStatus() != AnalysisJobEntity.JobStatus.RUNNING
                    || attemptOf(job) != attempt) {
                return new CandidateResult(Outcome.SKIPPED, "row changed before delivery deferral");
            }
            int nextCount = recoveryCountOf(job) + 1;
            job.setRecoveryCount(nextCount);
            job.setRecoveryCheckedAt(now);
            if (nextCount >= Math.max(1, maxAttempts)) {
                job.setStatus(AnalysisJobEntity.JobStatus.FAILED);
                job.setLeaseUntil(null);
                job.setHeartbeatAt(null);
                job.setCompletedAt(now);
                job.setNextRecoveryAt(null);
                job.setErrorMessage("result delivery failed " + nextCount + " times: "
                        + safeDetail(detail));
                jobRepository.save(job);
                alertedKeys.remove(alertKey(jobId, attempt));
                log.error("Job {} attempt {} exhausted result delivery ({}); marked FAILED",
                        jobId, attempt, nextCount);
                return new CandidateResult(Outcome.FAILED, "delivery_error exhausted");
            }
            job.setNextRecoveryAt(nextAt);
            jobRepository.save(job);
            log.warn("Result delivery failed for job {} attempt {} ({}/{}); next check at {}",
                    jobId, attempt, nextCount, maxAttempts, nextAt);
            return new CandidateResult(Outcome.DEFERRED, "delivery_error #" + nextCount);
        });
    }

    /**
     * 存储不可达/无权限/超时：只记录并延后下次探测，<b>不重排</b>。
     * 把“不知道”当成“没有”会重复推理并可能覆盖已写好的结果（协议 §5.2）。
     */
    private CandidateResult deferForStoreError(String jobId, int attempt, LocalDateTime cutoff,
                                               String detail, LocalDateTime now) {
        LocalDateTime nextAt = now.plusSeconds(Math.max(1, retryBackoffSeconds));
        return transactionTemplate.execute(status -> {
            AnalysisJobEntity job = jobRepository.findByIdForUpdate(jobId).orElse(null);
            if (job == null || !isSameRunningAttempt(job, attempt, cutoff)) {
                return new CandidateResult(Outcome.SKIPPED, "row changed before deferral");
            }
            job.setRecoveryCheckedAt(now);
            job.setNextRecoveryAt(nextAt);
            jobRepository.save(job);
            log.warn("Result store unavailable for job {} attempt {} ({}); next check at {}",
                    jobId, attempt, safeDetail(detail), nextAt);
            return new CandidateResult(Outcome.DEFERRED, "next check at " + nextAt);
        });
    }

    /**
     * 重排时同一事务内重置该任务的 outbox 事件（协议 §5.3）：
     * 有事件则 {@code published_at=NULL, attempt=0, next_retry_at=now}；
     * 没有事件则插入一条 {@code ANALYSIS_JOB_QUEUED}（消息结构与
     * {@link AnalysisJobService#createJob} 一致）。
     */
    private void resetOutbox(AnalysisJobEntity job, LocalDateTime now) {
        String jobId = job.getId();
        List<TaskOutboxEntity> events = outboxRepository.findByAggregateIdOrderByCreatedAtAsc(jobId);
        if (events == null) {
            events = List.of();
        }
        if (!events.isEmpty()) {
            boolean republishable = false;
            for (TaskOutboxEntity event : events) {
                // V33 park 守卫：被迁移判定为“不可发送”的事件（payload 不是合法入队
                // 对象，last_error 以 repair_failed: 开头）**不得**在这里被重新打开。
                // 否则一次租约过期就把坏 payload 复位成 attempt=0/next_retry_at=now，
                // 重新进入投递集合，V33 想消灭的无限退避循环会原样复活。
                // 这类事件只能由人工按运维手册确认后解除（unpark）。
                if (isParkedByRepair(event)) {
                    log.warn("Outbox event {} for job {} is parked by outbox repair "
                            + "(last_error={}); leaving it parked for manual triage",
                            event.getId(), jobId, event.getLastError());
                    continue;
                }
                event.setPublishedAt(null);
                event.setAttempt(0);
                event.setNextRetryAt(now);
                event.setLastError("requeued by recovery");
                outboxRepository.save(event);
                republishable = true;
            }
            // 全是被 park 的事件时不能再补一条新事件：那等于给坏任务重新投递。
            if (!republishable) {
                return;
            }
            return;
        }
        TaskOutboxEntity event = new TaskOutboxEntity();
        event.setId(UUID.randomUUID().toString());
        event.setAggregateId(jobId);
        event.setEventType("ANALYSIS_JOB_QUEUED");
        event.setPayload(buildQueuedPayload(job));
        event.setAttempt(0);
        event.setNextRetryAt(now);
        outboxRepository.save(event);
        log.info("Inserted missing outbox event for recovered job {}", jobId);
    }

    /**
     * 该 outbox 事件是否被历史消息修复迁移（V33）判定为“不可发送”并 park。
     *
     * <p>判据就是迁移写入的 {@code last_error} 前缀 {@code repair_failed:}。
     * 这类事件的 payload 不是合法的入队对象（params 曾是 JSON 数组/字符串/数字/
     * 非法 JSON），重新投递只会再次失败，因此任何自动重排都必须跳过它，
     * 交给人工按运维手册处理。</p>
     */
    static boolean isParkedByRepair(TaskOutboxEntity event) {
        if (event == null) {
            return false;
        }
        String lastError = event.getLastError();
        return lastError != null && lastError.startsWith(PARKED_LAST_ERROR_PREFIX);
    }

    /**
     * 与 {@link AnalysisJobService#createJob} 的入队消息同构：
     * id/type/priority + 展平的 params（worker 依赖这些键完整，缺项会导致消息无法处理）。
     */
    private String buildQueuedPayload(AnalysisJobEntity job) {
        String jobId = job.getId();
        Map<String, Object> message = new HashMap<>();
        message.put("id", jobId);
        message.put("type", job.getType() == null ? null : job.getType().name());
        message.put("priority", job.getPriority() == null ? null : job.getPriority().name());
        String params = job.getParams();
        if (params != null && !params.isBlank()) {
            try {
                Map<String, Object> parsed = objectMapper.readValue(
                        params, new TypeReference<Map<String, Object>>() {});
                message.putAll(parsed);
            } catch (Exception e) {
                log.warn("Could not flatten params for recovered job {}: {}", jobId, e.getMessage());
            }
        }
        try {
            return objectMapper.writeValueAsString(message);
        } catch (Exception e) {
            // 兜底：至少保证 id 可投递，避免事件无法序列化导致任务永远不被领取。
            return "{\"id\":\"" + jobId + "\"}";
        }
    }

    /** 审计：把已交付结果的引用写入 last_result_ref（仅元数据，不含签名 URL）。 */
    private void recordResultRef(String jobId, int attempt, JobResultStore.ResultRef ref) {
        if (ref == null) {
            return;
        }
        String json;
        try {
            json = objectMapper.writeValueAsString(ref.toMap());
        } catch (Exception e) {
            log.debug("Could not serialize result ref for job {}", jobId);
            return;
        }
        try {
            transactionTemplate.executeWithoutResult(status -> {
                AnalysisJobEntity job = jobRepository.findByIdForUpdate(jobId).orElse(null);
                if (job == null || attemptOf(job) != attempt || job.getStatus() == null
                        || job.getStatus() == AnalysisJobEntity.JobStatus.RUNNING
                        || job.getStatus() == AnalysisJobEntity.JobStatus.QUEUED) {
                    return;
                }
                job.setLastResultRef(json);
                jobRepository.save(job);
            });
        } catch (Exception e) {
            // 审计字段写失败不能影响已完成的交付。
            log.warn("Could not record result ref for job {}: {}", jobId, e.getMessage());
        }
    }

    /** 只告警、不改状态（协议 §5 告警要求）；同一 (jobId, attempt) 只告警一次。 */
    private int emitStaleRunningAlerts(LocalDateTime now) {
        LocalDateTime startedBefore = now.minusSeconds(Math.max(1, alertAfterSeconds));
        List<AnalysisJobEntity> stale = jobRepository.findRunningWithoutProgress(
                startedBefore, PageRequest.of(0, ALERT_BATCH_SIZE));
        if (stale == null || stale.isEmpty()) {
            alertedKeys.clear();
            return 0;
        }
        Set<String> stillAlerted = new LinkedHashSet<>();
        int alerts = 0;
        for (AnalysisJobEntity job : stale) {
            String key = alertKey(job.getId(), attemptOf(job));
            stillAlerted.add(key);
            if (alertedKeys.add(key)) {
                alerts++;
                log.warn("RUNNING job {} has been running since {} without completing (attempt={})",
                        job.getId(), job.getStartedAt(), attemptOf(job));
            }
        }
        // 未被截断的一页说明覆盖了全部告警对象，可安全出清已恢复的键；
        // 被截断时保留未出现的键，避免同一任务因分页顺序反复告警。
        if (stale.size() < ALERT_BATCH_SIZE) {
            alertedKeys.retainAll(stillAlerted);
        }
        return alerts;
    }

    /**
     * 条件更新的二次确认：仍为同一 RUNNING + attempt，且租约仍处于过期状态。
     *
     * <p>{@code lease_until IS NULL} 的历史行不能直接判 false：claim 对它们返回
     * {@code recovery_pending}，若这里也拒绝，任务就永远没有人处理。此时退回
     * heartbeat_at / started_at / createdAt 作为“最后一次已知活动”锚点。</p>
     */
    private boolean isSameRunningAttempt(AnalysisJobEntity job, int attempt, LocalDateTime cutoff) {
        if (job.getStatus() != AnalysisJobEntity.JobStatus.RUNNING) {
            return false;
        }
        if (attemptOf(job) != attempt) {
            return false;
        }
        LocalDateTime anchor = job.getLeaseUntil();
        if (anchor == null) {
            anchor = job.getHeartbeatAt() != null ? job.getHeartbeatAt()
                    : job.getStartedAt() != null ? job.getStartedAt() : job.getCreatedAt();
        }
        if (anchor == null) {
            return false;
        }
        return cutoff == null || anchor.isBefore(cutoff);
    }

    private static int attemptOf(AnalysisJobEntity job) {
        return job.getAttempt() == null ? 0 : job.getAttempt();
    }

    private static int recoveryCountOf(AnalysisJobEntity job) {
        return job.getRecoveryCount() == null ? 0 : job.getRecoveryCount();
    }

    private static String alertKey(String jobId, int attempt) {
        return jobId + "#" + attempt;
    }

    private static String shortSha(JobResultStore.ResultRead read) {
        String sha = read.ref() == null ? null : read.ref().sha256();
        return sha == null ? "?" : sha.substring(0, Math.min(12, sha.length()));
    }

    /** detail 只做单行摘要，防止日志/error_message 被对象内容撑爆。 */
    private static String safeDetail(String detail) {
        if (detail == null || detail.isBlank()) {
            return "unspecified";
        }
        String flat = detail.replaceAll("[\\r\\n]+", " ");
        return truncate(flat, 300);
    }

    private static String truncate(String value, int max) {
        return value.length() <= max ? value : value.substring(0, max);
    }

    private LocalDateTime now() {
        return LocalDateTime.ofInstant(clock.instant(), clock.getZone());
    }
}
