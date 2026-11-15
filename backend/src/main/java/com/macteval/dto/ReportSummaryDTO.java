package com.macteval.dto;

import java.time.LocalDateTime;
import java.util.List;

public record ReportSummaryDTO(
        String id,
        String title,
        String courseName,
        String className,
        String teacherName,
        LocalDateTime date,
        String status,
        Double overallScore,
        Double climateScore,
        String dataSource,
        LocalDateTime createdAt,
        boolean generated
) {}
