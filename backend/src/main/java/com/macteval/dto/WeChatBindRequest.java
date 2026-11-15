package com.macteval.dto;

import jakarta.validation.constraints.NotBlank;

public record WeChatBindRequest(
        @NotBlank String code,
        String studentId
) {}
