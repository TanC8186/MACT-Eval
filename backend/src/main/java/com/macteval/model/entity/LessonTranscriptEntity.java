package com.macteval.model.entity;

import jakarta.persistence.*;
import lombok.Data;
import java.time.LocalDateTime;

@Data
@Entity
@Table(name = "lesson_transcripts", indexes = {
        @Index(name = "idx_transcript_lesson", columnList = "lesson_session_id")
})
public class LessonTranscriptEntity {
    @Id
    @Column(length = 36)
    private String id;

    @Column(name = "lesson_session_id", length = 36, nullable = false)
    private String lessonSessionId;

    @Column(name = "job_id", length = 36)
    private String jobId;

    @Column(nullable = false, columnDefinition = "TEXT")
    private String text;

    @Column(length = 20)
    private String language;

    @Column(name = "language_probability")
    private Double languageProbability;

    @Column(columnDefinition = "TEXT")
    private String segments;

    @Column(name = "model_version", length = 100)
    private String modelVersion;

    /** 完整媒体时长与采集覆盖区间：区分“静音”与“音频未覆盖”。 */
    @Column(name = "media_duration_ms")
    private Long mediaDurationMs;

    @Column(name = "coverage_start_ms")
    private Long coverageStartMs;

    @Column(name = "coverage_end_ms")
    private Long coverageEndMs;

    @Column(name = "model_digest", length = 64)
    private String modelDigest;

    @Column(name = "config_json", columnDefinition = "TEXT")
    private String configJson;

    @Column(name = "created_at", nullable = false, updatable = false)
    private LocalDateTime createdAt;

    @PrePersist
    protected void onCreate() {
        createdAt = LocalDateTime.now();
    }
}
