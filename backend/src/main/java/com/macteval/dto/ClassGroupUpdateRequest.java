package com.macteval.dto;

import jakarta.validation.constraints.NotBlank;

public record ClassGroupUpdateRequest(
        @NotBlank String name,
        String grade,
        String department
) {}
