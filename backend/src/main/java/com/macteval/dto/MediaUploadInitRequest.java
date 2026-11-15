package com.macteval.dto;

import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Positive;
import jakarta.validation.constraints.Size;

/**
 * 直传媒体上传会话申请（SEC-03，2026-10-28）。
 *
 * <p>本轮新增 {@code sizeBytes} 与 {@code contentType} 两个**必填**字段。原因：
 * 直传路径的文件不经过 Spring，{@code spring.servlet.multipart.max-file-size}
 * 对它完全无效；前端那个 2 GB 检查只是 UI 兜底。没有声明值时，服务端无从判断
 * "这个对象本该多大"，也就无法在 confirm 阶段发现"声明 50 MB、实传 5 GB"。</p>
 *
 * <p>声明值不是信任，是**对照**：confirm 用 {@code statObject} 的实测大小与它
 * 比较，不一致即拒绝。两者都必须通过同一组上限。</p>
 *
 * @param mediaType       video | audio
 * @param fileName        原始文件名（用于取扩展名与展示）
 * @param lessonSessionId 目标课堂
 * @param sizeBytes       客户端声明的大小；必须为正且不超过服务端上限
 * @param contentType     客户端声明的 MIME；必须在该扩展名允许的集合内
 */
public record MediaUploadInitRequest(
        @NotBlank @Pattern(regexp = "video|audio") String mediaType,
        @NotBlank @Size(max = 255) String fileName,
        @NotBlank String lessonSessionId,
        @NotNull @Positive @Max(2L * 1024 * 1024 * 1024) Long sizeBytes,
        @NotBlank @Size(max = 100) String contentType
) {}
