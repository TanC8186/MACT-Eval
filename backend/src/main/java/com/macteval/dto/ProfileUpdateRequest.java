package com.macteval.dto;

import jakarta.validation.constraints.NotBlank;

public record ProfileUpdateRequest(
        @NotBlank String name,
        String phone
) {}
