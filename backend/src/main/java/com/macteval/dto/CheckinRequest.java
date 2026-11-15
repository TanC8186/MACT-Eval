package com.macteval.dto;

import jakarta.validation.constraints.NotBlank;

public record CheckinRequest(
        @NotBlank String lessonSessionId,
        @NotBlank String studentId
) {}
