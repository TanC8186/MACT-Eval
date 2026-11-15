package com.macteval.dto;

import java.time.LocalDateTime;

public record JobStatusDTO(
        String jobId,
        String type,
        String status,
        int progress,
        String resultMetadata,
        String errorMessage,
        LocalDateTime createdAt,
        LocalDateTime completedAt
) {}
