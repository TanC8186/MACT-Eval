package com.macteval.dto;

public record AttendanceSummaryDTO(
        String lessonSessionId,
        int totalStudents,
        int present,
        int late,
        int absent,
        int excused,
        int pending,
        double coverage,
        String dataQuality
) {}
