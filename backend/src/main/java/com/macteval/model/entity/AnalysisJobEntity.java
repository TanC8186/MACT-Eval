package com.macteval.model.entity;

import jakarta.persistence.*;
import lombok.Data;
import java.time.LocalDateTime;

@Data
@Entity
@Table(name = "analysis_jobs", indexes = {
    @Index(name = "idx_job_status", columnList = "status"),
    @Index(name = "idx_job_type_status", columnList = "type, status")
})
public class AnalysisJobEntity {
    @Id
    @Column(length = 36)
    private String id;

    @Column(nullable = false, length = 30)
    @Enumerated(EnumType.STRING)
    private JobType type;

    @Column(nullable = false, length = 20)
    @Enumerated(EnumType.STRING)
    private JobStatus status = JobStatus.QUEUED;

    @Column(nullable = false, length = 10)
    @Enumerated(EnumType.STRING)
    private JobPriority priority = JobPriority.NORMAL;

    private Integer progress = 0;

    @Column(name = "source_hash", length = 255)
    private String sourceHash;

    /** 发起任务的教师/管理员 ID；历史任务可为空。 */
    @Column(name = "owner_id", length = 36)
    private String ownerId;

    /** 任务所属课堂；用于按课堂授权与追溯。 */
    @Column(name = "lesson_session_id", length = 36)
    private String lessonSessionId;

    @Column(columnDefinition = "TEXT")
    private String params;

    @Column(name = "result_metadata", columnDefinition = "TEXT")
    private String resultMetadata;

    @Column(name = "error_message", columnDefinition = "TEXT")
    private String errorMessage;

    @Column(name = "retry_count")
    private Integer retryCount = 0;

    /** 执行尝试次数（推理尝试，不含回调重试）。 */
    @Column(nullable = false)
    private Integer attempt = 0;

    /** 执行租约：worker 心跳续租；过期后允许接管。 */
    @Column(name = "lease_until")
    private LocalDateTime leaseUntil;

    @Column(name = "heartbeat_at")
    private LocalDateTime heartbeatAt;

    // === 任务恢复（V28；见 docs/operations/result-delivery-and-recovery-protocol.md §5） ===

    /** RUNNING 过期租约被数据库回收器重排的次数；达到上限后必须 FAILED。 */
    @Column(name = "recovery_count", nullable = false)
    private Integer recoveryCount = 0;

    /** 最近一次恢复探测时间（含结果存储不可达导致的延期）。 */
    @Column(name = "recovery_checked_at")
    private LocalDateTime recoveryCheckedAt;

    /** 非 NULL 表示在此时间之前跳过扫描（结果存储不可达退避）。 */
    @Column(name = "next_recovery_at")
    private LocalDateTime nextRecoveryAt;

    /** 最近一次已持久化结果引用 JSON（bucket/key/sha256/bytes），不含签名 URL。 */
    @Column(name = "last_result_ref", columnDefinition = "TEXT")
    private String lastResultRef;

    // === 业务落库/窗口对齐阶段（R01 / R03） ===

    /**
     * 业务落库（ingest）阶段：null=历史行/不适用；PENDING=原始结果 succeeded
     * 但业务落库失败、等待补偿；INGESTED=落库完成；EXHAUSTED=补偿耗尽。
     *
     * <p>用它区分「落库失败可补偿」与「AI 合法返回 partial/NO_WINDOWS」，
     * 不能只按 status=PARTIAL 判断——后者不欠任何落库工作。</p>
     */
    @Column(name = "ingest_state", length = 20)
    private String ingestState;

    /**
     * 窗口对齐阶段：null=不适用；PENDING=观测已落库、窗口待对齐；
     * DONE=对齐完成；FAILED=对齐补偿耗尽。
     *
     * <p>对齐只重建派生窗口，重试绝不需要重新推理。</p>
     */
    @Column(name = "alignment_state", length = 20)
    private String alignmentState;

    /** 窗口对齐补偿已尝试次数。 */
    @Column(name = "alignment_attempts", nullable = false)
    private Integer alignmentAttempts = 0;

    /** 同一媒体/课堂的幂等键，避免重复提交生成多个有效任务。 */
    @Column(name = "idempotency_key", length = 255)
    private String idempotencyKey;

    /** 实际权重文件的 SHA-256（不是文件名）。 */
    @Column(name = "model_digest", length = 64)
    private String modelDigest;

    /** 推理配置快照（设备、采样间隔、窗口等）。 */
    @Column(name = "config_json", columnDefinition = "TEXT")
    private String configJson;

    @Column(name = "created_at", nullable = false, updatable = false)
    private LocalDateTime createdAt;

    @Column(name = "started_at")
    private LocalDateTime startedAt;

    @Column(name = "completed_at")
    private LocalDateTime completedAt;

    public enum JobType {
        VIDEO_ANALYSIS,
        AUDIO_ANALYSIS,
        REPORT_GENERATION
    }

    public enum JobStatus {
        QUEUED,
        RUNNING,
        PARTIAL,
        SUCCEEDED,
        FAILED,
        CANCELLED
    }

    public enum JobPriority {
        HIGH,
        NORMAL,
        LOW
    }

    @PrePersist
    protected void onCreate() {
        createdAt = LocalDateTime.now();
    }
}
