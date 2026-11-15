package com.macteval.controller;

import com.macteval.dto.*;
import com.macteval.service.ReportService;
import com.macteval.service.ReportEvidenceService;
import lombok.RequiredArgsConstructor;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
@RequestMapping("/reports")
@RequiredArgsConstructor
public class ReportController {

    private final ReportService reportService;
    private final ReportEvidenceService reportEvidenceService;

    /** URLs are refreshed per read, while report/video version selection stays immutable. */
    @GetMapping("/{lessonSessionId}/behavior-evidence")
    @PreAuthorize("hasAnyRole('teacher','admin')")
    public ResponseEntity<ApiResponse<List<EvidenceFrameDTO>>> behaviorEvidence(
            @PathVariable String lessonSessionId,
            @RequestParam(required = false) Integer version,
            Authentication auth) {
        return ApiResponse.ok(reportEvidenceService.getFrames(lessonSessionId, version, auth.getName()));
    }

    @GetMapping
    public ResponseEntity<ApiResponse<List<ReportSummaryDTO>>> listReports(Authentication auth) {
        String role = roleOf(auth);
        return ApiResponse.ok(reportService.listReports(auth.getName(), role));
    }

    @GetMapping("/page")
    public ResponseEntity<ApiResponse<PageResponse<ReportSummaryDTO>>> listReportsPage(
            @RequestParam(defaultValue = "0") int page,
            @RequestParam(defaultValue = "20") int size,
            Authentication auth) {
        return ApiResponse.ok(reportService.listReportsPage(
                auth.getName(), roleOf(auth), page, size));
    }

    @PostMapping("/generate/{lessonSessionId}")
    @PreAuthorize("hasAnyRole('teacher','admin')")
    public ResponseEntity<ApiResponse<ReportDetailDTO>> generate(
            @PathVariable String lessonSessionId,
            Authentication auth) {
        String teacherId = auth != null ? auth.getName() : null;
        return ApiResponse.ok(reportService.generateReport(lessonSessionId, teacherId));
    }

    @GetMapping("/{lessonSessionId}")
    public ResponseEntity<ApiResponse<ReportDetailDTO>> getReport(
            @PathVariable String lessonSessionId,
            Authentication auth) {
        if ("student".equals(roleOf(auth))) {
            return ApiResponse.ok(reportService.getReportForStudent(lessonSessionId, auth.getName()));
        }
        String teacherId = auth != null ? auth.getName() : null;
        return ApiResponse.ok(reportService.getReport(lessonSessionId, teacherId));
    }

    private String roleOf(Authentication auth) {
        if (auth == null) return "anonymous";
        if (auth.getAuthorities().stream().anyMatch(a -> "ROLE_admin".equals(a.getAuthority()))) return "admin";
        if (auth.getAuthorities().stream().anyMatch(a -> "ROLE_student".equals(a.getAuthority()))) return "student";
        return "teacher";
    }

    @PostMapping("/{lessonSessionId}/publish")
    @PreAuthorize("hasAnyRole('teacher','admin')")
    public ResponseEntity<ApiResponse<ReportDetailDTO>> publish(
            @PathVariable String lessonSessionId,
            Authentication auth) {
        String teacherId = auth != null ? auth.getName() : null;
        return ApiResponse.ok(reportService.publishReport(lessonSessionId, teacherId));
    }

    /** 查看某个已发布版本的完整快照内容。 */
    @GetMapping("/{lessonSessionId}/versions/{version}")
    @PreAuthorize("hasAnyRole('teacher','admin')")
    public ResponseEntity<ApiResponse<ReportDetailDTO>> versionDetail(
            @PathVariable String lessonSessionId,
            @PathVariable Integer version,
            Authentication auth) {
        return ApiResponse.ok(reportService.getVersionDetail(
                lessonSessionId, version, auth.getName()));
    }

    /** 已发布报告版本快照摘要（追溯历史发布内容）。 */
    @GetMapping("/{lessonSessionId}/versions")
    @PreAuthorize("hasAnyRole('teacher','admin')")
    public ResponseEntity<ApiResponse<List<ReportVersionSummaryDTO>>> versions(
            @PathVariable String lessonSessionId,
            Authentication auth) {
        return ApiResponse.ok(reportService.getVersionHistory(lessonSessionId, auth.getName()));
    }

    /**
     * 删除评价报告（授权撤回 / 数据删除）。
     * 报告按课堂会话定位（与 GET /reports/{lessonSessionId} 一致）。
     */
    @DeleteMapping("/{lessonSessionId}")
    @PreAuthorize("hasAnyRole('teacher','admin')")
    public ResponseEntity<ApiResponse<String>> deleteReport(
            @PathVariable String lessonSessionId,
            Authentication auth) {
        String teacherId = auth != null ? auth.getName() : null;
        reportService.deleteReport(lessonSessionId, teacherId);
        return ApiResponse.ok("删除成功");
    }
}
