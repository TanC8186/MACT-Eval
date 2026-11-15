package com.macteval.dto;

import java.time.LocalDateTime;

public record CourseDTO(
        String id,
        String name,
        String code,
        String description,
        String teacherId,
        String department,
        LocalDateTime createdAt,
        LocalDateTime updatedAt
) {}
