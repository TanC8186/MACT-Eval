package com.macteval.repository;

import com.macteval.model.entity.TaskOutboxEntity;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.stereotype.Repository;

import java.time.LocalDateTime;
import java.util.List;

@Repository
public interface TaskOutboxRepository extends JpaRepository<TaskOutboxEntity, String> {

    /**
     * 某任务的全部 outbox 事件（按创建时间升序）。
     *
     * <p>恢复器把过期 RUNNING 任务重排为 QUEUED 时，必须在同一事务内重置该任务的
     * 投递事件：仅有状态变更而没有可投递事件，任务会停在 QUEUED 永远不被领取。
     */
    List<TaskOutboxEntity> findByAggregateIdOrderByCreatedAtAsc(String aggregateId);

    @Query("select o from TaskOutboxEntity o where o.publishedAt is null "
            + "and o.nextRetryAt <= :now order by o.createdAt asc")
    List<TaskOutboxEntity> findPending(LocalDateTime now, Pageable pageable);

    /**
     * Published event whose job is still QUEUED. This is the safety net for a
     * Redis stream/group lost after XADD: published_at alone cannot prove that
     * a consumer ever received the entry, so the event is eligible to be
     * republished after a grace period. Duplicates are safe because claim and
     * ingest are idempotent.
     */
    @Query(value = "select o.* from task_outbox o "
            + "join analysis_jobs j on j.id = o.aggregate_id "
            + "where o.published_at is not null and o.published_at < :cutoff "
            + "and j.status = 'QUEUED' order by o.published_at asc",
            nativeQuery = true)
    List<TaskOutboxEntity> findStalePublishedForQueuedJobs(
            LocalDateTime cutoff, Pageable pageable);
}
