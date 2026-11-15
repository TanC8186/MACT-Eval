package com.macteval.controller;

import com.macteval.dto.ApiResponse;
import com.macteval.dto.EvaluationReportDTO;
import com.macteval.service.EvaluationService;
import jakarta.validation.constraints.NotBlank;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.multipart.MultipartFile;

import java.io.IOException;

/**
 * 课堂评价控制器。
 * 使用统一 ApiResponse 模板替代 Map.of 手动构造，保证响应格式一致。
 */
@RestController
@RequestMapping("/evaluate")
@RequiredArgsConstructor
@Slf4j
public class EvaluationController {

    private final EvaluationService evaluationService;

    @PostMapping("/upload")
    @PreAuthorize("hasAnyRole('teacher','admin')")
    public ResponseEntity<ApiResponse<EvaluationReportDTO>> uploadVideo(
            @RequestParam("file") MultipartFile file,
            @RequestParam(value = "lesson_id", required = false) String lessonId,
            Authentication auth) {
        // 1. Create upload directory if not exists
        String baseDir = System.getProperty("user.dir");
        String uploadDir = baseDir + "/data/uploads/";
        java.nio.file.Path uploadPath = java.nio.file.Paths.get(uploadDir).toAbsolutePath().normalize();
        try {
            java.nio.file.Files.createDirectories(uploadPath);
        } catch (IOException e) {
            log.error("Failed to create upload directory: {}", e.getMessage());
            return ApiResponse.fail("上传目录创建失败");
        }
        String originalFilename = file.getOriginalFilename();
        String safeName = (originalFilename != null ? originalFilename : "video.bin")
                .replaceAll("[^a-zA-Z0-9._-]", "_");
        // lessonId 也参与文件名拼接，必须同样净化，否则可路径穿越写出上传目录
        String safeLessonId = (lessonId != null && !lessonId.isBlank())
                ? lessonId.replaceAll("[^a-zA-Z0-9._-]", "_")
                : "upload";
        if (safeLessonId.isBlank() || safeLessonId.contains("..")) {
            safeLessonId = "upload";
        }
        String savedFilename = safeLessonId
                + "_" + System.currentTimeMillis()
                + "_" + safeName;
        java.nio.file.Path target = uploadPath.resolve(savedFilename).normalize();
        if (!target.startsWith(uploadPath)) {
            return ApiResponse.fail("非法的文件路径");
        }
        String absolutePath = target.toString();
        try {
            file.transferTo(target.toFile());
        } catch (IOException e) {
            log.error("Failed to save uploaded file: {}", e.getMessage());
            return ApiResponse.fail("文件保存失败: " + e.getMessage());
        }

        // 2. Log the filename and size
        log.info("File uploaded successfully: name={}, size={}, savedTo={}", originalFilename, file.getSize(), absolutePath);

        // 3. Pass the file path to service for real analysis
        EvaluationReportDTO report = evaluationService.processUpload(
                lessonId, absolutePath, auth.getName(), isAdmin(auth));
        return ApiResponse.ok(report);
    }

    @GetMapping("/{lessonId}")
    @PreAuthorize("hasAnyRole('teacher','admin')")
    public ResponseEntity<ApiResponse<EvaluationReportDTO>> getEvaluation(
            @PathVariable @NotBlank String lessonId, Authentication auth) {
        EvaluationReportDTO report = evaluationService.getEvaluationReport(
                lessonId, auth.getName(), isAdmin(auth));
        return ApiResponse.ok(report);
    }

    @GetMapping("/{lessonId}/timeline")
    @PreAuthorize("hasAnyRole('teacher','admin')")
    public ResponseEntity<ApiResponse<java.util.List<EvaluationReportDTO.TimelineEvent>>> getTimeline(
            @PathVariable @NotBlank String lessonId, Authentication auth) {
        EvaluationReportDTO report = evaluationService.getEvaluationReport(
                lessonId, auth.getName(), isAdmin(auth));
        return ApiResponse.ok(report.timeline());
    }

    private boolean isAdmin(Authentication auth) {
        return auth != null && auth.getAuthorities().stream()
                .anyMatch(a -> "ROLE_admin".equals(a.getAuthority()));
    }
}
