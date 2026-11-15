package com.macteval.controller;

import com.macteval.dto.*;
import com.macteval.service.EvaluationService;
import lombok.RequiredArgsConstructor;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;

import java.util.List;

/**
 * 仪表盘控制器 — 提供总览数据、对比和教学建议接口。
 */
@RestController
@RequestMapping("/dashboard")
@RequiredArgsConstructor
public class DashboardController {

    private final EvaluationService evaluationService;

    @GetMapping("/summary")
    public ResponseEntity<ApiResponse<DashboardSummaryDTO>> getSummary(Authentication auth) {
        return ApiResponse.ok(evaluationService.getDashboardSummary(auth.getName(), isAdmin(auth)));
    }

    private boolean isAdmin(Authentication auth) {
        return auth != null && auth.getAuthorities().stream()
                .anyMatch(a -> "ROLE_admin".equals(a.getAuthority()));
    }

    @GetMapping("/comparison")
    public ResponseEntity<ApiResponse<List<ComparisonDataDTO>>> getComparison() {
        return ApiResponse.ok(evaluationService.getComparison());
    }

    @GetMapping("/suggestions")
    public ResponseEntity<ApiResponse<List<SuggestionDTO>>> getSuggestions(Authentication auth) {
        return ApiResponse.ok(evaluationService.getSuggestions(auth.getName(), isAdmin(auth)));
    }

}
