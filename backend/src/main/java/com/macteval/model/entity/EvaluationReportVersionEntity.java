package com.macteval.model.entity;

import jakarta.persistence.*;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * 已发布报告的不可变内容快照。
 *
 * <p>重新生成报告只更新工作行（evaluation_reports）；历史发布版本从本表读取，
 * 保证教师/学生此前看到的分数、建议与证据可追溯。</p>
 */
@Data
@Entity
@Table(name = "evaluation_report_versions")
public class EvaluationReportVersionEntity {

    @Id
    @Column(length = 36)
    private String id;

    @Column(name = "report_id", length = 36, nullable = false)
    private String reportId;

    @Column(name = "lesson_session_id", length = 36, nullable = false)
    private String lessonSessionId;

    @Column(nullable = false)
    private Integer version;

    @Column(nullable = false, length = 20)
    private String status;

    @Column(name = "snapshot_json", nullable = false, columnDefinition = "TEXT")
    private String snapshotJson;

    @Column(name = "content_hash", length = 64)
    private String contentHash;

    @Column(name = "analysis_video_job_id", length = 36)
    private String analysisVideoJobId;

    @Column(name = "analysis_audio_job_id", length = 36)
    private String analysisAudioJobId;

    @Column(name = "published_by", length = 36)
    private String publishedBy;

    @Column(name = "published_at")
    private LocalDateTime publishedAt;

    @Column(name = "created_at", nullable = false, updatable = false)
    private LocalDateTime createdAt;

    @PrePersist
    protected void onCreate() {
        if (createdAt == null) createdAt = LocalDateTime.now();
    }
}
