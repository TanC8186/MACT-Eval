package com.macteval.controller;

import com.macteval.dto.ApiResponse;
import com.macteval.dto.MediaUploadConfirmRequest;
import com.macteval.dto.MediaUploadInitRequest;
import com.macteval.service.MediaAssetService;
import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;

import java.util.Map;

@RestController
@RequestMapping("/upload")
@RequiredArgsConstructor
public class MediaUploadController {

    private final MediaAssetService mediaAssetService;

    /**
     * 两段式第一步：创建媒体资产上传会话 + 预签名 URL（不创建分析任务）。
     * 资产记录 owner/lesson/objectKey，确认时据此拒绝跨用户对象复用。
     */
    @PostMapping("/media")
    @PreAuthorize("hasAnyRole('teacher','admin')")
    public ResponseEntity<ApiResponse<Map<String, String>>> requestMediaUpload(
            @Valid @RequestBody MediaUploadInitRequest body,
            Authentication auth) {
        return ApiResponse.ok(mediaAssetService.createUploadSession(body, auth.getName()));
    }

    /** 两段式第二步：校验资产归属与对象存在后创建分析任务。 */
    @PostMapping("/media/confirm")
    @PreAuthorize("hasAnyRole('teacher','admin')")
    public ResponseEntity<ApiResponse<Map<String, String>>> confirmMediaUpload(
            @Valid @RequestBody MediaUploadConfirmRequest body,
            Authentication auth) {
        return ApiResponse.ok(mediaAssetService.confirmUpload(body, auth.getName()));
    }
}
