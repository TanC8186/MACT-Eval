package com.macteval.dto;

import java.time.LocalDateTime;
import java.util.Map;

public record SubmissionDTO(
        String id,
        String assignmentId,
        String studentId,
        String studentName,
        Map<String, String> answers,
        String attachments,
        LocalDateTime submittedAt,
        String status,
        Double totalScore,
        String teacherComment,
        String aiGradingDraft,
        boolean teacherConfirmed,
        LocalDateTime createdAt
) {}
