package com.macteval.dto;

public record SpeechMetricsDTO(
        String lessonSessionId,
        int windowCount,
        double speechCoverageAvg,
        int totalQuestionCount,
        double totalSpeechSeconds) {
}
