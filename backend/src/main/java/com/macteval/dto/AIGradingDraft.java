package com.macteval.dto;

public record AIGradingDraft(
        double suggestedScore,
        double confidence,
        String evidence,
        String feedback
) {}
