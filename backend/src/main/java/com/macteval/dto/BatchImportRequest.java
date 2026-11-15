package com.macteval.dto;

import jakarta.validation.Valid;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotEmpty;
import jakarta.validation.constraints.Size;

import java.util.List;

public record BatchImportRequest(
        @NotBlank String classId,
        @NotEmpty @Size(max = 500) List<@Valid StudentEntry> students
) {
    public record StudentEntry(
            @NotBlank @Size(max = 50) String studentId,
            @NotBlank @Size(max = 100) String name,
            @Size(max = 50) String phone,
            @Min(0) @Max(500) Integer seatRow,
            @Min(0) @Max(500) Integer seatCol
    ) {}
}
