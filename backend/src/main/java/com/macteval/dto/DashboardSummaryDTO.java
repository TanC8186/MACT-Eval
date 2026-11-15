package com.macteval.dto;

public record DashboardSummaryDTO(
        int totalLessons,
        double avgEngagement,
        double avgTeaching,
        double avgClimate,
        String trend,
        String dataSource
) {}
