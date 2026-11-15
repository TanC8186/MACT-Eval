package com.macteval.service;

import io.minio.GetObjectArgs;
import io.minio.GetObjectResponse;
import io.minio.GetPresignedObjectUrlArgs;
import io.minio.MinioClient;
import io.minio.RemoveObjectArgs;
import io.minio.ListObjectsArgs;
import io.minio.Result;
import io.minio.StatObjectArgs;
import io.minio.messages.Item;
import io.minio.StatObjectResponse;
import io.minio.errors.ErrorResponseException;
import io.minio.http.Method;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.stereotype.Service;

import java.io.ByteArrayOutputStream;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.concurrent.TimeUnit;

@Slf4j
@Service
public class MinioService {

    /** 后端内部操作客户端（容器内网端点，用于 statObject 等）。 */
    private final MinioClient minioClient;

    /** 预签名 URL 专用客户端（浏览器可达的 public endpoint，签名按该 host 计算）。 */
    private final MinioClient presignMinioClient;

    public MinioService(
            @Qualifier("minioClient") MinioClient minioClient,
            @Qualifier("presignMinioClient") MinioClient presignMinioClient) {
        this.minioClient = minioClient;
        this.presignMinioClient = presignMinioClient;
    }

    /**
     * Generate presigned upload URL for a file.
     */
    public String generateUploadUrl(String bucket, String objectName, long expiryMinutes) {
        try {
            return presignMinioClient.getPresignedObjectUrl(
                    GetPresignedObjectUrlArgs.builder()
                            .method(Method.PUT)
                            .bucket(bucket)
                            .object(objectName)
                            .expiry((int) expiryMinutes, TimeUnit.MINUTES)
                            .build()
            );
        } catch (Exception e) {
            log.error("Failed to generate upload URL for {}/{}: {}", bucket, objectName, e.getMessage());
            throw new RuntimeException("生成上传链接失败", e);
        }
    }

    /**
     * Generate presigned download URL for a file.
     */
    public String generateDownloadUrl(String bucket, String objectName, long expiryMinutes) {
        try {
            return presignMinioClient.getPresignedObjectUrl(
                    GetPresignedObjectUrlArgs.builder()
                            .method(Method.GET)
                            .bucket(bucket)
                            .object(objectName)
                            .expiry((int) expiryMinutes, TimeUnit.MINUTES)
                            .build()
            );
        } catch (Exception e) {
            log.error("Failed to generate download URL for {}/{}: {}", bucket, objectName, e.getMessage());
            throw new RuntimeException("生成下载链接失败", e);
        }
    }

    /**
     * Check if an object exists.
     */
    public boolean objectExists(String bucket, String objectName) {
        try {
            minioClient.statObject(StatObjectArgs.builder()
                    .bucket(bucket).object(objectName).build());
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    public record ObjectInfo(long size, String etag, String contentType) {}

    /** Object metadata used to bind an uploaded object to a media asset. */
    public ObjectInfo statObject(String bucket, String objectName) {
        try {
            StatObjectResponse stat = minioClient.statObject(StatObjectArgs.builder()
                    .bucket(bucket).object(objectName).build());
            return new ObjectInfo(stat.size(), stat.etag(), stat.contentType());
        } catch (Exception e) {
            return null;
        }
    }

    /** Delete an object; used by retention cleanup and explicit deletion. */
    public boolean removeObject(String bucket, String objectName) {
        try {
            minioClient.removeObject(RemoveObjectArgs.builder()
                    .bucket(bucket).object(objectName).build());
            return true;
        } catch (Exception e) {
            log.warn("Failed to remove {}/{}: {}", bucket, objectName, e.getMessage());
            return false;
        }
    }

    // === 对象探测 / 原始字节读取（任务结果恢复；协议 §2/§6） ===
    // 注意：objectExists / statObject 把一切异常折叠成 false/null，无法区分
    // “对象不存在”与“存储不可达/无权限”。恢复器据此决定是否重新推理，
    // 误判会把已持久化的结果变成永不交付的旧 attempt，因此必须使用下面的
    // probeObject：只有 FOUND 与 NOT_FOUND 允许推进状态，ERROR 一律视为未知。

    /** 结果对象读取上限的兜底值（调用方应传入显式上限）。 */
    public static final long DEFAULT_MAX_OBJECT_BYTES = 8L * 1024 * 1024;

    /** S3 错误码中只表示“对象/桶不存在”的集合；其余码都不是“不存在”。 */
    private static final Set<String> MISSING_ERROR_CODES = Set.of(
            "NoSuchKey", "NoSuchObject", "NoSuchBucket", "NoSuchVersion", "NotFound");

    public enum ObjectProbeStatus {
        /** statObject 成功，对象存在。 */
        FOUND,
        /** 存储明确回答“不存在”（NoSuchKey/NoSuchObject/NoSuchBucket/...）。 */
        NOT_FOUND,
        /** 无法判断（403/AccessDenied、超时、连接失败、5xx、其它异常）。 */
        ERROR
    }

    /**
     * 对象探测结果。{@code ERROR} 绝不能被上层当作“不存在”。
     *
     * @param status    FOUND / NOT_FOUND / ERROR
     * @param size      对象字节数（仅 FOUND 有意义，否则 -1）
     * @param etag      ETag（仅 FOUND 时可能非空）
     * @param errorCode S3 错误码或异常类型名（仅 ERROR 时非空；不含凭据）
     * @param message   人类可读摘要（不含签名 URL / 凭据 / 对象内容）
     */
    public record ObjectProbe(ObjectProbeStatus status, long size, String etag,
                              String errorCode, String message) {

        public static ObjectProbe found(long size, String etag) {
            return new ObjectProbe(ObjectProbeStatus.FOUND, size, etag, null, null);
        }

        public static ObjectProbe notFound(String message) {
            return new ObjectProbe(ObjectProbeStatus.NOT_FOUND, -1L, null, null, message);
        }

        public static ObjectProbe error(String errorCode, String message) {
            return new ObjectProbe(ObjectProbeStatus.ERROR, -1L, null, errorCode, message);
        }

        public boolean isFound() {
            return status == ObjectProbeStatus.FOUND;
        }

        public boolean isNotFound() {
            return status == ObjectProbeStatus.NOT_FOUND;
        }

        public boolean isError() {
            return status == ObjectProbeStatus.ERROR;
        }
    }

    /**
     * 探测对象是否存在，并把“不存在”与“无法判断”分开（协议 §6）。
     *
     * <p>FOUND：statObject 成功；NOT_FOUND：明确的 NoSuchKey/NoSuchObject/
     * NoSuchBucket/NoSuchVersion/NotFound；ERROR：403/AccessDenied、超时、
     * 连接失败、5xx 及一切其它异常。本方法不抛异常。</p>
     */
    public ObjectProbe probeObject(String bucket, String objectName) {
        try {
            StatObjectResponse stat = minioClient.statObject(StatObjectArgs.builder()
                    .bucket(bucket).object(objectName).build());
            return ObjectProbe.found(stat.size(), stat.etag());
        } catch (ErrorResponseException e) {
            String code = errorCodeOf(e);
            if (code != null && MISSING_ERROR_CODES.contains(code)) {
                return ObjectProbe.notFound("code=" + code);
            }
            return ObjectProbe.error(code, "stat failed: code=" + code
                    + " httpStatus=" + httpStatusOf(e));
        } catch (Exception e) {
            log.warn("probeObject failed for {}/{}: {}", bucket, objectName,
                    e.getClass().getSimpleName());
            // 只记录异常类型，不把可能含端点/查询串的消息写进可外露的 detail。
            return ObjectProbe.error(e.getClass().getSimpleName(),
                    "stat failed: " + e.getClass().getSimpleName());
        }
    }

    /**
     * 读取对象原始字节（结果校验必须基于原始字节做 SHA-256）。
     *
     * @param maxBytes 读取上限；超过即抛 {@link ObjectReadException}（{@code missing=false}），
     *                 防止畸形对象把堆撑爆
     * @throws ObjectReadException 存储不可达/无权限/对象不存在/超限
     */
    public byte[] readObject(String bucket, String objectName, long maxBytes) {
        long limit = maxBytes > 0 ? maxBytes : DEFAULT_MAX_OBJECT_BYTES;
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        try (GetObjectResponse response = minioClient.getObject(GetObjectArgs.builder()
                .bucket(bucket).object(objectName).build())) {
            byte[] buffer = new byte[8192];
            long total = 0;
            int read;
            while ((read = response.read(buffer)) != -1) {
                total += read;
                if (total > limit) {
                    throw new ObjectReadException(
                            "object " + objectName + " exceeds " + limit + " bytes",
                            "ObjectTooLarge", false, null);
                }
                out.write(buffer, 0, read);
            }
            return out.toByteArray();
        } catch (ObjectReadException e) {
            throw e;
        } catch (ErrorResponseException e) {
            String code = errorCodeOf(e);
            boolean missing = code != null && MISSING_ERROR_CODES.contains(code);
            throw new ObjectReadException("read failed: code=" + code
                    + " httpStatus=" + httpStatusOf(e), code, missing, e);
        } catch (Exception e) {
            log.warn("readObject failed for {}/{}: {}", bucket, objectName,
                    e.getClass().getSimpleName());
            throw new ObjectReadException("read failed: " + e.getClass().getSimpleName(),
                    e.getClass().getSimpleName(), false, e);
        }
    }

    /** 对象读取失败；{@code missing} 只在存储明确回答“不存在”时为 true。 */
    public static final class ObjectReadException extends RuntimeException {
        private final String errorCode;
        private final boolean missing;

        public ObjectReadException(String message, String errorCode, boolean missing, Throwable cause) {
            super(message, cause);
            this.errorCode = errorCode;
            this.missing = missing;
        }

        public String errorCode() {
            return errorCode;
        }

        public boolean missing() {
            return missing;
        }
    }

    private static String errorCodeOf(ErrorResponseException e) {
        try {
            return e.errorResponse() == null ? null : e.errorResponse().code();
        } catch (Exception ignored) {
            return null;
        }
    }

    private static String httpStatusOf(ErrorResponseException e) {
        try {
            return e.response() == null ? "unknown" : String.valueOf(e.response().code());
        } catch (Exception ignored) {
            return "unknown";
        }
    }

    // === 前缀列举（临时上传清理用，SEC-04） ===

    /** 桶内一个对象的摘要；只保留清理需要的字段。 */
    public record StoredObject(String objectName, long sizeBytes, Instant lastModified) { }

    /**
     * 列举某前缀下的对象。
     *
     * <p><b>用途</b>：{@code temporary-uploads} 桶此前<b>没有任何</b>清理任务——
     * 它是个"永不过期、永不回收"的写入口。预签名 PUT 无法携带
     * content-length-range（见 MediaUploadPolicy 的说明），所以服务端无法在
     * 写入那一刻拒掉超大对象；唯一可行的兜底是按对象实际写入时间定期清理，
     * 而清理必须先能列举。</p>
     *
     * <p>返回值带 {@code lastModified}，不依赖对象名里的时间戳：对象名前缀是
     * 客户端提供的、可伪造；存储时间不能。</p>
     *
     * @param prefix 对象名前缀（例如 {@code "temp/"}）；空串表示整桶
     * @return 对象摘要列表；存储不可达时返回空列表<b>并记 WARN</b>——
     *         调用方据此不会误判"桶是空的、已删干净"，日志里能看到失败
     */
    public List<StoredObject> listObjects(String bucket, String prefix) {
        List<StoredObject> objects = new ArrayList<>();
        try {
            Iterable<Result<Item>> results = minioClient.listObjects(
                    ListObjectsArgs.builder()
                            .bucket(bucket)
                            .prefix(prefix == null ? "" : prefix)
                            .recursive(true)
                            .build());
            for (Result<Item> result : results) {
                Item item = result.get();
                if (item.isDir()) {
                    continue;
                }
                objects.add(new StoredObject(item.objectName(), item.size(),
                        item.lastModified() == null ? null : item.lastModified().toInstant()));
            }
        } catch (Exception e) {
            // 只记异常类型：消息里可能带端点/凭据片段（见本类 probeObject 的注释）。
            log.warn("listObjects failed for {}/{}: {}", bucket, prefix,
                    e.getClass().getSimpleName());
        }
        return objects;
    }

    /**
     * Get presigned URL for courseware upload.
     */
    public String getCoursewareUploadUrl(String objectName) {
        return generateUploadUrl("teaching-resources", objectName, 30);
    }

    /**
     * Get presigned URL for student submission upload.
     */
    public String getSubmissionUploadUrl(String objectName) {
        return generateUploadUrl("student-submissions", objectName, 30);
    }

    /**
     * Get presigned upload URL for classroom raw media (video/audio).
     */
    public String getMediaUploadUrl(String objectName) {
        return generateUploadUrl("classroom-raw-media", objectName, 30);
    }
}