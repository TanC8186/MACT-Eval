package com.macteval.controller;

import com.macteval.dto.ApiResponse;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;

/**
 * 健康检查控制器。
 */
@RestController
public class HealthController {

    @Value("${spring.application.name:MACT-Eval}")
    private String appName;

    @GetMapping("/health")
    public ResponseEntity<ApiResponse<Map<String, Object>>> healthCheck() {
        return ApiResponse.ok(Map.of(
                "status", "ok",
                "service", appName,
                "version", "1.0.0"
        ));
    }
}
