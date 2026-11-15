package com.macteval.model.entity;

import jakarta.persistence.*;
import lombok.Getter;
import lombok.NoArgsConstructor;
import lombok.Setter;

import java.time.LocalDateTime;

@Entity
@Table(name = "lesson_window_features")
@Getter
@Setter
@NoArgsConstructor
public class LessonWindowFeatureEntity {

    @Id
    private String id;

    @Column(name = "lesson_session_id")
    private String lessonSessionId;

    /**
     * 产生该窗口的视频/音频分析任务（R04：窗口按分析版本保存）。
     * 空串表示该版本该模态没有任务；历史行为 NULL。
     */
    @Column(name = "video_job_id", length = 36)
    private String videoJobId;

    @Column(name = "audio_job_id", length = 36)
    private String audioJobId;

    @Column(name = "window_index")
    private Integer windowIndex;

    @Column(name = "window_start")
    private LocalDateTime windowStart;

    @Column(name = "window_end")
    private LocalDateTime windowEnd;

    @Column(name = "video_score")
    private Double videoScore;

    @Column(name = "speech_score")
    private Double speechScore;

    @Column(name = "business_score")
    private Double businessScore;

    @Column(name = "fused_score")
    private Double fusedScore;

    @Column(name = "video_features", columnDefinition = "TEXT")
    private String videoFeatures;

    @Column(name = "speech_features", columnDefinition = "TEXT")
    private String speechFeatures;

    @Column(name = "business_features", columnDefinition = "TEXT")
    private String businessFeatures;

    @Column(name = "fusion_features", columnDefinition = "TEXT")
    private String fusionFeatures;

    @Column(name = "created_at")
    private LocalDateTime createdAt;

    @PrePersist
    void onCreate() {
        if (createdAt == null) createdAt = LocalDateTime.now();
    }
}
