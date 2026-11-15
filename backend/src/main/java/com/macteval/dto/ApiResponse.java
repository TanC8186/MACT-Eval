package com.macteval.dto;

import com.fasterxml.jackson.annotation.JsonInclude;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;

/**
 * 统一 API 响应包装器。
 * 替代每个 Controller 手动构造 Map.of("status","success") ——
 * 保证全系统响应格式一致，减少重复代码。
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record ApiResponse<T>(
        int code,
        String message,
        T data
) {
    public static <T> ResponseEntity<ApiResponse<T>> ok(T data) {
        return ResponseEntity.ok(new ApiResponse<>(200, "success", data));
    }

    public static <T> ResponseEntity<ApiResponse<T>> created(T data) {
        return ResponseEntity.status(HttpStatus.CREATED)
                .body(new ApiResponse<>(201, "success", data));
    }

    public static <T> ResponseEntity<ApiResponse<T>> error(int code, String message) {
        HttpStatus status = HttpStatus.resolve(code);
        if (status == null) {
            status = code >= 500 ? HttpStatus.INTERNAL_SERVER_ERROR
                    : code >= 400 ? HttpStatus.BAD_REQUEST : HttpStatus.OK;
        }
        return ResponseEntity.status(status)
                .body(new ApiResponse<>(code, message, null));
    }

    public static <T> ResponseEntity<ApiResponse<T>> fail(String message) {
        return fail(message, HttpStatus.BAD_REQUEST);
    }

    public static <T> ResponseEntity<ApiResponse<T>> fail(String message, HttpStatus status) {
        return ResponseEntity.status(status)
                .body(new ApiResponse<>(status.value(), message, null));
    }
}
