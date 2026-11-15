package com.macteval.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.security.MessageDigest;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 未交付任务结果的只读访问（协议 §2 / §8）。
 *
 * <p>AI worker 在 ACK 之前把结果写入私有桶的两个对象：
 * <code>{prefix}/{jobId}/{attempt}/result.json</code>（规范 JSON 的原始字节）与
 * <code>envelope.json</code>（提交标记）。本类只负责“读回并做字节级校验”，
 * 决定恢复器能否直接把结果交付出去：</p>
 *
 * <ul>
 *   <li>envelope 缺失 + result 存在 → 未完成的写入，{@link Status#INVALID}，
 *       <b>不得</b>当作“没有结果”去重新推理（那会覆盖/丢弃已写好的对象）。</li>
 *   <li>存储不可达 / 403 / 5xx → {@link Status#ERROR}，同样不重排。</li>
 *   <li>只有确认 envelope 与 result 都不存在才是 {@link Status#NOT_FOUND}。</li>
 * </ul>
 *
 * <p>本类不写数据库、不签发 URL、不在 detail 中放签名 URL/凭据/结果内容
 * （只放长度、摘要前缀等可审计摘要）。</p>
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class JobResultStore {

    /** 支持的 envelope schema 版本。 */
    public static final int SUPPORTED_SCHEMA_VERSION = 1;

    private static final String RESULT_OBJECT = "result.json";
    private static final String ENVELOPE_OBJECT = "envelope.json";

    /** envelope 很小；上限只用于防止畸形对象撑爆堆。 */
    private static final long MAX_ENVELOPE_BYTES = 64L * 1024;

    private final MinioService minioService;
    private final ObjectMapper objectMapper;

    @Value("${mact.result-store.bucket:analysis-artifacts}")
    private String bucket = "analysis-artifacts";

    @Value("${mact.result-store.prefix:analysis-results}")
    private String prefix = "analysis-results";

    @Value("${mact.result-store.max-result-bytes:8388608}")
    private long maxResultBytes = 8L * 1024 * 1024;

    public enum Status {
        /** 结果存在且通过全部校验，可交付。 */
        FOUND,
        /** 存储明确确认 (jobId, attempt) 没有已提交的结果。 */
        NOT_FOUND,
        /** 结果存在但不可信（未完成写入、摘要/字段不符、无法解析）。不得重排。 */
        INVALID,
        /** 存储不可达/无权限/超时，无法判断。不得重排。 */
        ERROR
    }

    /** 已校验结果的引用；键值可审计，不含签名 URL。 */
    public record ResultRef(String jobId, int attempt, String bucket, String resultKey,
                            String envelopeKey, String sha256, long bytes) {

        /** 审计用 JSON（可直接写入 analysis_jobs.last_result_ref）。 */
        public Map<String, Object> toMap() {
            Map<String, Object> map = new LinkedHashMap<>();
            map.put("jobId", jobId);
            map.put("attempt", attempt);
            map.put("bucket", bucket);
            map.put("resultKey", resultKey);
            map.put("envelopeKey", envelopeKey);
            map.put("sha256", sha256);
            map.put("bytes", bytes);
            return map;
        }
    }

    /**
     * 一次读取判定。
     *
     * @param status FOUND / NOT_FOUND / INVALID / ERROR
     * @param result 仅 FOUND 时为解析后的结果
     * @param ref    仅 FOUND 时为结果引用
     * @param detail 可审计的简短说明（不含 URL/凭据/结果内容）
     */
    public record ResultRead(Status status, Map<String, Object> result, ResultRef ref, String detail) {

        static ResultRead found(Map<String, Object> result, ResultRef ref) {
            return new ResultRead(Status.FOUND, result, ref, "ok");
        }

        static ResultRead notFound(String detail) {
            return new ResultRead(Status.NOT_FOUND, null, null, detail);
        }

        static ResultRead invalid(String detail) {
            return new ResultRead(Status.INVALID, null, null, detail);
        }

        static ResultRead error(String detail) {
            return new ResultRead(Status.ERROR, null, null, detail);
        }
    }

    /**
     * 读取并校验 <code>(jobId, attempt)</code> 对应的结果（协议 §2 规则 3）。
     *
     * <p>顺序：探测 envelope → 校验 jobId/attempt/schemaVersion → 读 result.json
     * 原始字节 → 校验长度与 SHA-256 → 才解析 JSON。</p>
     */
    public ResultRead read(String jobId, int attempt) {
        String resultKey = objectKey(jobId, attempt, RESULT_OBJECT);
        String envelopeKey = objectKey(jobId, attempt, ENVELOPE_OBJECT);

        MinioService.ObjectProbe envelopeProbe = minioService.probeObject(bucket, envelopeKey);
        if (envelopeProbe.isError()) {
            return ResultRead.error("envelope probe failed: " + envelopeProbe.errorCode());
        }
        if (envelopeProbe.isNotFound()) {
            // envelope 是提交标记：先确认 result.json 也不存在，否则是未完成写入。
            MinioService.ObjectProbe resultProbe = minioService.probeObject(bucket, resultKey);
            if (resultProbe.isFound()) {
                return ResultRead.invalid("envelope missing (unfinished write)");
            }
            if (resultProbe.isError()) {
                // 无法确认 result.json 是否也不存在 → 不能判 NOT_FOUND，否则可能重复推理。
                return ResultRead.error("result probe failed: " + resultProbe.errorCode());
            }
            return ResultRead.notFound("no result object for this attempt");
        }

        byte[] envelopeBytes;
        try {
            envelopeBytes = minioService.readObject(bucket, envelopeKey, MAX_ENVELOPE_BYTES);
        } catch (MinioService.ObjectReadException e) {
            return e.missing() ? ResultRead.notFound("envelope vanished during read")
                    : ResultRead.error("envelope read failed: " + e.errorCode());
        }

        Map<String, Object> envelope;
        try {
            envelope = objectMapper.readValue(envelopeBytes, new TypeReference<Map<String, Object>>() {});
        } catch (Exception e) {
            return ResultRead.invalid("envelope is not valid JSON object");
        }
        if (envelope == null) {
            return ResultRead.invalid("envelope is not valid JSON object");
        }

        Integer schemaVersion = asInt(envelope.get("schemaVersion"));
        if (schemaVersion == null || schemaVersion != SUPPORTED_SCHEMA_VERSION) {
            return ResultRead.invalid("unsupported schemaVersion " + envelope.get("schemaVersion"));
        }
        if (!jobId.equals(asString(envelope.get("jobId")))) {
            return ResultRead.invalid("envelope jobId mismatch");
        }
        Integer envelopeAttempt = asInt(envelope.get("attempt"));
        if (envelopeAttempt == null || envelopeAttempt != attempt) {
            return ResultRead.invalid("envelope attempt mismatch");
        }
        String expectedSha = asString(envelope.get("resultSha256"));
        if (expectedSha == null || expectedSha.length() != 64 || !isHex(expectedSha)) {
            return ResultRead.invalid("envelope resultSha256 malformed");
        }
        Integer declaredBytes = asInt(envelope.get("resultBytes"));
        if (declaredBytes == null || declaredBytes < 0) {
            return ResultRead.invalid("envelope resultBytes malformed");
        }
        if (declaredBytes > maxResultBytes) {
            return ResultRead.invalid("envelope resultBytes exceeds read limit");
        }

        byte[] payload;
        try {
            payload = minioService.readObject(bucket, resultKey,
                    Math.max(1L, Math.min(maxResultBytes, declaredBytes + 1L)));
        } catch (MinioService.ObjectReadException e) {
            if (e.missing()) {
                return ResultRead.invalid("envelope exists but result object is missing");
            }
            if ("ObjectTooLarge".equals(e.errorCode())) {
                // 对象比 envelope 声明的大：写入被截断或 envelope 与对象不是一对，属不可信。
                return ResultRead.invalid("result object larger than envelope declares");
            }
            return ResultRead.error("result read failed: " + e.errorCode());
        }

        if (payload.length != declaredBytes) {
            return ResultRead.invalid("result size " + payload.length
                    + " != envelope " + declaredBytes);
        }
        String actualSha = sha256Hex(payload);
        if (!actualSha.equalsIgnoreCase(expectedSha)) {
            // 只暴露摘要前缀，便于审计且不泄露内容。
            return ResultRead.invalid("result sha256 mismatch (stored "
                    + prefix(actualSha) + ", envelope " + prefix(expectedSha) + ")");
        }

        Map<String, Object> parsed;
        try {
            parsed = objectMapper.readValue(payload, new TypeReference<Map<String, Object>>() {});
        } catch (Exception e) {
            return ResultRead.invalid("result.json is not a JSON object");
        }
        if (parsed == null) {
            return ResultRead.invalid("result.json is not a JSON object");
        }

        ResultRef ref = new ResultRef(jobId, attempt, bucket, resultKey, envelopeKey,
                actualSha.toLowerCase(), payload.length);
        return ResultRead.found(parsed, ref);
    }

    /** 结果对象键：<code>{prefix}/{jobId}/{attempt}/{name}</code>。 */
    public String objectKey(String jobId, int attempt, String name) {
        String base = normalizedPrefix() + "/" + jobId + "/" + attempt;
        return base + "/" + name;
    }

    public String bucket() {
        return bucket;
    }

    private String normalizedPrefix() {
        String value = prefix == null ? "" : prefix.trim();
        while (value.startsWith("/")) {
            value = value.substring(1);
        }
        while (value.endsWith("/")) {
            value = value.substring(0, value.length() - 1);
        }
        return value.isEmpty() ? "analysis-results" : value;
    }

    private static Integer asInt(Object value) {
        if (value instanceof Integer i) {
            return i;
        }
        if (value instanceof Long l && l >= Integer.MIN_VALUE && l <= Integer.MAX_VALUE) {
            return l.intValue();
        }
        // 浮点/字符串/布尔一律拒绝：envelope 的数值字段必须是 JSON 整数。
        return null;
    }

    private static String asString(Object value) {
        return value instanceof String s ? s : null;
    }

    private static boolean isHex(String value) {
        for (int i = 0; i < value.length(); i++) {
            char c = value.charAt(i);
            boolean hex = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
            if (!hex) {
                return false;
            }
        }
        return true;
    }

    private static String prefix(String sha) {
        return sha == null ? "?" : sha.substring(0, Math.min(12, sha.length()));
    }

    static String sha256Hex(byte[] payload) {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            return HexFormat.of().formatHex(digest.digest(payload));
        } catch (Exception e) {
            throw new IllegalStateException("SHA-256 unavailable", e);
        }
    }

    /** 测试与诊断用：当前配置的 UTF-8 结果读取上限。 */
    public long maxResultBytes() {
        return maxResultBytes;
    }

}
