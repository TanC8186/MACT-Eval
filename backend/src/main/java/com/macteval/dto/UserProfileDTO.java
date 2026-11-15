package com.macteval.dto;

public record UserProfileDTO(
        String id,
        String name,
        String role,
        String phone,
        String avatar,
        boolean wechatBound,
        String teacherId,
        String studentId,
        boolean mustChangePassword
) {}
