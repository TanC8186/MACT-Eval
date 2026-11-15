package com.macteval.dto;

import java.time.LocalDateTime;

public record AnswerResultDTO(
        String id,
        String lessonSessionId,
        String questionId,
        String studentId,
        String answer,
        Boolean isCorrect,
        Double score,
        String gradingStatus,
        LocalDateTime submittedAt
) {}
