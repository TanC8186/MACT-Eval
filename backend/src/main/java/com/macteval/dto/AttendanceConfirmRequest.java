package com.macteval.dto;

import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotEmpty;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

import java.util.List;

public record AttendanceConfirmRequest(
        @NotBlank String lessonSessionId,
        @NotEmpty @Size(max = 500) List<@Valid AttendanceItem> records
) {
    public record AttendanceItem(
            @NotBlank String recordId,
            @NotBlank
            @Pattern(regexp = "PENDING|PRESENT|LATE|ABSENT|EXCUSED",
                    message = "无效的考勤状态")
            String status,
            @Size(max = 500) String comment
    ) {}
}
