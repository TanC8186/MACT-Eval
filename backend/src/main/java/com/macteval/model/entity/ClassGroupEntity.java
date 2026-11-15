package com.macteval.model.entity;

import jakarta.persistence.*;
import lombok.Data;
import java.time.LocalDateTime;

@Data
@Entity
@Table(name = "class_groups")
public class ClassGroupEntity {
    @Id
    @Column(length = 36)
    private String id;

    @Column(nullable = false, length = 200)
    private String name;

    @Column(length = 50)
    private String grade;

    @Column(length = 200)
    private String department;

    @Column(name = "student_count")
    private Integer studentCount = 0;

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
