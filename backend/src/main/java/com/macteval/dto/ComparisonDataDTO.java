package com.macteval.dto;

public record ComparisonDataDTO(
        String dimension,
        double selfScore,
        double classAvg,
        double gradeAvg
) {}
