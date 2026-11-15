package com.macteval.model.entity;

import jakarta.persistence.*;
import lombok.*;
import java.time.LocalDateTime;

@Entity
@Table(name = "classroom_configs")
@Data
@NoArgsConstructor
@AllArgsConstructor
@Builder
public class ClassroomConfig {

    @Id
    private String classroomId;

    @Column(nullable = false)
    private String name;

    @Column(name = "rows_count")
    private Integer rowsCount;

    @Column(name = "cols_count")
    private Integer colsCount;

    @Column(name = "camera_config_json", columnDefinition = "TEXT")
    private String cameraConfigJson;

    @Column(name = "seat_config_json", columnDefinition = "TEXT")
    private String seatConfigJson;

    @Column(name = "created_at")
    private LocalDateTime createdAt;

    @Column(name = "updated_at")
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
