package com.macteval.model.entity;

import jakarta.persistence.*;
import lombok.Data;
import java.time.LocalDateTime;

@Data
@Entity
@Table(name = "evaluation_reports",
        uniqueConstraints = @UniqueConstraint(columnNames = "lesson_session_id"))
public class EvaluationReportEntity {
    @Id
    @Column(length = 36)
    private String id;

    @Column(name = "lesson_session_id", length = 36)
    private String lessonSessionId;

    @Column(name = "course_id", length = 36)
    private String courseId;

    @Column(name = "class_id", length = 36)
    private String classId;

    @Column(name = "teacher_id", length = 36)
    private String teacherId;

    @Column(nullable = false, length = 200)
    private String title;

    @Column(columnDefinition = "TEXT")
    private String summary;

    @Column(name = "engagement_score")
    private Double engagementScore;

    @Column(name = "teaching_score")
    private Double teachingScore;

    @Column(name = "climate_score")
    private Double climateScore;

    @Column(name = "data_coverage", columnDefinition = "TEXT")
    private String dataCoverage;

    @Column(name = "attendance_summary", columnDefinition = "TEXT")
    private String attendanceSummary;

    @Column(name = "behavior_summary", columnDefinition = "TEXT")
    private String behaviorSummary;

    @Column(name = "quiz_statistics", columnDefinition = "TEXT")
    private String quizStatistics;

    @Column(name = "assignment_summary", columnDefinition = "TEXT")
    private String assignmentSummary;

    @Column(name = "feedback_summary", columnDefinition = "TEXT")
    private String feedbackSummary;

    @Column(name = "suggestions", columnDefinition = "TEXT")
    private String suggestions;

    @Column(name = "timeline_json", columnDefinition = "TEXT")
    private String timelineJson;

    @Column(name = "raw_report", columnDefinition = "TEXT")
    private String rawReport;

    @Column(name = "data_source", nullable = false, length = 20)
    private String dataSource = "UNKNOWN";

    @Column(name = "model_version", length = 50)
    private String modelVersion;

    @Column(name = "coverage")
    private Double coverage;

    @Column(name = "window_count")
    private Integer windowCount;

    @Column(name = "feature_count")
    private Integer featureCount;

    /** 报告工作版本号；发布快照存于 evaluation_report_versions。 */
    @Column(nullable = false)
    private Integer version = 1;

    @Column(name = "published_by", length = 36)
    private String publishedBy;

    /** 生成该版本时实际使用的分析任务（可追溯）。 */
    @Column(name = "analysis_video_job_id", length = 36)
    private String analysisVideoJobId;

    @Column(name = "analysis_audio_job_id", length = 36)
    private String analysisAudioJobId;

    @Column(name = "content_hash", length = 64)
    private String contentHash;

    /** 生成该报告时实际使用的媒体摘要/模型摘要/运行配置。 */
    @Column(name = "traceability_json", columnDefinition = "TEXT")
    private String traceabilityJson;

    @Column(nullable = false, length = 20)
    private String status = "draft";

    @Column(name = "published_at")
    private LocalDateTime publishedAt;

    @Column(name = "created_at", nullable = false, updatable = false)
    private LocalDateTime createdAt;

    @Column(name = "updated_at", nullable = false)
    private LocalDateTime updatedAt;

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
