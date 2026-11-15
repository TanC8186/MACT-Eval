package com.macteval.dto;

import jakarta.validation.constraints.NotBlank;

public record WeChatBindResponse(
        String userId,
        boolean isNewBinding,
        String role
) {}
