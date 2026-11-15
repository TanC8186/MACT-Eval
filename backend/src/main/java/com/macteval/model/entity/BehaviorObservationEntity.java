package com.macteval.model.entity;

import jakarta.persistence.*;
import lombok.Data;
import java.time.LocalDateTime;

@Data
@Entity
@Table(name = "behavior_observations", indexes = {
    @Index(name = "idx_behavior_lesson", columnList = "lessonSessionId")
})
public class BehaviorObservationEntity {
    @Id
    @Column(length = 36)
    private String id;

    @Column(name = "lesson_session_id", length = 36, nullable = false)
    private String lessonSessionId;

    @Column(name = "activity_id", length = 36)
    private String activityId;

    /** 产生该观测的分析任务；与 windowIndex 组成幂等键。 */
    @Column(name = "job_id", length = 36)
    private String jobId;

    /** 窗口在媒体时间轴上的索引（由 AI worker 按配置窗口时长计算）。 */
    @Column(name = "window_index")
    private Integer windowIndex;

    /** 相对媒体起点的窗口偏移（毫秒），不受任务提交时间影响。 */
    @Column(name = "window_start_offset_ms")
    private Long windowStartOffsetMs;

    @Column(name = "window_end_offset_ms")
    private Long windowEndOffsetMs;

    /** 第 0 帧对应的绝对时间锚点；同一媒体重跑保持不变。 */
    @Column(name = "media_anchor")
    private java.time.LocalDateTime mediaAnchor;

    @Column(name = "window_start", nullable = false)
    private LocalDateTime windowStart;

    @Column(name = "window_end", nullable = false)
    private LocalDateTime windowEnd;

    @Column(name = "visible_seat_count")
    private Integer visibleSeatCount;

    @Column(name = "total_configured_seats")
    private Integer totalConfiguredSeats;

    private Double coverage;

    @Column(length = 20)
    private String dataQuality;

    @Column(name = "in_seat_count")
    private Integer inSeatCount;

    @Column(name = "leave_seat_count")
    private Integer leaveSeatCount;

    @Column(name = "hand_raised_count")
    private Integer handRaisedCount;

    @Column(name = "standing_count")
    private Integer standingCount;

    @Column(name = "upright_posture_count")
    private Integer uprightPostureCount;

    @Column(length = 20)
    private String aggregatedUprightProportion;

    @Column(name = "created_at", nullable = false, updatable = false)
    private LocalDateTime createdAt;

    @PrePersist
    protected void onCreate() {
        createdAt = LocalDateTime.now();
    }
}
