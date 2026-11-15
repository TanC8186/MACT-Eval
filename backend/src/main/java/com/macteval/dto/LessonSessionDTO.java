package com.macteval.dto;

import java.time.LocalDateTime;
import java.util.List;

public record LessonSessionDTO(
        String id,
        String courseId,
        String courseName,
        String classId,
        String className,
        String teacherId,
        String title,
        LocalDateTime date,
        LocalDateTime startTime,
        LocalDateTime endTime,
        String status,
        List<String> knowledgePointIds,
        String teachingObjectives,
        LocalDateTime createdAt
) {}
