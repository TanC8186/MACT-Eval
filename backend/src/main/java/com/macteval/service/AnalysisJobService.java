package com.macteval.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.macteval.dto.JobStatusDTO;
import com.macteval.model.entity.AnalysisJobEntity;
import com.macteval.model.entity.TaskOutboxEntity;
import com.macteval.repository.AnalysisJobRepository;
import com.macteval.repository.TaskOutboxRepository;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.data.redis.core.RedisTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;
import java.util.*;

@Slf4j
@Service
@RequiredArgsConstructor
public class AnalysisJobService {

    private final AnalysisJobRepository jobRepository;
    private final TaskOutboxRepository outboxRepository;
    private final RedisTemplate<String, String> redisTemplate;
    private final ObjectMapper objectMapper;
    private final BehaviorService behaviorService;
    private final TranscriptService transcriptService;
    private final WindowAlignmentService windowAlignmentService;

    private static final String JOB_STREAM = "analysis:jobs";

    // 业务落库阶段（R01）：null=历史行/不适用
    static final String INGEST_PENDING = "PENDING";
    static final String INGEST_INGESTED = "INGESTED";
    static final String INGEST_EXHAUSTED = "EXHAUSTED";

    // 窗口对齐阶段（R03）：null=不适用
    static final String ALIGNMENT_PENDING = "PENDING";
    static final String ALIGNMENT_DONE = "DONE";
    static final String ALIGNMENT_FAILED = "FAILED";

    /** 对齐待补偿时写入 errorMessage 的固定前缀，成功补偿后据此清理。 */
    static final String ALIGNMENT_PENDING_PREFIX = "窗口对齐待补偿";

    /**
     * 兼容内部/测试调用：不记录属主与课堂。
     */
    @Transactional
    public AnalysisJobEntity createJob(AnalysisJobEntity.JobType type, Map<String, Object> params, String sourceHash) {
        return createJob(type, params, sourceHash, null, null);
    }

    /**
     * Create an analysis job and push to Redis Stream.
     *
     * @param ownerId         发起任务的教师/管理员；用于任务列表与详情的属主过滤
     * @param lessonSessionId 任务所属课堂；用于按课堂授权与追溯
     */
    @Transactional
    public AnalysisJobEntity createJob(AnalysisJobEntity.JobType type, Map<String, Object> params,
                                       String sourceHash, String ownerId, String lessonSessionId) {
        AnalysisJobEntity job = new AnalysisJobEntity();
        job.setId(UUID.randomUUID().toString());
        job.setType(type);
        job.setStatus(AnalysisJobEntity.JobStatus.QUEUED);
        job.setPriority(AnalysisJobEntity.JobPriority.NORMAL);
        job.setProgress(0);
        // source_hash 列最长 255：objectName 为 UUID_文件名，超长文件名截断兜底
        job.setSourceHash(sourceHash != null && sourceHash.length() > 255
                ? sourceHash.substring(0, 255) : sourceHash);
        job.setRetryCount(0);
        job.setOwnerId(ownerId);
        job.setLessonSessionId(lessonSessionId);

        try {
            job.setParams(objectMapper.writeValueAsString(params));
        } catch (Exception e) {
            job.setParams(params.toString());
        }

        jobRepository.save(job);

        // 同一数据库事务写入 outbox；Redis 暂时不可用不会把任务直接判失败，
        // 后台发布器会退避重试，恢复后任务仍可执行。
        try {
            Map<String, Object> message = new HashMap<>();
            message.put("id", job.getId());
            message.put("type", job.getType().name());
            message.put("priority", job.getPriority().name());
            try {
                @SuppressWarnings("unchecked")
                Map<String, Object> parsedParams = objectMapper.readValue(job.getParams(), Map.class);
                message.putAll(parsedParams);
            } catch (Exception ex) {
                log.warn("Could not flatten params for job {}: {}", job.getId(), ex.getMessage());
            }
            TaskOutboxEntity event = new TaskOutboxEntity();
            event.setId(UUID.randomUUID().toString());
            event.setAggregateId(job.getId());
            event.setEventType("ANALYSIS_JOB_QUEUED");
            event.setPayload(objectMapper.writeValueAsString(message));
            event.setAttempt(0);
            event.setNextRetryAt(LocalDateTime.now());
            outboxRepository.save(event);
            log.info("Job {} queued in outbox", job.getId());
        } catch (Exception e) {
            log.error("Failed to enqueue outbox event for job {}: {}", job.getId(), e.getMessage());
            job.setStatus(AnalysisJobEntity.JobStatus.FAILED);
            job.setErrorMessage("任务入队失败: " + e.getMessage());
            job.setCompletedAt(LocalDateTime.now());
            jobRepository.save(job);
        }

        return job;
    }

    /**
     * Worker 执行前领取任务：原子设置 RUNNING + 租约（协议 §1.1 决策表）。
     *
     * <p>过期 RUNNING（含历史 lease_until 为 NULL 的行）<b>不</b>在此推进 attempt：
     * 旧 attempt 可能已有可交付结果，必须由数据库回收器统一决策，否则会把
     * 已持久化的结果变成永不交付的 stale attempt。
     */
    @Transactional
    public Map<String, Object> claimJob(String jobId, int leaseSeconds) {
        AnalysisJobEntity job = jobRepository.findByIdForUpdate(jobId)
                // 与 applyResult 一致：缺任务映射 404，而不是 500 让 worker 当成后端故障重试。
                .orElseThrow(() -> new NoSuchElementException("任务不存在: " + jobId));
        int currentAttempt = job.getAttempt() == null ? 0 : job.getAttempt();

        if (isTerminal(job.getStatus())) {
            log.info("Job {} claim rejected: terminal ({})", jobId, job.getStatus());
            return Map.of("claimed", false, "reason", "terminal",
                    "status", job.getStatus().name(), "attempt", currentAttempt);
        }
        LocalDateTime now = LocalDateTime.now();
        if (job.getStatus() == AnalysisJobEntity.JobStatus.RUNNING) {
            boolean leased = job.getLeaseUntil() != null && job.getLeaseUntil().isAfter(now);
            if (leased) {
                log.info("Job {} claim rejected: leased until {}", jobId, job.getLeaseUntil());
                return Map.of("claimed", false, "reason", "leased",
                        "status", job.getStatus().name(), "attempt", currentAttempt);
            }
            log.info("Job {} claim rejected: recovery_pending (lease expired at {})",
                    jobId, job.getLeaseUntil());
            return Map.of("claimed", false, "reason", "recovery_pending",
                    "status", job.getStatus().name(), "attempt", currentAttempt);
        }
        if (job.getStatus() != AnalysisJobEntity.JobStatus.QUEUED) {
            log.info("Job {} claim rejected: not_claimable ({})", jobId, job.getStatus());
            return Map.of("claimed", false, "reason", "not_claimable",
                    "status", job.getStatus().name(), "attempt", currentAttempt);
        }

        int attempt = currentAttempt + 1;
        job.setStatus(AnalysisJobEntity.JobStatus.RUNNING);
        job.setAttempt(attempt);
        job.setStartedAt(job.getStartedAt() == null ? now : job.getStartedAt());
        job.setHeartbeatAt(now);
        job.setLeaseUntil(now.plusSeconds(Math.max(30, leaseSeconds)));
        jobRepository.save(job);
        log.info("Job {} claimed by worker (attempt {}, lease {}s)", jobId, attempt, leaseSeconds);
        return Map.of("claimed", true, "status", job.getStatus().name(), "attempt", attempt);
    }

    /** 兼容旧 worker：不携带 attempt 时只续租当前 RUNNING 行。 */
    @Transactional
    public boolean heartbeat(String jobId, int leaseSeconds) {
        return heartbeat(jobId, leaseSeconds, null);
    }

    /**
     * 周期续租；租约过期后其他 worker 才允许接管。
     * 携带 attempt 时，已被新尝试接管的旧 worker 不能再延长租约。
     */
    @Transactional
    public boolean heartbeat(String jobId, int leaseSeconds, Integer expectedAttempt) {
        return (boolean) heartbeatWithReason(jobId, leaseSeconds, expectedAttempt).get("extended");
    }

    /**
     * 心跳的带原因版本：{@code reason} 区分 not_running / attempt_mismatch，
     * 成功时只有 {@code extended=true}。控制器与 Java 调用方共用同一判定。
     */
    @Transactional
    public Map<String, Object> heartbeatWithReason(String jobId, int leaseSeconds, Integer expectedAttempt) {
        AnalysisJobEntity job = jobRepository.findByIdForUpdate(jobId)
                .orElseThrow(() -> new NoSuchElementException("任务不存在: " + jobId));
        if (job.getStatus() != AnalysisJobEntity.JobStatus.RUNNING) {
            return heartbeatRejected("not_running", jobId);
        }
        if (expectedAttempt != null && !expectedAttempt.equals(job.getAttempt())) {
            return heartbeatRejected("attempt_mismatch", jobId);
        }
        if (expectedAttempt == null && job.getAttempt() != null && job.getAttempt() > 1) {
            // After a takeover, only a caller that echoes the current attempt
            // may extend the lease. This closes the legacy fail-open path.
            log.warn("Rejecting attempt-less heartbeat for job {} at attempt {}",
                    jobId, job.getAttempt());
            return heartbeatRejected("attempt_mismatch", jobId);
        }
        LocalDateTime now = LocalDateTime.now();
        job.setHeartbeatAt(now);
        job.setLeaseUntil(now.plusSeconds(Math.max(30, leaseSeconds)));
        jobRepository.save(job);
        return Map.of("extended", true, "attempt", attemptOf(job));
    }

    private static Map<String, Object> heartbeatRejected(String reason, String jobId) {
        log.debug("Heartbeat rejected for job {}: {}", jobId, reason);
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("extended", false);
        payload.put("reason", reason);
        return payload;
    }

    /**
     * Worker 完成回调入口：解析 attempt 后交给统一入口 {@link #applyResult}。
     *
     * <p>兼容旧 worker 的受控例外：请求体缺少 attempt 键（不是显式 null）且
     * 当前 attempt ≤ 1 且状态为 RUNNING 时，使用 job.attempt；已接管（attempt &gt; 1）
     * 一律 stale_attempt。
     */
    @Transactional
    public Map<String, Object> completeJob(String jobId, Map<String, Object> result) {
        Map<String, Object> safeResult = result == null ? Map.of() : result;
        ParsedAttempt parsed = parseAttemptField(safeResult);
        if (parsed.present() && parsed.valid()) {
            return applyResult(jobId, parsed.value(), safeResult);
        }
        // 缺键（受控兼容例外）或非法值仍需区分，走同一套完成判定。
        return applyResultInternal(jobId, parsed, safeResult);
    }

    /**
     * 统一完成入口（协议 §1.2）：HTTP 回调与数据库恢复器必须共用本方法。
     *
     * <p>判定顺序：不存在 → 终态 already_terminal → 非 RUNNING not_running →
     * attempt 不匹配 stale_attempt → 落库并 ingest。
     * 终态分支只读不写，保证重复交付不改写业务数据。
     */
    @Transactional
    public Map<String, Object> applyResult(String jobId, int attempt, Map<String, Object> result) {
        return applyResultInternal(jobId, ParsedAttempt.of(attempt),
                result == null ? Map.of() : result);
    }

    private Map<String, Object> applyResultInternal(String jobId, ParsedAttempt parsed,
                                                    Map<String, Object> result) {
        // 行锁串行化同一任务的并发回调；第二个回调会在第一个提交后看到终态并直接返回。
        AnalysisJobEntity job = jobRepository.findByIdForUpdate(jobId)
                .orElseThrow(() -> new NoSuchElementException("任务不存在: " + jobId));
        int currentAttempt = attemptOf(job);

        if (isTerminal(job.getStatus())) {
            log.info("Job {} already in terminal state {}; ignoring duplicate callback ({})",
                    jobId, job.getStatus(), parsed.present() ? "attempt " + parsed.value() : "no attempt");
            return Map.of("applied", false, "reason", "already_terminal",
                    "status", job.getStatus().name(), "attempt", currentAttempt);
        }
        if (job.getStatus() != AnalysisJobEntity.JobStatus.RUNNING) {
            log.warn("Rejecting result for non-running job {} (status {}, attempt {})",
                    jobId, job.getStatus(), parsed.present() ? parsed.value() : -1);
            return Map.of("applied", false, "reason", "not_running",
                    "status", job.getStatus().name(), "attempt", currentAttempt);
        }
        if (parsed.present() && !parsed.valid()) {
            // 显式提供但非法（小数/布尔/负数/非数字/越界/null）：不落库、不 ingest。
            log.warn("Rejecting invalid attempt for job {}", jobId);
            return rejected("invalid_attempt", job);
        }
        int effectiveAttempt;
        if (!parsed.present()) {
            // 受控兼容例外：旧 worker 不回声 attempt，只允许在首次领取（attempt ≤ 1）时接受。
            if (currentAttempt > 1) {
                // An updated worker always echoes the claim attempt. After a
                // takeover, an attempt-less callback can only be a stale worker.
                log.warn("Rejecting attempt-less callback for job {} after takeover (current {})",
                        jobId, currentAttempt);
                return Map.of("applied", false, "reason", "stale_attempt",
                        "status", job.getStatus().name(), "attempt", currentAttempt);
            }
            effectiveAttempt = currentAttempt;
        } else {
            if (parsed.value() != currentAttempt) {
                // 旧 worker 的迟到回调不能覆盖已被接管后的新尝试。
                log.warn("Ignoring stale callback for job {} (attempt {} != current {})",
                        jobId, parsed.value(), currentAttempt);
                return Map.of("applied", false, "reason", "stale_attempt",
                        "status", job.getStatus().name(), "attempt", currentAttempt);
            }
            effectiveAttempt = parsed.value();
        }
        log.info("Applying result for job {} attempt {}", jobId, effectiveAttempt);

        // 缺 status 不得默认成功（fail-open）：旧实现把 {"error": "..."} 也推进为
        // SUCCEEDED 并 ingest。现在只接受显式 status；缺失时若带 error 视为失败，
        // 否则拒绝落库（不 ACK、不 ingest）。
        Object rawStatus = result.get("status");
        boolean hasError = result.get("error") != null
                && !String.valueOf(result.get("error")).isBlank();
        if (rawStatus == null && !hasError) {
            log.warn("Rejecting completion for job {} without a status field", jobId);
            return rejected("invalid_status", job);
        }
        String statusStr = rawStatus == null ? "failed" : String.valueOf(rawStatus);
        if ("succeeded".equalsIgnoreCase(statusStr)) {
            job.setStatus(AnalysisJobEntity.JobStatus.SUCCEEDED);
        } else if ("partial".equalsIgnoreCase(statusStr)) {
            job.setStatus(AnalysisJobEntity.JobStatus.PARTIAL);
        } else {
            job.setStatus(AnalysisJobEntity.JobStatus.FAILED);
        }

        job.setProgress(100);
        job.setCompletedAt(LocalDateTime.now());

        try {
            job.setResultMetadata(objectMapper.writeValueAsString(result));
        } catch (Exception e) {
            job.setResultMetadata(result.toString());
        }

        Object error = result.get("error");
        if (error != null && !error.toString().isBlank()) {
            job.setErrorMessage(String.valueOf(error));
        }
        // 媒体内容摘要与模型身份随结果一起存档；source_hash 不再是 objectName。
        Object mediaSha = result.get("media_sha256");
        if (mediaSha != null && !String.valueOf(mediaSha).isBlank()) {
            String digest = "sha256:" + mediaSha;
            job.setSourceHash(digest.length() > 255 ? digest.substring(0, 255) : digest);
        }
        Object modelDigest = result.get("model_digest");
        if (modelDigest != null && !String.valueOf(modelDigest).isBlank()) {
            String value = String.valueOf(modelDigest);
            job.setModelDigest(value.length() > 64 ? value.substring(0, 64) : value);
        }
        Object config = result.get("config");
        if (config != null) {
            try {
                job.setConfigJson(objectMapper.writeValueAsString(config));
            } catch (Exception e) {
                log.debug("Could not serialize job config for {}", jobId);
            }
        }

        // 只有「原始结果成功」才需要业务落库。AI 合法返回的 partial/NO_WINDOWS
        // 同样是 PARTIAL，但那是「分析不完整」而不是「落库失败」，不欠任何重放工作，
        // 必须原样保留状态与错误原因（R01）。
        if (job.getStatus() == AnalysisJobEntity.JobStatus.SUCCEEDED) {
            applyIngestOutcome(job, ingestBusinessData(job));
        }

        jobRepository.save(job);
        return Map.of("applied", true, "status", job.getStatus().name(),
                "attempt", job.getAttempt() == null ? 0 : job.getAttempt());
    }

    /** 业务落库的三个阶段（R01/R03）。 */
    public enum IngestOutcome {
        /** 观测/转写落库失败：需要走 ingest 补偿（PARTIAL + ingest_state=PENDING）。 */
        FAILED,
        /** 观测已落库，且窗口对齐完成。 */
        STORED_ALIGNED,
        /** 观测已落库，但窗口对齐失败；等待只重建派生窗口的补偿。 */
        STORED_ALIGN_PENDING
    }

    /**
     * 业务落库（ingest + 窗口对齐）。
     *
     * <p>被 {@link #applyResult} 与 {@link #reingestStoredResult} 共用：补偿路径必须
     * 与首次交付走完全相同的逻辑，否则两条路径会产出不一致的业务数据。</p>
     *
     * <p>返回类型显式区分「观测没落库」与「观测已落库但对齐待补偿」：旧实现把
     * 对齐异常吞掉后仍返回成功，任务显示成功而报告窗口没更新（R03）。</p>
     *
     * <p>调用方负责前置状态判断（只有 SUCCEEDED 的任务才需要 ingest）。</p>
     */
    private IngestOutcome ingestBusinessData(AnalysisJobEntity job) {
        try {
            if (job.getType() == AnalysisJobEntity.JobType.VIDEO_ANALYSIS) {
                behaviorService.ingestCompletedVideoJob(job);
            } else if (job.getType() == AnalysisJobEntity.JobType.AUDIO_ANALYSIS) {
                transcriptService.ingestCompletedAudioJob(job);
            } else {
                return IngestOutcome.STORED_ALIGNED;
            }
        } catch (Exception e) {
            log.error("Job ingest failed for job {}: {}", job.getId(), e.getMessage(), e);
            // 不能继续显示 SUCCEEDED，否则报告会误判“有数据”
            if (job.getErrorMessage() == null || job.getErrorMessage().isBlank()) {
                job.setErrorMessage("结果落库失败: " + e.getMessage());
            }
            return IngestOutcome.FAILED;
        }
        // 观测已提交：对齐失败不再被吞掉，改为留下「观测已落库、窗口待对齐」的阶段标记。
        String alignmentFailure = alignCommittedObservations(job);
        if (alignmentFailure != null) {
            if (job.getErrorMessage() == null || job.getErrorMessage().isBlank()) {
                job.setErrorMessage(ALIGNMENT_PENDING_PREFIX + ": " + alignmentFailure);
            }
            return IngestOutcome.STORED_ALIGN_PENDING;
        }
        return IngestOutcome.STORED_ALIGNED;
    }

    /**
     * 只重建派生窗口特征：用既有 lessonSessionId 与 jobId 调对齐，<b>绝不重新推理</b>。
     *
     * @return 成功返回 null；失败返回真实原因（写入 errorMessage 供人工定位）
     */
    private String alignCommittedObservations(AnalysisJobEntity job) {
        Map<String, Object> params = parseJson(job.getParams());
        Object sid = params.get("lessonSessionId");
        if (sid == null) {
            // 无课堂归属的旧任务没有可对齐目标，不算失败。
            return null;
        }
        try {
            windowAlignmentService.alignLesson(
                    String.valueOf(sid),
                    job.getType() == AnalysisJobEntity.JobType.VIDEO_ANALYSIS ? job.getId() : null,
                    job.getType() == AnalysisJobEntity.JobType.AUDIO_ANALYSIS ? job.getId() : null);
            return null;
        } catch (Exception e) {
            log.warn("Window alignment failed for job {}: {}", job.getId(), e.getMessage(), e);
            return e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage();
        }
    }

    /** 把 ingest 结果落到任务字段：状态、阶段标志、重试时间与原因。 */
    private void applyIngestOutcome(AnalysisJobEntity job, IngestOutcome outcome) {
        switch (outcome) {
            case FAILED -> {
                // 原始分析成功但业务落库失败：降为 PARTIAL 并标记「待补偿」，
                // 由 JobRecoveryService 的 PARTIAL 扫描重放 ingest（协议 §1.2）。
                job.setStatus(AnalysisJobEntity.JobStatus.PARTIAL);
                job.setIngestState(INGEST_PENDING);
                job.setNextRecoveryAt(LocalDateTime.now());
            }
            case STORED_ALIGNED -> {
                job.setIngestState(INGEST_INGESTED);
                job.setAlignmentState(ALIGNMENT_DONE);
                job.setNextRecoveryAt(null);
            }
            case STORED_ALIGN_PENDING -> {
                job.setIngestState(INGEST_INGESTED);
                markAlignmentPending(job, LocalDateTime.now());
            }
        }
    }

    /**
     * 标记「观测已落库、窗口待对齐」。任务状态保持 SUCCEEDED
     * （报告需要 analysisVideoJobId 指向它），靠 alignment_state 让恢复器发现。
     */
    private void markAlignmentPending(AnalysisJobEntity job, LocalDateTime nextRecoveryAt) {
        job.setAlignmentState(ALIGNMENT_PENDING);
        job.setNextRecoveryAt(nextRecoveryAt);
        if (job.getErrorMessage() == null || job.getErrorMessage().isBlank()) {
            job.setErrorMessage(ALIGNMENT_PENDING_PREFIX + ": 观测已落库，窗口特征尚未重建");
        }
    }

    /** PARTIAL 补偿的结果。 */
    public enum ReingestOutcome {
        /** 已重新落库，状态推进为 SUCCEEDED。 */
        REINGESTED,
        /** 仍失败，已写入退避时间与次数。 */
        FAILED,
        /** 次数耗尽，已转 FAILED 等人工处理。 */
        EXHAUSTED,
        /** 不适用（非 PARTIAL / 缺少结果 / 类型不在白名单 / 任务已消失）。 */
        NOT_APPLICABLE
    }

    /**
     * 对 {@code PARTIAL} 任务重放业务落库（独立审查 A 的 F2 补偿路径）。
     *
     * <p>只处理「结果已在 resultMetadata 中、原始结果 succeeded、但 ingest 曾失败」的
     * VIDEO/AUDIO 任务：{@code ingest_state} 必须是 {@code PENDING}。AI 合法返回的
     * {@code partial/NO_WINDOWS} 与落库失败共用 PARTIAL 状态，但只有后者欠重放；
     * 用显式阶段标志分开，绝不把合法 partial 升成 SUCCEEDED（R01）。</p>
     *
     * <p>幂等性由 {@code BehaviorService}（jobId+windowIndex 幂等键、重放前按 job 清空）
     * 与 {@code TranscriptService}/{@code WindowAlignmentService} 的既有语义保证。</p>
     */
    @Transactional
    public ReingestOutcome reingestStoredResult(String jobId, int maxAttempts, long backoffSeconds) {
        AnalysisJobEntity job = jobRepository.findByIdForUpdate(jobId)
                .orElse(null);
        if (job == null) {
            return ReingestOutcome.NOT_APPLICABLE;
        }
        if (job.getStatus() != AnalysisJobEntity.JobStatus.PARTIAL) {
            return ReingestOutcome.NOT_APPLICABLE;
        }
        // 只有「落库失败待补偿」的行才重放；合法 partial/NO_WINDOWS 的 ingest_state 为 null。
        if (!INGEST_PENDING.equals(job.getIngestState())) {
            return ReingestOutcome.NOT_APPLICABLE;
        }
        if (job.getType() != AnalysisJobEntity.JobType.VIDEO_ANALYSIS
                && job.getType() != AnalysisJobEntity.JobType.AUDIO_ANALYSIS) {
            return ReingestOutcome.NOT_APPLICABLE;
        }
        if (job.getResultMetadata() == null || job.getResultMetadata().isBlank()) {
            return ReingestOutcome.NOT_APPLICABLE;
        }
        // 防御性校验：原始结果必须是 succeeded。记录不可信时既不落库也不改任何字段，
        // 否则一条 status=partial 的结果会被“补偿”成成功。
        if (!originalResultSucceeded(job.getResultMetadata())) {
            log.warn("Refusing to reingest job {}: stored result is not succeeded", jobId);
            return ReingestOutcome.NOT_APPLICABLE;
        }
        int attempt = attemptOf(job);
        if (attempt < 1) {
            return ReingestOutcome.NOT_APPLICABLE;
        }
        job.setStatus(AnalysisJobEntity.JobStatus.SUCCEEDED);
        IngestOutcome outcome = ingestBusinessData(job);
        if (outcome != IngestOutcome.FAILED) {
            job.setIngestState(INGEST_INGESTED);
            job.setErrorMessage(null);
            if (outcome == IngestOutcome.STORED_ALIGN_PENDING) {
                markAlignmentPending(job, LocalDateTime.now());
            } else {
                job.setAlignmentState(ALIGNMENT_DONE);
                job.setNextRecoveryAt(null);
            }
            jobRepository.save(job);
            log.info("Reingested business data for PARTIAL job {} attempt {}", jobId, attempt);
            return ReingestOutcome.REINGESTED;
        }
        int nextCount = (job.getRecoveryCount() == null ? 0 : job.getRecoveryCount()) + 1;
        job.setRecoveryCount(nextCount);
        job.setRecoveryCheckedAt(LocalDateTime.now());
        if (nextCount >= Math.max(1, maxAttempts)) {
            job.setStatus(AnalysisJobEntity.JobStatus.FAILED);
            job.setIngestState(INGEST_EXHAUSTED);
            job.setNextRecoveryAt(null);
            job.setErrorMessage("ingest recovery exhausted (" + nextCount + ")");
            jobRepository.save(job);
            log.error("Job {} attempt {} exhausted ingest recovery ({}); marked FAILED",
                    jobId, attempt, nextCount);
            return ReingestOutcome.EXHAUSTED;
        }
        job.setStatus(AnalysisJobEntity.JobStatus.PARTIAL);
        job.setNextRecoveryAt(LocalDateTime.now().plusSeconds(Math.max(1, backoffSeconds)));
        jobRepository.save(job);
        log.warn("Ingest recovery failed for job {} attempt {} ({}/{}); retry later",
                jobId, attempt, nextCount, maxAttempts);
        return ReingestOutcome.FAILED;
    }

    /** 窗口对齐补偿的结果（R03）。 */
    public enum RealignOutcome {
        /** 已重建窗口特征，alignment_state=DONE。 */
        REALIGNED,
        /** 仍失败，已按退避设置下次重试时间。 */
        FAILED,
        /** 次数耗尽，alignment_state=FAILED 并写入真实原因。 */
        EXHAUSTED,
        /** 不适用（非 SUCCEEDED / 对齐不处于 PENDING / 类型不支持 / 任务已消失）。 */
        NOT_APPLICABLE
    }

    /**
     * 重建「观测已落库、窗口待对齐」任务的派生窗口（R03）。
     *
     * <p>只调用 {@link WindowAlignmentService#alignLesson} 重建窗口特征，用的是
     * job 已有的 lessonSessionId 与 jobId——<b>绝不重新推理</b>，因此不会覆盖已落库的
     * 观测/转写，也不会产生第二次 AI 计算。</p>
     */
    @Transactional
    public RealignOutcome realignStoredResult(String jobId, int maxAttempts, long backoffSeconds) {
        AnalysisJobEntity job = jobRepository.findByIdForUpdate(jobId).orElse(null);
        if (job == null) {
            return RealignOutcome.NOT_APPLICABLE;
        }
        if (job.getStatus() != AnalysisJobEntity.JobStatus.SUCCEEDED) {
            return RealignOutcome.NOT_APPLICABLE;
        }
        if (!ALIGNMENT_PENDING.equals(job.getAlignmentState())) {
            return RealignOutcome.NOT_APPLICABLE;
        }
        if (job.getType() != AnalysisJobEntity.JobType.VIDEO_ANALYSIS
                && job.getType() != AnalysisJobEntity.JobType.AUDIO_ANALYSIS) {
            return RealignOutcome.NOT_APPLICABLE;
        }
        int attempt = attemptOf(job);
        int nextCount = (job.getAlignmentAttempts() == null ? 0 : job.getAlignmentAttempts()) + 1;
        job.setAlignmentAttempts(nextCount);
        job.setRecoveryCheckedAt(LocalDateTime.now());
        String failure = alignCommittedObservations(job);
        if (failure == null) {
            job.setAlignmentState(ALIGNMENT_DONE);
            job.setNextRecoveryAt(null);
            if (job.getErrorMessage() != null
                    && job.getErrorMessage().startsWith(ALIGNMENT_PENDING_PREFIX)) {
                job.setErrorMessage(null);
            }
            jobRepository.save(job);
            log.info("Realigned windows for job {} attempt {} (alignment #{})",
                    jobId, attempt, nextCount);
            return RealignOutcome.REALIGNED;
        }
        if (nextCount >= Math.max(1, maxAttempts)) {
            job.setAlignmentState(ALIGNMENT_FAILED);
            job.setNextRecoveryAt(null);
            job.setErrorMessage("window alignment recovery exhausted (" + nextCount + "): " + failure);
            jobRepository.save(job);
            log.error("Job {} attempt {} exhausted window alignment ({}); alignment FAILED",
                    jobId, attempt, nextCount);
            return RealignOutcome.EXHAUSTED;
        }
        LocalDateTime nextAt = LocalDateTime.now().plusSeconds(Math.max(1, backoffSeconds));
        job.setErrorMessage(ALIGNMENT_PENDING_PREFIX + ": " + failure);
        markAlignmentPending(job, nextAt);
        jobRepository.save(job);
        log.warn("Window alignment retry failed for job {} attempt {} ({}/{}); next check at {}",
                jobId, attempt, nextCount, maxAttempts, nextAt);
        return RealignOutcome.FAILED;
    }

    /** 原始结果状态是否为 succeeded（不可解析视为不可信）。 */
    @SuppressWarnings("unchecked")
    private boolean originalResultSucceeded(String metadata) {
        try {
            Map<String, Object> parsed = objectMapper.readValue(metadata, Map.class);
            Object status = parsed.get("status");
            return status != null && "succeeded".equalsIgnoreCase(String.valueOf(status));
        } catch (Exception e) {
            return false;
        }
    }

    private static int attemptOf(AnalysisJobEntity job) {
        return job.getAttempt() == null ? 0 : job.getAttempt();
    }

    private static Map<String, Object> rejected(String reason, AnalysisJobEntity job) {
        return Map.of("applied", false, "reason", reason,
                "status", job.getStatus().name(), "attempt", attemptOf(job));
    }

    /**
     * 解析完成回调的 attempt 键（协议 §1.3）。
     *
     * <p>{@code present} 表示请求体中存在该键；{@code valid} 表示其值可当作
     * 非负 int 使用。缺键不是 null：缺键由调用方走受控兼容例外。
     */
    public record ParsedAttempt(boolean present, boolean valid, int value) {
        static ParsedAttempt absent() {
            return new ParsedAttempt(false, false, 0);
        }

        static ParsedAttempt invalid() {
            return new ParsedAttempt(true, false, 0);
        }

        static ParsedAttempt of(int value) {
            return new ParsedAttempt(true, true, value);
        }
    }

    /** 从结果回调体解析 attempt；缺键返回 present=false。 */
    public static ParsedAttempt parseAttemptField(Map<String, Object> body) {
        if (body == null || !body.containsKey("attempt")) {
            return ParsedAttempt.absent();
        }
        return parseAttempt(body.get("attempt"));
    }

    /**
     * 严格 attempt 解析：只接受 JSON 整数类型与纯数字字符串。
     * 拒绝浮点（含 2.0）、BigDecimal、布尔、负数、非数字、超 int 范围与 null。
     */
    public static ParsedAttempt parseAttempt(Object raw) {
        if (raw instanceof Integer || raw instanceof Short || raw instanceof Byte) {
            int value = ((Number) raw).intValue();
            return value < 0 ? ParsedAttempt.invalid() : ParsedAttempt.of(value);
        }
        if (raw instanceof Long l) {
            if (l < 0 || l > Integer.MAX_VALUE) {
                return ParsedAttempt.invalid();
            }
            return ParsedAttempt.of(l.intValue());
        }
        if (raw instanceof String text) {
            String trimmed = text.trim();
            if (!trimmed.matches("\\d+")) {
                return ParsedAttempt.invalid();
            }
            try {
                return ParsedAttempt.of(Integer.parseInt(trimmed));
            } catch (NumberFormatException e) {
                return ParsedAttempt.invalid();
            }
        }
        // Float/Double/BigDecimal/Boolean/其它类型一律拒绝；键存在但值为 null
        // 同样拒绝（parseAttempt 只会由 present 的键调用）。
        return ParsedAttempt.invalid();
    }

    private static boolean isTerminal(AnalysisJobEntity.JobStatus status) {
        return isTerminalStatus(status);
    }

    /** 终态判定（供内网状态端点复用）：SUCCEEDED/PARTIAL/FAILED/CANCELLED。 */
    public static boolean isTerminalStatus(AnalysisJobEntity.JobStatus status) {
        return status == AnalysisJobEntity.JobStatus.SUCCEEDED
                || status == AnalysisJobEntity.JobStatus.PARTIAL
                || status == AnalysisJobEntity.JobStatus.FAILED
                || status == AnalysisJobEntity.JobStatus.CANCELLED;
    }

    /**
     * 按 id 读取任务（内网状态端点用）；不存在抛 {@link java.util.NoSuchElementException}，
     * 由调用方映射 404，而不是让空指针变成 500。
     */
    @Transactional(readOnly = true)
    public AnalysisJobEntity findJob(String jobId) {
        return jobRepository.findById(jobId)
                .orElseThrow(() -> new java.util.NoSuchElementException("任务不存在: " + jobId));
    }

    /**
     * Get job status (internal/system call — no ownership check).
     */
    @Transactional(readOnly = true)
    public JobStatusDTO getJobStatus(String jobId) {
        AnalysisJobEntity job = jobRepository.findById(jobId)
                .orElseThrow(() -> new RuntimeException("任务不存在"));
        return toStatusDTO(job);
    }

    /**
     * Get job status scoped to the authenticated user.
     * Admin may read any job; teachers only their own; legacy jobs without an
     * owner are visible to admins only.
     */
    @Transactional(readOnly = true)
    public JobStatusDTO getJobStatus(String jobId, String userId, boolean isAdmin) {
        AnalysisJobEntity job = jobRepository.findById(jobId)
                .orElseThrow(() -> new java.util.NoSuchElementException("任务不存在"));
        if (!isAdmin && (job.getOwnerId() == null || !job.getOwnerId().equals(userId))) {
            throw new org.springframework.security.access.AccessDeniedException("无权访问其他用户的任务");
        }
        return toStatusDTO(job);
    }

    /**
     * List recent jobs within the caller's authorization scope.
     */
    @Transactional(readOnly = true)
    public List<JobStatusDTO> listJobs(String userId, boolean isAdmin,
                                       int limit, AnalysisJobEntity.JobStatus status) {
        int pageSize = Math.max(1, Math.min(limit, 100));
        org.springframework.data.domain.Pageable pageable =
                org.springframework.data.domain.PageRequest.of(0, pageSize);
        List<AnalysisJobEntity> jobs;
        if (isAdmin) {
            jobs = status != null
                    ? jobRepository.findByStatusOrderByCreatedAtDesc(status, pageable)
                    : jobRepository.findAllByOrderByCreatedAtDesc(pageable);
        } else if (userId == null) {
            return List.of();
        } else {
            jobs = status != null
                    ? jobRepository.findByOwnerIdAndStatusOrderByCreatedAtDesc(userId, status, pageable)
                    : jobRepository.findByOwnerIdOrderByCreatedAtDesc(userId, pageable);
        }
        return jobs.stream()
                .map(this::toStatusDTO)
                .toList();
    }

    /**
     * Get pending jobs for AI worker to process.
     */
    @Transactional(readOnly = true)
    public List<AnalysisJobEntity> getPendingJobs(AnalysisJobEntity.JobType type, int limit) {
        return jobRepository.findByTypeAndStatusOrderByCreatedAtAsc(type, AnalysisJobEntity.JobStatus.QUEUED)
                .stream().limit(limit).toList();
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> parseJson(String json) {
        if (json == null || json.isBlank()) return Map.of();
        try {
            return objectMapper.readValue(json, Map.class);
        } catch (Exception e) {
            return Map.of();
        }
    }

    private JobStatusDTO toStatusDTO(AnalysisJobEntity job) {
        return new JobStatusDTO(
                job.getId(), job.getType().name(), job.getStatus().name(),
                job.getProgress() != null ? job.getProgress() : 0,
                job.getResultMetadata(), job.getErrorMessage(),
                job.getCreatedAt(), job.getCompletedAt()
        );
    }
}
