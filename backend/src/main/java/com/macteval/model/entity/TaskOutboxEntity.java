package com.macteval.model.entity;

import jakarta.persistence.*;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * 事务性 outbox：任务行与待发布事件在同一数据库事务写入，
 * 后台发布器再可靠投递到 Redis Stream（至少一次语义，消费端幂等）。
 */
@Data
@Entity
@Table(name = "task_outbox")
public class TaskOutboxEntity {

    @Id
    @Column(length = 36)
    private String id;

    @Column(name = "aggregate_id", length = 36, nullable = false)
    private String aggregateId;

    @Column(name = "event_type", length = 40, nullable = false)
    private String eventType;

    @Column(nullable = false, columnDefinition = "TEXT")
    private String payload;

    @Column(nullable = false)
    private Integer attempt = 0;

    @Column(name = "next_retry_at", nullable = false)
    private LocalDateTime nextRetryAt;

    @Column(name = "published_at")
    private LocalDateTime publishedAt;

    @Column(name = "last_error", columnDefinition = "TEXT")
    private String lastError;

    @Column(name = "created_at", nullable = false, updatable = false)
    private LocalDateTime createdAt;

    @PrePersist
    protected void onCreate() {
        if (createdAt == null) createdAt = LocalDateTime.now();
        if (nextRetryAt == null) nextRetryAt = LocalDateTime.now();
    }
}
