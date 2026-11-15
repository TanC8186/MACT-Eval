package com.macteval.dto;

import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

public record MediaUploadConfirmRequest(
        @NotBlank String assetId,
        @NotBlank @Size(max = 255) String objectName,
        @NotBlank String lessonSessionId,
        @Min(1) @Max(500) Integer totalSeats,
        @Min(1) @Max(60) Integer sampleInterval,
        @Min(10) @Max(600) Integer windowDuration,
        @Min(1) @Max(60) Integer minObservations
) {}
