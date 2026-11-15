package com.macteval.dto;

import jakarta.validation.constraints.NotBlank;
import java.util.Map;

public record SubmissionRequest(
        @NotBlank String assignmentId,
        @NotBlank String studentId,
        Map<String, String> answers,
        String attachments
) {}
