package com.macteval.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

/**
 * 结果对象读取与字节级校验（协议 §2 规则 3、§8）。
 *
 * <p>核心不变量：只有“存储明确回答不存在”才是 NOT_FOUND；
 * 未完成写入（有 result 无 envelope）、摘要不符、字段不符都是 INVALID；
 * 存储不可达/无权限是 ERROR。后两者<b>都不能</b>触发重新推理。</p>
 */
class JobResultStoreTest {

    private static final String BUCKET = "analysis-artifacts";
    private static final String PREFIX = "analysis-results";

    private final ObjectMapper mapper = new ObjectMapper();

    private static String resultKey(String jobId, int attempt) {
        return PREFIX + "/" + jobId + "/" + attempt + "/result.json";
    }

    private static String envelopeKey(String jobId, int attempt) {
        return PREFIX + "/" + jobId + "/" + attempt + "/envelope.json";
    }

    private static byte[] envelopeBytes(String jobId, int attempt, String sha, int bytes) {
        Map<String, Object> envelope = new LinkedHashMap<>();
        envelope.put("schemaVersion", 1);
        envelope.put("jobId", jobId);
        envelope.put("attempt", attempt);
        envelope.put("resultSha256", sha);
        envelope.put("resultBytes", bytes);
        envelope.put("createdAt", "2026-10-26T00:00:00.000000Z");
        envelope.put("status", "succeeded");
        envelope.put("modelVersion", "yolov8n");
        envelope.put("modelDigest", null);
        try {
            return new ObjectMapper().writeValueAsBytes(envelope);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    @Test
    void foundResultRoundTripsUtf8Content() {
        String jobId = "job-found";
        int attempt = 2;
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("status", "succeeded");
        result.put("summary", "课堂氛围：积极，学生参与度高");
        result.put("windows", 42);
        byte[] payload;
        try {
            payload = mapper.writeValueAsBytes(result);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
        String sha = JobResultStore.sha256Hex(payload);

        MinioService minio = org.mockito.Mockito.mock(MinioService.class);
        org.mockito.Mockito.when(minio.probeObject(BUCKET, envelopeKey(jobId, attempt)))
                .thenReturn(MinioService.ObjectProbe.found(payload.length, "etag-1"));
        org.mockito.Mockito.when(minio.readObject(
                        org.mockito.ArgumentMatchers.eq(BUCKET),
                        org.mockito.ArgumentMatchers.eq(envelopeKey(jobId, attempt)),
                        org.mockito.ArgumentMatchers.anyLong()))
                .thenReturn(envelopeBytes(jobId, attempt, sha, payload.length));
        org.mockito.Mockito.when(minio.readObject(
                        org.mockito.ArgumentMatchers.eq(BUCKET),
                        org.mockito.ArgumentMatchers.eq(resultKey(jobId, attempt)),
                        org.mockito.ArgumentMatchers.anyLong()))
                .thenReturn(payload);

        JobResultStore.ResultRead read = new JobResultStore(minio, mapper).read(jobId, attempt);

        assertEquals(JobResultStore.Status.FOUND, read.status(), read.detail());
        assertEquals("succeeded", read.result().get("status"));
        assertEquals("课堂氛围：积极，学生参与度高", read.result().get("summary"));
        assertNotNull(read.ref());
        assertEquals(sha, read.ref().sha256());
        assertEquals(payload.length, read.ref().bytes());
        assertEquals(resultKey(jobId, attempt), read.ref().resultKey());
        assertEquals(BUCKET, read.ref().bucket());
    }

    @Test
    void resultWithoutEnvelopeIsInvalidUnfinishedWrite() {
        String jobId = "job-unfinished";
        int attempt = 1;
        MinioService minio = org.mockito.Mockito.mock(MinioService.class);
        org.mockito.Mockito.when(minio.probeObject(BUCKET, envelopeKey(jobId, attempt)))
                .thenReturn(MinioService.ObjectProbe.notFound("code=NoSuchKey"));
        org.mockito.Mockito.when(minio.probeObject(BUCKET, resultKey(jobId, attempt)))
                .thenReturn(MinioService.ObjectProbe.found(10, "etag"));

        JobResultStore.ResultRead read = new JobResultStore(minio, mapper).read(jobId, attempt);

        assertEquals(JobResultStore.Status.INVALID, read.status());
        assertTrue(read.detail().contains("envelope missing"), read.detail());
        // 未完成写入不得被当作“没有结果”去重排（否则可能覆盖已写好的对象）。
        assertNull(read.result());
    }

    @Test
    void digestMismatchIsInvalid() {
        String jobId = "job-bad-sha";
        int attempt = 1;
        byte[] payload = "{\"status\":\"succeeded\"}".getBytes(StandardCharsets.UTF_8);
        String wrongSha = "0".repeat(64);

        MinioService minio = org.mockito.Mockito.mock(MinioService.class);
        org.mockito.Mockito.when(minio.probeObject(BUCKET, envelopeKey(jobId, attempt)))
                .thenReturn(MinioService.ObjectProbe.found(100, "etag"));
        org.mockito.Mockito.when(minio.readObject(
                        org.mockito.ArgumentMatchers.eq(BUCKET),
                        org.mockito.ArgumentMatchers.eq(envelopeKey(jobId, attempt)),
                        org.mockito.ArgumentMatchers.anyLong()))
                .thenReturn(envelopeBytes(jobId, attempt, wrongSha, payload.length));
        org.mockito.Mockito.when(minio.readObject(
                        org.mockito.ArgumentMatchers.eq(BUCKET),
                        org.mockito.ArgumentMatchers.eq(resultKey(jobId, attempt)),
                        org.mockito.ArgumentMatchers.anyLong()))
                .thenReturn(payload);

        JobResultStore.ResultRead read = new JobResultStore(minio, mapper).read(jobId, attempt);

        assertEquals(JobResultStore.Status.INVALID, read.status());
        assertTrue(read.detail().contains("sha256 mismatch"), read.detail());
        // detail 只含摘要前缀，不含结果内容本身。
        assertFalse(read.detail().contains("succeeded"), read.detail());
    }

    @Test
    void envelopeJobIdMismatchIsInvalid() {
        String jobId = "job-mismatch";
        int attempt = 1;
        byte[] payload = "{}".getBytes(StandardCharsets.UTF_8);
        String sha = JobResultStore.sha256Hex(payload);

        MinioService minio = org.mockito.Mockito.mock(MinioService.class);
        org.mockito.Mockito.when(minio.probeObject(BUCKET, envelopeKey(jobId, attempt)))
                .thenReturn(MinioService.ObjectProbe.found(100, "etag"));
        org.mockito.Mockito.when(minio.readObject(
                        org.mockito.ArgumentMatchers.eq(BUCKET),
                        org.mockito.ArgumentMatchers.eq(envelopeKey(jobId, attempt)),
                        org.mockito.ArgumentMatchers.anyLong()))
                .thenReturn(envelopeBytes("someone-else", attempt, sha, payload.length));

        JobResultStore.ResultRead read = new JobResultStore(minio, mapper).read(jobId, attempt);

        assertEquals(JobResultStore.Status.INVALID, read.status());
        assertTrue(read.detail().contains("jobId"), read.detail());
    }

    @Test
    void envelopeAttemptMismatchIsInvalid() {
        String jobId = "job-attempt-mismatch";
        int attempt = 3;
        byte[] payload = "{}".getBytes(StandardCharsets.UTF_8);
        String sha = JobResultStore.sha256Hex(payload);

        MinioService minio = org.mockito.Mockito.mock(MinioService.class);
        org.mockito.Mockito.when(minio.probeObject(BUCKET, envelopeKey(jobId, attempt)))
                .thenReturn(MinioService.ObjectProbe.found(100, "etag"));
        org.mockito.Mockito.when(minio.readObject(
                        org.mockito.ArgumentMatchers.eq(BUCKET),
                        org.mockito.ArgumentMatchers.eq(envelopeKey(jobId, attempt)),
                        org.mockito.ArgumentMatchers.anyLong()))
                .thenReturn(envelopeBytes(jobId, attempt - 1, sha, payload.length));

        JobResultStore.ResultRead read = new JobResultStore(minio, mapper).read(jobId, attempt);

        assertEquals(JobResultStore.Status.INVALID, read.status());
        assertTrue(read.detail().contains("attempt"), read.detail());
    }

    @Test
    void unsupportedSchemaVersionIsInvalid() {
        String jobId = "job-schema";
        int attempt = 1;
        byte[] payload = "{}".getBytes(StandardCharsets.UTF_8);
        String sha = JobResultStore.sha256Hex(payload);
        Map<String, Object> envelope = new LinkedHashMap<>();
        envelope.put("schemaVersion", 2);
        envelope.put("jobId", jobId);
        envelope.put("attempt", attempt);
        envelope.put("resultSha256", sha);
        envelope.put("resultBytes", payload.length);
        byte[] envelopeJson;
        try {
            envelopeJson = mapper.writeValueAsBytes(envelope);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }

        MinioService minio = org.mockito.Mockito.mock(MinioService.class);
        org.mockito.Mockito.when(minio.probeObject(BUCKET, envelopeKey(jobId, attempt)))
                .thenReturn(MinioService.ObjectProbe.found(100, "etag"));
        org.mockito.Mockito.when(minio.readObject(
                        org.mockito.ArgumentMatchers.eq(BUCKET),
                        org.mockito.ArgumentMatchers.eq(envelopeKey(jobId, attempt)),
                        org.mockito.ArgumentMatchers.anyLong()))
                .thenReturn(envelopeJson);

        JobResultStore.ResultRead read = new JobResultStore(minio, mapper).read(jobId, attempt);

        assertEquals(JobResultStore.Status.INVALID, read.status());
        assertTrue(read.detail().contains("schemaVersion"), read.detail());
    }

    @Test
    void bothObjectsMissingIsNotFound() {
        String jobId = "job-missing";
        int attempt = 1;
        MinioService minio = org.mockito.Mockito.mock(MinioService.class);
        org.mockito.Mockito.when(minio.probeObject(BUCKET, envelopeKey(jobId, attempt)))
                .thenReturn(MinioService.ObjectProbe.notFound("code=NoSuchKey"));
        org.mockito.Mockito.when(minio.probeObject(BUCKET, resultKey(jobId, attempt)))
                .thenReturn(MinioService.ObjectProbe.notFound("code=NoSuchKey"));

        JobResultStore.ResultRead read = new JobResultStore(minio, mapper).read(jobId, attempt);

        assertEquals(JobResultStore.Status.NOT_FOUND, read.status());
        assertNull(read.ref());
    }

    @Test
    void storeErrorIsNeverTreatedAsNotFound() {
        String jobId = "job-store-error";
        int attempt = 1;
        MinioService minio = org.mockito.Mockito.mock(MinioService.class);
        org.mockito.Mockito.when(minio.probeObject(BUCKET, envelopeKey(jobId, attempt)))
                .thenReturn(MinioService.ObjectProbe.error("AccessDenied", "stat failed"));

        JobResultStore.ResultRead read = new JobResultStore(minio, mapper).read(jobId, attempt);

        assertEquals(JobResultStore.Status.ERROR, read.status());
        assertNotEquals(JobResultStore.Status.NOT_FOUND, read.status());
        assertNotEquals(JobResultStore.Status.INVALID, read.status());
        // 未探测 result.json：无法判断时不能继续猜测。
        org.mockito.Mockito.verify(minio, org.mockito.Mockito.never())
                .probeObject(BUCKET, resultKey(jobId, attempt));
    }

    @Test
    void envelopeMissingButResultProbeErrorIsErrorNotNotFound() {
        String jobId = "job-half-error";
        int attempt = 1;
        MinioService minio = org.mockito.Mockito.mock(MinioService.class);
        org.mockito.Mockito.when(minio.probeObject(BUCKET, envelopeKey(jobId, attempt)))
                .thenReturn(MinioService.ObjectProbe.notFound("code=NoSuchKey"));
        org.mockito.Mockito.when(minio.probeObject(BUCKET, resultKey(jobId, attempt)))
                .thenReturn(MinioService.ObjectProbe.error("TimeoutException", "stat failed"));

        JobResultStore.ResultRead read = new JobResultStore(minio, mapper).read(jobId, attempt);

        assertEquals(JobResultStore.Status.ERROR, read.status(),
                "无法确认 result.json 不存在时不得判 NOT_FOUND");
    }

    @Test
    void readFailureOnResultObjectIsInvalidOrError() {
        String jobId = "job-read-fail";
        int attempt = 1;
        byte[] payload = "{}".getBytes(StandardCharsets.UTF_8);
        String sha = JobResultStore.sha256Hex(payload);

        MinioService minio = org.mockito.Mockito.mock(MinioService.class);
        org.mockito.Mockito.when(minio.probeObject(BUCKET, envelopeKey(jobId, attempt)))
                .thenReturn(MinioService.ObjectProbe.found(100, "etag"));
        org.mockito.Mockito.when(minio.readObject(
                        org.mockito.ArgumentMatchers.eq(BUCKET),
                        org.mockito.ArgumentMatchers.eq(envelopeKey(jobId, attempt)),
                        org.mockito.ArgumentMatchers.anyLong()))
                .thenReturn(envelopeBytes(jobId, attempt, sha, payload.length));
        org.mockito.Mockito.when(minio.readObject(
                        org.mockito.ArgumentMatchers.eq(BUCKET),
                        org.mockito.ArgumentMatchers.eq(resultKey(jobId, attempt)),
                        org.mockito.ArgumentMatchers.anyLong()))
                .thenThrow(new MinioService.ObjectReadException(
                        "read failed: NoSuchKey", "NoSuchKey", true, null));

        JobResultStore.ResultRead read = new JobResultStore(minio, mapper).read(jobId, attempt);

        // envelope 已在但 result 消失：存储不一致，不是“没有结果”。
        assertEquals(JobResultStore.Status.INVALID, read.status());
        assertTrue(read.detail().contains("missing"), read.detail());
    }

    @Test
    void detailNeverContainsUrlsOrCredentials() {
        String jobId = "job-detail";
        int attempt = 1;
        byte[] payload = "{\"status\":\"succeeded\",\"secret\":\"should-not-leak\"}"
                .getBytes(StandardCharsets.UTF_8);
        MinioService minio = org.mockito.Mockito.mock(MinioService.class);
        org.mockito.Mockito.when(minio.probeObject(BUCKET, envelopeKey(jobId, attempt)))
                .thenReturn(MinioService.ObjectProbe.error("InvalidResponse", "stat failed"));
        JobResultStore.ResultRead errorRead = new JobResultStore(minio, mapper).read(jobId, attempt);

        MinioService minio2 = org.mockito.Mockito.mock(MinioService.class);
        org.mockito.Mockito.when(minio2.probeObject(BUCKET, envelopeKey(jobId, attempt)))
                .thenReturn(MinioService.ObjectProbe.found(10, "etag"));
        org.mockito.Mockito.when(minio2.readObject(
                        org.mockito.ArgumentMatchers.eq(BUCKET),
                        org.mockito.ArgumentMatchers.eq(envelopeKey(jobId, attempt)),
                        org.mockito.ArgumentMatchers.anyLong()))
                .thenReturn(envelopeBytes(jobId, attempt, "f".repeat(64), payload.length));
        org.mockito.Mockito.when(minio2.readObject(
                        org.mockito.ArgumentMatchers.eq(BUCKET),
                        org.mockito.ArgumentMatchers.eq(resultKey(jobId, attempt)),
                        org.mockito.ArgumentMatchers.anyLong()))
                .thenReturn(payload);
        JobResultStore.ResultRead invalidRead = new JobResultStore(minio2, mapper).read(jobId, attempt);

        for (String detail : new String[]{errorRead.detail(), invalidRead.detail()}) {
            assertNotNull(detail);
            assertFalse(detail.contains("http://"), detail);
            assertFalse(detail.contains("https://"), detail);
            assertFalse(detail.contains("X-Amz-Signature"), detail);
            assertFalse(detail.contains("should-not-leak"), detail);
            assertFalse(detail.contains("macteval_secret"), detail);
        }
    }
}
