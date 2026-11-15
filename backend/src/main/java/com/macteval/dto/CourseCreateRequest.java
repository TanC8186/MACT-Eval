package com.macteval.dto;

import jakarta.validation.constraints.NotBlank;

public record CourseCreateRequest(
        @NotBlank String name,
        String code,
        String description,
        String department
) {}
