package com.macteval.model.entity;

import jakarta.persistence.*;
import lombok.Data;
import java.time.LocalDateTime;

@Data
@Entity
@Table(name = "lesson_sessions")
public class LessonSessionEntity {
    @Id
    @Column(length = 36)
    private String id;

    @Column(name = "course_id", length = 36, nullable = false)
    private String courseId;

    @Column(name = "class_id", length = 36, nullable = false)
    private String classId;

    @Column(name = "teacher_id", length = 36, nullable = false)
    private String teacherId;

    @Column(nullable = false, length = 200)
    private String title;

    @Column(nullable = false)
    private LocalDateTime date;

    @Column(name = "start_time", nullable = false)
    private LocalDateTime startTime;

    @Column(name = "end_time")
    private LocalDateTime endTime;

    /** 实际开始/结束时间：与计划时间分开，创建课堂不等于课堂已开始。 */
    @Column(name = "actual_start_time")
    private LocalDateTime actualStartTime;

    @Column(name = "actual_end_time")
    private LocalDateTime actualEndTime;

    /** 乐观锁：并发开始/结束课堂时返回 409，而不是最后写入者获胜。 */
    @Version
    @Column(nullable = false)
    private Long version = 0L;

    @Column(nullable = false, length = 20)
    @Enumerated(EnumType.STRING)
    private SessionStatus status = SessionStatus.SCHEDULED;

    @Column(name = "knowledge_point_ids", columnDefinition = "TEXT")
    private String knowledgePointIds;

    @Column(name = "teaching_objectives", columnDefinition = "TEXT")
    private String teachingObjectives;

    @Column(name = "created_at", nullable = false, updatable = false)
    private LocalDateTime createdAt;

    @Column(name = "updated_at", nullable = false)
    private LocalDateTime updatedAt;

    public enum SessionStatus {
        SCHEDULED,
        IN_PROGRESS,
        COMPLETED,
        CANCELLED
    }

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
