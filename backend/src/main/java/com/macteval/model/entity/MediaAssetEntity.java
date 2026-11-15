package com.macteval.model.entity;

import jakarta.persistence.*;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * 媒体资产：把“谁上传的、属于哪节课、对象键、大小/摘要、保留期限”绑定在一起。
 *
 * <p>两段式上传的第一段创建 PENDING 资产；确认时只允许资产属主使用同一
 * objectKey，避免知道他人 objectName 的教师复用对象。</p>
 */
@Data
@Entity
@Table(name = "media_assets")
public class MediaAssetEntity {

    @Id
    @Column(length = 36)
    private String id;

    @Column(name = "owner_id", length = 36, nullable = false)
    private String ownerId;

    @Column(name = "lesson_session_id", length = 36, nullable = false)
    private String lessonSessionId;

    @Column(nullable = false, length = 64)
    private String bucket;

    @Column(name = "object_key", nullable = false, length = 255)
    private String objectKey;

    @Column(name = "original_file_name", length = 255)
    private String originalFileName;

    @Column(name = "media_type", nullable = false, length = 20)
    private String mediaType;

    @Column(name = "content_type", length = 100)
    private String contentType;

    @Column(name = "size_bytes")
    private Long sizeBytes;

    @Column(length = 100)
    private String etag;

    @Column(length = 64)
    private String sha256;

    @Column(nullable = false, length = 20)
    private String status = "PENDING";

    @Column(name = "expires_at")
    private LocalDateTime expiresAt;

    @Column(name = "created_at", nullable = false, updatable = false)
    private LocalDateTime createdAt;

    @Column(name = "updated_at", nullable = false)
    private LocalDateTime updatedAt;

    public enum Status { PENDING, UPLOADED, PROCESSING, EXPIRED, DELETED }

    @PrePersist
    protected void onCreate() {
        createdAt = LocalDateTime.now();
        updatedAt = LocalDateTime.now();
    }

    @PreUpdate
    protected void onUpdate() {
        updatedAt = LocalDateTime.now();
    }
}
