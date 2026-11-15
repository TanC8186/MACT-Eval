package com.macteval.dto;

import jakarta.validation.constraints.NotBlank;

public record GradeConfirmRequest(
        @NotBlank String submissionId,
        double score,
        String comment
) {}
