package com.macteval.model.entity;

import jakarta.persistence.*;
import lombok.Data;
import lombok.EqualsAndHashCode;
import java.time.LocalDateTime;

@Data
@EqualsAndHashCode(callSuper = true)
@Entity
@DiscriminatorValue("student")
public class StudentEntity extends UserEntity {
    @Column(name = "student_id", length = 50, unique = true)
    private String studentId;

    @Column(name = "class_id", length = 36)
    private String classId;

    @Column(name = "seat_row")
    private Integer seatRow;

    @Column(name = "seat_col")
    private Integer seatCol;

    @Column(name = "face_consent_granted")
    private boolean faceConsentGranted = false;

    @Column(name = "face_consent_granted_at")
    private LocalDateTime faceConsentGrantedAt;
}
