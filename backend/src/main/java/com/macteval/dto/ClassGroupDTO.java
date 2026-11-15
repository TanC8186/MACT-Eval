package com.macteval.dto;

import java.time.LocalDateTime;

public record ClassGroupDTO(
        String id,
        String name,
        String grade,
        String department,
        int studentCount,
        LocalDateTime createdAt
) {}
