package com.macteval.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

public record PasswordChangeRequest(
        @NotBlank String oldPassword,
        @NotBlank @Size(min = 6) String newPassword
) {}
