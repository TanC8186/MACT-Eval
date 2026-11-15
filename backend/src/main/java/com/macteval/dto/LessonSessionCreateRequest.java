package com.macteval.dto;

import jakarta.validation.constraints.NotBlank;
import java.time.LocalDateTime;

public record LessonSessionCreateRequest(
        @NotBlank String courseId,
        @NotBlank String classId,
        @NotBlank String title,
        LocalDateTime date,
        LocalDateTime startTime,
        LocalDateTime endTime,
        String teachingObjectives
) {}
