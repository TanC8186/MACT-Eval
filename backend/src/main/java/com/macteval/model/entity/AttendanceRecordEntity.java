package com.macteval.model.entity;

import jakarta.persistence.*;
import lombok.Data;
import java.time.LocalDateTime;

@Data
@Entity
@Table(name = "attendance_records", indexes = {
    @Index(name = "idx_attendance_lesson", columnList = "lessonSessionId"),
    @Index(name = "idx_attendance_student", columnList = "studentId")
})
public class AttendanceRecordEntity {
    @Id
    @Column(length = 36)
    private String id;

    @Column(name = "lesson_session_id", length = 36, nullable = false)
    private String lessonSessionId;

    @Column(name = "student_id", length = 36, nullable = false)
    private String studentId;

    @Column(nullable = false, length = 20)
    @Enumerated(EnumType.STRING)
    private AttendanceStatus status = AttendanceStatus.PENDING;

    @Column(nullable = false, length = 30)
    @Enumerated(EnumType.STRING)
    private AttendanceSource source;

    @Column(name = "camera_confidence")
    private Double cameraConfidence;

    @Column(name = "camera_visible")
    private Boolean cameraVisible;

    @Column(name = "checkin_time")
    private LocalDateTime checkinTime;

    @Column(name = "teacher_confirmed")
    private boolean teacherConfirmed = false;

    @Column(name = "teacher_confirmed_at")
    private LocalDateTime teacherConfirmedAt;

    @Column(name = "teacher_comment", columnDefinition = "TEXT")
    private String teacherComment;

    @Column(name = "created_at", nullable = false, updatable = false)
    private LocalDateTime createdAt;

    @Column(name = "updated_at", nullable = false)
    private LocalDateTime updatedAt;

    public enum AttendanceStatus {
        PENDING,
        PRESENT,
        LATE,
        ABSENT,
        EXCUSED
    }

    public enum AttendanceSource {
        STUDENT_CHECKIN,
        CAMERA_EVIDENCE,
        TEACHER_CORRECTION
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
