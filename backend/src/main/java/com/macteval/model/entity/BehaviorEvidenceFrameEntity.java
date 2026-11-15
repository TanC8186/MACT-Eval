package com.macteval.model.entity;

import jakarta.persistence.*;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * 行为识别证据帧：保存可人工复核的画面对象键与标签。
 *
 * <p>对象在私有桶中，只通过带属主校验的签名 URL 暴露给教师。</p>
 */
@Data
@Entity
@Table(name = "behavior_evidence_frames")
public class BehaviorEvidenceFrameEntity {

    @Id
    @Column(length = 36)
    private String id;

    @Column(name = "lesson_session_id", length = 36, nullable = false)
    private String lessonSessionId;

    @Column(name = "job_id", length = 36, nullable = false)
    private String jobId;

    @Column(name = "window_index", nullable = false)
    private Integer windowIndex;

    @Column(name = "timestamp_ms")
    private Long timestampMs;

    @Column(nullable = false, length = 64)
    private String bucket;

    @Column(name = "object_key", nullable = false, length = 255)
    private String objectKey;

    @Column(nullable = false, length = 30)
    private String code;

    @Column(length = 50)
    private String label;

    @Column(columnDefinition = "TEXT")
    private String metadata;

    @Column(name = "created_at", nullable = false, updatable = false)
    private LocalDateTime createdAt;

    @PrePersist
    protected void onCreate() {
        if (createdAt == null) createdAt = LocalDateTime.now();
    }
}
