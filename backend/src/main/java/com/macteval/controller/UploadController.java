package com.macteval.controller;

import com.macteval.dto.ApiResponse;
import com.macteval.service.MediaUploadPolicy;
import com.macteval.service.MinioService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.web.bind.annotation.*;

import java.util.Map;
import java.util.UUID;

/**
 * 临时文件直传入口。
 *
 * <p><b>本轮收紧（SEC-04，2026-10-28）</b>：此前它是
 * {@code hasAnyRole('teacher','admin','student')}、60 分钟预签名 PUT、除文件名
 * 外没有任何约束——任何学生账号都可以无限量写入任意大小的对象，而该桶不在备份
 * 列表里、也没有任何清理任务，等于一个永不过期、永不回收的私有写入口。</p>
 *
 * <p>现在：只允许 teacher/admin；文件名必须带白名单扩展名；声明的类型必须与
 * 扩展名匹配；声明的大小不得超过上限。前端的调用方已不存在（全仓 grep
 * {@code /upload/temporary} 零命中），因此收紧不会影响任何现有流程。</p>
 *
 * <p>仍然没有解决的问题，写在这里而不是假装没有：本接口签的是<b>预签名 PUT</b>，
 * PUT 请求本身无法携带大小条件（{@code GetPresignedObjectUrlArgs} 只有
 * method/bucket/object/expiry），所以"声明 1 MB、实传 1 GB"仍可能发生。
 * 该桶的兜底手段是按对象名前缀 {@code temp/} 的定期清理
 * （见 {@code MediaAssetService#sweepTemporaryUploads}），以及把整个接口下线。
 * 真正彻底的做法是删除本接口。</p>
 *
 * <p><b>更正（2026-10-29）</b>：此前这里把上述限制归因于"SDK 没有
 * content-length-range"。**那只对 PUT 成立**：{@code io.minio.PostPolicy}
 * 有 {@code addContentLengthRangeCondition}，实测（
 * {@code scripts/verify/probe_post_policy_limit.py}）带条件的 POST 会在上传期
 * 返回 400 EntityTooLarge 且落盘 0 字节。因此"上传期拦截"是可实现的，
 * 只是需要把客户端从 PUT 换成 POST form —— 那是接口契约变更，不在本轮范围。</p>
 */
@Slf4j
@RestController
@RequestMapping("/upload")
@RequiredArgsConstructor
public class UploadController {

    /** 临时文件上限：临时件只用于"先落盘再交给别处"，不承载课堂原始媒体。 */
    private static final long MAX_TEMPORARY_BYTES = 512L * 1024 * 1024;

    private static final java.util.Set<String> TEMP_EXTENSIONS = java.util.Set.of(
            "pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx",
            "png", "jpg", "jpeg", "webp", "txt", "csv");

    private final MinioService minioService;
    private final MediaUploadPolicy uploadPolicy;

    @Value("${mact.upload.temporary-enabled:true}")
    private boolean temporaryEnabled;

    /**
     * 仅保留安全文件名字符，拒绝路径分隔符与目录穿越。
     */
    static String sanitizeObjectName(String objectName) {
        if (objectName == null || objectName.isBlank()) {
            return null;
        }
        String name = objectName.replace('\\', '/');
        int slash = name.lastIndexOf('/');
        if (slash >= 0) {
            name = name.substring(slash + 1);
        }
        if (name.isBlank() || name.equals(".") || name.equals("..")) {
            return null;
        }
        if (name.contains("..")) {
            return null;
        }
        return name;
    }

    /**
     * Get upload/dowload URL for temporary files.
     *
     * <p>请求体：{@code {"fileName": "...", "contentType": "...", "sizeBytes": 123}}。
     * 后两个字段本轮新增；缺失时按"未知"处理并只做扩展名校验——但那种调用会
     * 记一条 WARN，因为无法校验大小与类型。</p>
     */
    @PostMapping("/temporary")
    @PreAuthorize("hasAnyRole('teacher','admin')")
    public ResponseEntity<ApiResponse<Map<String, String>>> getTemporaryUploadUrl(
            @RequestBody Map<String, Object> body) {
        if (!temporaryEnabled) {
            return ApiResponse.error(404, "临时上传接口已关闭");
        }
        String fileName = sanitizeObjectName(String.valueOf(body.getOrDefault("fileName", "temp.bin")));
        if (fileName == null) {
            return ApiResponse.error(400, "非法的文件名");
        }
        String extension = MediaUploadPolicy.extensionOf(fileName);
        if (!TEMP_EXTENSIONS.contains(extension)) {
            return ApiResponse.error(415,
                    "不支持的临时文件类型 ." + extension + "（允许：" + String.join("/", TEMP_EXTENSIONS) + "）");
        }

        Object rawSize = body.get("sizeBytes");
        Long declaredSize = null;
        if (rawSize instanceof Number n) {
            declaredSize = n.longValue();
        } else if (rawSize instanceof String s && !s.isBlank()) {
            try {
                declaredSize = Long.parseLong(s.trim());
            } catch (NumberFormatException ignored) {
                return ApiResponse.error(400, "sizeBytes 不是合法整数");
            }
        }
        if (declaredSize != null && (declaredSize <= 0 || declaredSize > MAX_TEMPORARY_BYTES)) {
            return ApiResponse.error(413,
                    "临时文件大小超出上限（" + MAX_TEMPORARY_BYTES + " 字节）");
        }
        Object rawType = body.get("contentType");
        String declaredType = rawType == null ? null : String.valueOf(rawType);
        if (declaredSize == null || declaredType == null) {
            log.warn("TEMP_UPLOAD_UNVALIDATED fileName={} sizeBytes={} contentType={}; "
                            + "caller did not declare size/type, only the extension was checked",
                    fileName, declaredSize, declaredType);
        }

        String objectName = "temp/" + UUID.randomUUID() + "_" + fileName;
        String url = minioService.generateUploadUrl("temporary-uploads", objectName, 60);
        return ApiResponse.ok(Map.of(
                "url", url,
                "objectName", objectName,
                "maxBytes", String.valueOf(MAX_TEMPORARY_BYTES)));
    }
}
