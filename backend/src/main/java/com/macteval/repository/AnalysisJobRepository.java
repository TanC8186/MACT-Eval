package com.macteval.repository;

import com.macteval.model.entity.AnalysisJobEntity;
import jakarta.persistence.LockModeType;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import org.springframework.stereotype.Repository;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Optional;

@Repository
public interface AnalysisJobRepository extends JpaRepository<AnalysisJobEntity, String> {
    List<AnalysisJobEntity> findByStatus(AnalysisJobEntity.JobStatus status);
    List<AnalysisJobEntity> findByTypeAndStatus(AnalysisJobEntity.JobType type, AnalysisJobEntity.JobStatus status);
    List<AnalysisJobEntity> findByTypeAndStatusOrderByCreatedAtAsc(AnalysisJobEntity.JobType type, AnalysisJobEntity.JobStatus status);
    List<AnalysisJobEntity> findAllByOrderByCreatedAtDesc(Pageable pageable);
    List<AnalysisJobEntity> findByStatusOrderByCreatedAtDesc(AnalysisJobEntity.JobStatus status, Pageable pageable);

    List<AnalysisJobEntity> findByOwnerIdOrderByCreatedAtDesc(String ownerId, Pageable pageable);
    List<AnalysisJobEntity> findByOwnerIdAndStatusOrderByCreatedAtDesc(
            String ownerId, AnalysisJobEntity.JobStatus status, Pageable pageable);

    Optional<AnalysisJobEntity> findFirstByTypeAndLessonSessionIdAndStatusOrderByCompletedAtDesc(
            AnalysisJobEntity.JobType type, String lessonSessionId, AnalysisJobEntity.JobStatus status);

    List<AnalysisJobEntity> findByLessonSessionIdAndTypeAndStatusOrderByCompletedAtDesc(
            String lessonSessionId, AnalysisJobEntity.JobType type, AnalysisJobEntity.JobStatus status);

    Optional<AnalysisJobEntity> findByIdempotencyKey(String idempotencyKey);

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select j from AnalysisJobEntity j where j.id = :id")
    Optional<AnalysisJobEntity> findByIdForUpdate(@Param("id") String id);

    /**
     * 恢复器候选：RUNNING 且租约已过期。按租约（缺失时退回心跳/开始时间）升序分页。
     *
     * <p>必须包含 {@code lease_until IS NULL} 的历史行：claim 对这类行返回
     * {@code recovery_pending}（协议禁止在那里推进 attempt），如果回收器又不选它们，
     * 任务会永久停在 RUNNING——两端都不处理就是死锁。锚点按
     * heartbeat_at / started_at / created_at 依次回退，保证 NULL 租约也有可比较时间。</p>
     *
     * <p>同时排除 next_recovery_at 未到期的行（结果存储不可达时的退避）。</p>
     */
    @Query("select j from AnalysisJobEntity j where j.status = "
            + "com.macteval.model.entity.AnalysisJobEntity.JobStatus.RUNNING "
            + "and ((j.leaseUntil is not null and j.leaseUntil < :leaseCutoff) "
            + "     or (j.leaseUntil is null "
            + "         and coalesce(j.heartbeatAt, j.startedAt, j.createdAt) < :leaseCutoff)) "
            + "and (j.nextRecoveryAt is null or j.nextRecoveryAt <= :now) "
            + "order by j.leaseUntil asc")
    List<AnalysisJobEntity> findRecoverable(@Param("leaseCutoff") LocalDateTime leaseCutoff,
                                            @Param("now") LocalDateTime now,
                                            Pageable pageable);

    /**
     * 条件重置（协议 §5.3）：仅当仍是同一 RUNNING + attempt 且租约仍过期时生效。
     *
     * @return 受影响行数；0 表示期间被心跳续租/完成/取消/其他扫描器处理，调用方应跳过。
     */
    @Modifying
    @Query("update AnalysisJobEntity j set "
            + "j.status = com.macteval.model.entity.AnalysisJobEntity.JobStatus.QUEUED, "
            + "j.leaseUntil = null, j.heartbeatAt = null, j.recoveryCheckedAt = :now "
            + "where j.id = :id "
            + "and j.status = com.macteval.model.entity.AnalysisJobEntity.JobStatus.RUNNING "
            + "and j.attempt = :attempt "
            + "and (j.leaseUntil is null or j.leaseUntil < :cutoff)")
    int requeueExpired(@Param("id") String id,
                       @Param("attempt") Integer attempt,
                       @Param("cutoff") LocalDateTime cutoff,
                       @Param("now") LocalDateTime now);

    /**
     * 补偿候选：ingest 曾失败的 {@code PARTIAL} 任务，且已到重试时间。
     * 只选有结果元数据、attempt ≥ 1 的行（没有结果就无从重放）。
     *
     * <p>必须同时要求 {@code ingest_state = 'PENDING'}：AI 合法返回的
     * {@code partial/NO_WINDOWS} 同样是 PARTIAL，但那是“分析不完整”而不是
     * “落库失败”，不欠任何重放工作，不能被补偿器升成 SUCCEEDED。</p>
     */
    @Query("select j from AnalysisJobEntity j where j.status = "
            + "com.macteval.model.entity.AnalysisJobEntity.JobStatus.PARTIAL "
            + "and j.attempt >= 1 "
            + "and j.ingestState = 'PENDING' "
            + "and j.resultMetadata is not null "
            + "and (j.nextRecoveryAt is null or j.nextRecoveryAt <= :now) "
            + "order by j.completedAt asc")
    List<AnalysisJobEntity> findReingestCandidates(@Param("now") LocalDateTime now,
                                                   Pageable pageable);

    /**
     * 窗口对齐补偿候选：观测已落库但对齐失败（R03）。
     *
     * <p>任务保持 SUCCEEDED（报告要引用它的 analysisVideoJobId），因此不能靠
     * status 发现；只有 {@code alignment_state = 'PENDING'} 才能让恢复器看到它。
     * 重试只重建派生窗口，不需要重新推理。</p>
     */
    @Query("select j from AnalysisJobEntity j where j.status = "
            + "com.macteval.model.entity.AnalysisJobEntity.JobStatus.SUCCEEDED "
            + "and j.alignmentState = 'PENDING' "
            + "and (j.nextRecoveryAt is null or j.nextRecoveryAt <= :now) "
            + "order by j.completedAt asc")
    List<AnalysisJobEntity> findRealignCandidates(@Param("now") LocalDateTime now,
                                                  Pageable pageable);

    /** 告警统计：早已开始但（仍）未完成的 RUNNING 任务。 */
    @Query("select j from AnalysisJobEntity j where j.status = "
            + "com.macteval.model.entity.AnalysisJobEntity.JobStatus.RUNNING "
            + "and j.startedAt is not null and j.startedAt < :startedBefore "
            + "order by j.startedAt asc")
    List<AnalysisJobEntity> findRunningWithoutProgress(@Param("startedBefore") LocalDateTime startedBefore,
                                                       Pageable pageable);
}
