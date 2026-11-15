package com.macteval.service;

import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

/**
 * 直传媒体的服务端策略（SEC-03，2026-10-28）。
 *
 * <p><b>为什么需要它</b>：两段式直传的文件不经过 Spring，因此
 * {@code spring.servlet.multipart.max-file-size} 完全管不到它；前端那个 2 GB
 * 检查只是 UI 兜底，任何人都能绕过。审计实测：用任意大小、任意 content-type
 * 的对象直接 PUT 到预签名 URL 都会被接受，然后在 confirm 阶段被原样记成
 * {@code sizeBytes/contentType} 并创建分析任务。</p>
 *
 * <p><b>为什么当前是"事后校验"</b>：直传走的是<b>预签名 PUT</b>
 * （{@code GetPresignedObjectUrlArgs} 只支持 method/bucket/object/expiry，
 * PUT 请求本身无法携带大小条件）。因此超限对象会先被完整写入，再由
 * {@link MediaAssetService#confirmUpload} 用 {@code statObject} 的实测值拒绝并删除。
 * 这条"完整上传后 confirm 删除"的残余风险写进了交付报告。</p>
 *
 * <p><b>更正（2026-10-29）</b>：此前这里写的是"MinIO SDK 没有
 * content-length-range 等价物，所以上传期拦截做不到"。**那句话只对预签名 PUT
 * 成立，对 POST policy 不成立**——实测证据：</p>
 *
 * <pre>
 * javap io.minio.PostPolicy         → addContentLengthRangeCondition(long, long)
 * javap io.minio.MinioClient        → getPresignedPostFormData(PostPolicy)
 * scripts/verify/probe_post_policy_limit.py 实测：
 *   带 content-length-range 的 POST：limit 1 KiB / 实发 8 MiB
 *     → HTTP 400 EntityTooLarge，桶内 0 字节（未落盘）
 *   预签名 PUT 对照（当前生产路径）：
 *     → HTTP 200，桶内 8,388,608 字节
 * </pre>
 *
 * <p>也就是说 **SDK 支持上传期拦截，只是我们还没用它**。改成 POST form 上传
 * 是可行的改进路径（前端要从 {@code PUT} 换成 {@code multipart/form-data}），
 * 但它改变客户端契约，需要前端与安全测试同步改，因此不在收口轮里做。
 * 在此之前：本类的 confirm 校验是唯一防线，且不得声称"预签名限制大小"。</p>
 */
@Slf4j
@Component
public class MediaUploadPolicy {

    /**
     * 扩展名 → 允许的 MIME 集合。
     *
     * <p>集合而不是单个 MIME：同一个 mp4 在不同系统上会被报成
     * {@code video/mp4}（标准）或 {@code application/mp4}（少见），
     * Windows 上传的 mkv 常报 {@code application/octet-stream}。
     * 白名单若只留一个值，会误杀合法文件。</p>
     *
     * <p>{@code application/octet-stream} 只在 permit-octet-stream=true 时放行，
     * 因为它是一个"我不知道"的声明，允许它等于允许任意类型。</p>
     */
    private static final Map<String, Set<String>> VIDEO_TYPES = Map.of(
            "mp4", Set.of("video/mp4", "application/mp4"),
            "m4v", Set.of("video/x-m4v", "video/mp4"),
            "avi", Set.of("video/x-msvideo", "video/avi", "video/msvideo"),
            "mov", Set.of("video/quicktime"),
            "mkv", Set.of("video/x-matroska", "video/mkv"),
            "webm", Set.of("video/webm"));

    private static final Map<String, Set<String>> AUDIO_TYPES = Map.of(
            "wav", Set.of("audio/wav", "audio/x-wav", "audio/wave"),
            "mp3", Set.of("audio/mpeg", "audio/mp3"),
            "m4a", Set.of("audio/mp4", "audio/x-m4a", "audio/m4a"),
            "flac", Set.of("audio/flac", "audio/x-flac"),
            "ogg", Set.of("audio/ogg"),
            "aac", Set.of("audio/aac", "audio/x-aac"),
            "webm", Set.of("audio/webm"));

    /**
     * 单文件上限。取值依据：前端与 {@code application.yml} 都是 2 GB，
     * 本轮不让服务端比对外宣称的值更宽松——否则"最大 2GB"就是一句假话。
     */
    @Value("${mact.media.max-bytes:2147483648}")
    private long maxBytes;

    /** 单课堂累计字节上限。20 个资产 × 2 GB = 40 GB，远超合理用量，因此另设总容量。 */
    @Value("${mact.media.max-bytes-per-lesson:8589934592}")
    private long maxBytesPerLesson;

    /** 是否放行 {@code application/octet-stream}（默认否）。 */
    @Value("${mact.media.allow-octet-stream:false}")
    private boolean allowOctetStream;

    public long maxBytes() {
        return maxBytes;
    }

    public long maxBytesPerLesson() {
        return maxBytesPerLesson;
    }

    /** 取小写扩展名（不含点）；无扩展名返回空串。 */
    public static String extensionOf(String fileName) {
        if (fileName == null) {
            return "";
        }
        String name = fileName.replace('\\', '/');
        int slash = name.lastIndexOf('/');
        if (slash >= 0) {
            name = name.substring(slash + 1);
        }
        int dot = name.lastIndexOf('.');
        if (dot < 0 || dot == name.length() - 1) {
            return "";
        }
        return name.substring(dot + 1).toLowerCase(Locale.ROOT);
    }

    /** 扩展名是否属于该媒体类型。 */
    public boolean isAllowedExtension(String mediaType, String extension) {
        Map<String, Set<String>> table = tableFor(mediaType);
        return table != null && table.containsKey(extension);
    }

    /**
     * MIME 是否与该扩展名匹配。
     *
     * <p>先按 {@code ;} 截掉 charset 等参数：浏览器对 mp4 常发
     * {@code video/mp4;codecs=avc1}，直接字符串比较会全部判死。</p>
     */
    public boolean isAllowedContentType(String mediaType, String extension, String contentType) {
        if (contentType == null || contentType.isBlank()) {
            return false;
        }
        String mime = contentType.split(";", 2)[0].trim().toLowerCase(Locale.ROOT);
        if (allowOctetStream && "application/octet-stream".equals(mime)) {
            return true;
        }
        Map<String, Set<String>> table = tableFor(mediaType);
        if (table == null) {
            return false;
        }
        Set<String> allowed = table.get(extension);
        return allowed != null && allowed.contains(mime);
    }

    /** 供测试与错误信息使用：某媒体类型允许的扩展名列表。 */
    public List<String> allowedExtensions(String mediaType) {
        Map<String, Set<String>> table = tableFor(mediaType);
        return table == null ? List.of() : List.copyOf(new LinkedHashSet<>(table.keySet()));
    }

    private static Map<String, Set<String>> tableFor(String mediaType) {
        if ("video".equals(mediaType)) {
            return VIDEO_TYPES;
        }
        if ("audio".equals(mediaType)) {
            return AUDIO_TYPES;
        }
        return null;
    }
}
