package com.macteval.dto;

import java.time.LocalDateTime;

public record UpdateSessionRequest(
        String title,
        LocalDateTime date,
        LocalDateTime startTime,
        LocalDateTime endTime,
        String teachingObjectives
) {}
