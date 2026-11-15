package com.macteval.dto;

import java.util.Map;

public record ErrorResponse(
        int code,
        String message,
        long timestamp,
        Map<String, String> fieldErrors
) {
    public static ErrorResponse of(int code, String message) {
        return new ErrorResponse(code, message, System.currentTimeMillis(), null);
    }

    public static ErrorResponse of(int code, String message, Map<String, String> fieldErrors) {
        return new ErrorResponse(code, message, System.currentTimeMillis(), fieldErrors);
    }
}
