package com.macteval.dto;

import java.time.LocalDateTime;

public record AttendanceDTO(
        String id,
        String lessonSessionId,
        String studentId,
        String studentName,
        String status,
        String source,
        Double cameraConfidence,
        Boolean cameraVisible,
        LocalDateTime checkinTime,
        boolean teacherConfirmed,
        String teacherComment,
        LocalDateTime createdAt
) {}
