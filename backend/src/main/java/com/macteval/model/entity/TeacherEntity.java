package com.macteval.model.entity;

import jakarta.persistence.*;
import lombok.Data;
import lombok.EqualsAndHashCode;

@Data
@EqualsAndHashCode(callSuper = true)
@Entity
@DiscriminatorValue("teacher")
public class TeacherEntity extends UserEntity {
    @Column(name = "teacher_id", length = 50, unique = true)
    private String teacherId;

    @Column(length = 100)
    private String title;

    @Column(length = 200)
    private String department;
}
