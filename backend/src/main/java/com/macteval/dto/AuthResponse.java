package com.macteval.dto;

public record AuthResponse(
        String token,
        String refreshToken,
        String userId,
        String role,
        String name,
        boolean mustChangePassword
) {}
