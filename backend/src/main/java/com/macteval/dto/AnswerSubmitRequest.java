package com.macteval.dto;

import jakarta.validation.constraints.NotBlank;
import java.util.List;

public record AnswerSubmitRequest(
        @NotBlank String lessonSessionId,
        @NotBlank String questionId,
        @NotBlank String studentId,
        @NotBlank String answer
) {}
