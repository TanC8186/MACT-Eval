package com.macteval.service;

import com.macteval.dto.MediaUploadConfirmRequest;
import com.macteval.dto.MediaUploadInitRequest;
import com.macteval.exception.BusinessException;
import com.macteval.model.entity.LessonSessionEntity;
import com.macteval.model.entity.MediaAssetEntity;
import com.macteval.repository.BehaviorEvidenceFrameRepository;
import com.macteval.repository.LessonSessionRepository;
import com.macteval.repository.MediaAssetRepository;
import com.macteval.security.ResourceGuard;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.test.util.ReflectionTestUtils;

import java.util.List;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * SEC-03 回归：直传的服务端校验必须真的能挡住绕过前端的调用。
 *
 * <p>每个用例对应审计里点名的一种绕过方式：超大文件、伪造 MIME、扩展名错配、
 * 错误课堂、未知 object key、重复 confirm。断言的重点是**副作用**——
 * 不合格对象必须被删除、且绝不能在删除后继续创建分析任务。</p>
 */
class MediaAssetUploadValidationTest {

    private MediaAssetRepository assetRepository;
    private MinioService minio;
    private MediaUploadService mediaUploadService;
    private LessonSessionRepository lessonSessionRepository;
    private MediaAssetService service;

    private static final String OWNER = "teacher-1";
    private static final String LESSON = "lesson-1";

    @BeforeEach
    void setUp() {
        assetRepository = mock(MediaAssetRepository.class);
        minio = mock(MinioService.class);
        mediaUploadService = mock(MediaUploadService.class);
        lessonSessionRepository = mock(LessonSessionRepository.class);

        LessonSessionEntity session = new LessonSessionEntity();
        session.setId(LESSON);
        session.setTeacherId(OWNER);
        session.setClassId("class-001");
        when(lessonSessionRepository.findById(LESSON)).thenReturn(Optional.of(session));

        MediaUploadPolicy policy = new MediaUploadPolicy();
        ReflectionTestUtils.setField(policy, "maxBytes", 1000L);
        ReflectionTestUtils.setField(policy, "maxBytesPerLesson", 2500L);
        ReflectionTestUtils.setField(policy, "allowOctetStream", false);

        service = new MediaAssetService(assetRepository,
                mock(BehaviorEvidenceFrameRepository.class), lessonSessionRepository,
                mock(ResourceGuard.class), minio, mediaUploadService, policy);
        ReflectionTestUtils.setField(service, "retentionDays", 90L);
        ReflectionTestUtils.setField(service, "maxAssetsPerLesson", 20L);
    }

    private MediaAssetEntity asset(String objectKey, String fileName, String mediaType, Long declared) {
        MediaAssetEntity a = new MediaAssetEntity();
        a.setId("asset-1");
        a.setOwnerId(OWNER);
        a.setLessonSessionId(LESSON);
        a.setBucket(MediaAssetService.MEDIA_BUCKET);
        a.setObjectKey(objectKey);
        a.setOriginalFileName(fileName);
        a.setMediaType(mediaType);
        a.setSizeBytes(declared);
        a.setStatus(MediaAssetEntity.Status.PENDING.name());
        return a;
    }

    private static MediaUploadConfirmRequest confirm(String objectName) {
        return new MediaUploadConfirmRequest("asset-1", objectName, LESSON, 45, 2, 60, 5);
    }

    // ---------------------------------------------------------------- init

    @Test
    void initRejectsDisallowedExtensionWith415() {
        BusinessException ex = assertThrows(BusinessException.class, () ->
                service.createUploadSession(new MediaUploadInitRequest(
                        "video", "payload.exe", LESSON, 100L, "video/mp4"), OWNER));
        assertEquals(415, ex.getCode());
        verify(assetRepository, never()).save(any());
    }

    @Test
    void initRejectsMimeThatDoesNotMatchExtension() {
        BusinessException ex = assertThrows(BusinessException.class, () ->
                service.createUploadSession(new MediaUploadInitRequest(
                        "video", "lesson.mp4", LESSON, 100L, "text/html"), OWNER));
        assertEquals(415, ex.getCode());
    }

    @Test
    void initRejectsDeclaredSizeAboveServerLimit() {
        BusinessException ex = assertThrows(BusinessException.class, () ->
                service.createUploadSession(new MediaUploadInitRequest(
                        "video", "lesson.mp4", LESSON, 1001L, "video/mp4"), OWNER));
        assertEquals(413, ex.getCode());
    }

    @Test
    void initRejectsWhenLessonTotalWouldExceedCapacity() {
        // 已声明 2000 字节，再声明 600 就超过 2500 的单课堂上限。
        when(assetRepository.findByLessonSessionIdAndStatusIn(anyString(), any()))
                .thenReturn(List.of(asset("k", "a.mp4", "video", 2000L)));
        BusinessException ex = assertThrows(BusinessException.class, () ->
                service.createUploadSession(new MediaUploadInitRequest(
                        "video", "lesson.mp4", LESSON, 600L, "video/mp4"), OWNER));
        assertEquals(413, ex.getCode());
    }

    @Test
    void initAcceptsValidDeclaredValues() {
        when(assetRepository.countByLessonSessionIdAndStatusIn(anyString(), any())).thenReturn(0L);
        when(assetRepository.findByLessonSessionIdAndStatusIn(anyString(), any())).thenReturn(List.of());
        when(minio.getMediaUploadUrl(anyString())).thenReturn("http://minio/presigned");

        var result = service.createUploadSession(new MediaUploadInitRequest(
                "video", "lesson.mp4", LESSON, 500L, "video/mp4"), OWNER);
        assertEquals("video", result.get("mediaType"));
        assertTrue(result.containsKey("uploadUrl"));
        verify(assetRepository).save(any());
    }

    // ------------------------------------------------------------- confirm

    @Test
    void confirmRejectsOversizedObjectAndDeletesIt() {
        when(assetRepository.findByIdAndOwnerId("asset-1", OWNER))
                .thenReturn(Optional.of(asset("k_lesson.mp4", "lesson.mp4", "video", 500L)));
        // 声明 500 字节，实际写入 5000 字节。
        when(minio.statObject(MediaAssetService.MEDIA_BUCKET, "k_lesson.mp4"))
                .thenReturn(new MinioService.ObjectInfo(5000L, "etag", "video/mp4"));
        when(minio.removeObject(anyString(), anyString())).thenReturn(true);

        BusinessException ex = assertThrows(BusinessException.class,
                () -> service.confirmUpload(confirm("k_lesson.mp4"), OWNER));
        assertEquals(413, ex.getCode());
        // 关键副作用：越界对象必须被删掉，且不得创建分析任务。
        verify(minio).removeObject(MediaAssetService.MEDIA_BUCKET, "k_lesson.mp4");
        verify(mediaUploadService, never()).triggerAnalysis(
                any(), any(), any(), any(), any(), any(), any(), any(), any(), any());
    }

    @Test
    void confirmRejectsSizeMismatchAgainstDeclaration() {
        when(assetRepository.findByIdAndOwnerId("asset-1", OWNER))
                .thenReturn(Optional.of(asset("k_lesson.mp4", "lesson.mp4", "video", 100L)));
        when(minio.statObject(MediaAssetService.MEDIA_BUCKET, "k_lesson.mp4"))
                .thenReturn(new MinioService.ObjectInfo(140L, "etag", "video/mp4"));
        when(minio.removeObject(anyString(), anyString())).thenReturn(true);

        BusinessException ex = assertThrows(BusinessException.class,
                () -> service.confirmUpload(confirm("k_lesson.mp4"), OWNER));
        assertEquals(409, ex.getCode());
        verify(minio).removeObject(MediaAssetService.MEDIA_BUCKET, "k_lesson.mp4");
        verify(mediaUploadService, never()).triggerAnalysis(
                any(), any(), any(), any(), any(), any(), any(), any(), any(), any());
    }

    @Test
    void confirmRejectsForgedMimeEvenWhenSizeIsFine() {
        when(assetRepository.findByIdAndOwnerId("asset-1", OWNER))
                .thenReturn(Optional.of(asset("k_lesson.mp4", "lesson.mp4", "video", 140L)));
        // 大小一致，但上传者把 Content-Type 设成了 text/html。
        when(minio.statObject(MediaAssetService.MEDIA_BUCKET, "k_lesson.mp4"))
                .thenReturn(new MinioService.ObjectInfo(140L, "etag", "text/html"));
        when(minio.removeObject(anyString(), anyString())).thenReturn(true);

        BusinessException ex = assertThrows(BusinessException.class,
                () -> service.confirmUpload(confirm("k_lesson.mp4"), OWNER));
        assertEquals(415, ex.getCode());
        verify(minio).removeObject(MediaAssetService.MEDIA_BUCKET, "k_lesson.mp4");
    }

    @Test
    void confirmRejectsEmptyObject() {
        when(assetRepository.findByIdAndOwnerId("asset-1", OWNER))
                .thenReturn(Optional.of(asset("k_lesson.mp4", "lesson.mp4", "video", 140L)));
        when(minio.statObject(MediaAssetService.MEDIA_BUCKET, "k_lesson.mp4"))
                .thenReturn(new MinioService.ObjectInfo(0L, "etag", "video/mp4"));
        when(minio.removeObject(anyString(), anyString())).thenReturn(true);

        BusinessException ex = assertThrows(BusinessException.class,
                () -> service.confirmUpload(confirm("k_lesson.mp4"), OWNER));
        assertEquals(400, ex.getCode());
    }

    @Test
    void confirmRejectsUnknownObjectKeyWithoutTouchingStorage() {
        when(assetRepository.findByIdAndOwnerId("asset-1", OWNER))
                .thenReturn(Optional.of(asset("k_lesson.mp4", "lesson.mp4", "video", 140L)));

        BusinessException ex = assertThrows(BusinessException.class,
                () -> service.confirmUpload(confirm("someone-elses-object.mp4"), OWNER));
        assertEquals(409, ex.getCode());
        // objectKey 与上传会话不一致时，连 stat 都不该发出——
        // 否则攻击者可以用 confirm 探测任意对象是否存在。
        verify(minio, never()).statObject(anyString(), anyString());
    }

    @Test
    void confirmRejectsAssetBelongingToAnotherLesson() {
        MediaAssetEntity a = asset("k_lesson.mp4", "lesson.mp4", "video", 140L);
        a.setLessonSessionId("lesson-other");
        when(assetRepository.findByIdAndOwnerId("asset-1", OWNER)).thenReturn(Optional.of(a));

        BusinessException ex = assertThrows(BusinessException.class,
                () -> service.confirmUpload(confirm("k_lesson.mp4"), OWNER));
        assertEquals(409, ex.getCode());
        verify(minio, never()).statObject(anyString(), anyString());
    }

    @Test
    void confirmRejectsExpiredSessionBeforeStatting() {
        MediaAssetEntity a = asset("k_lesson.mp4", "lesson.mp4", "video", 140L);
        a.setStatus(MediaAssetEntity.Status.EXPIRED.name());
        when(assetRepository.findByIdAndOwnerId("asset-1", OWNER)).thenReturn(Optional.of(a));

        BusinessException ex = assertThrows(BusinessException.class,
                () -> service.confirmUpload(confirm("k_lesson.mp4"), OWNER));
        assertEquals(409, ex.getCode());
        verify(minio, never()).statObject(anyString(), anyString());
    }

    @Test
    void confirmSucceedsAndCreatesJobWhenEverythingMatches() {
        when(assetRepository.findByIdAndOwnerId("asset-1", OWNER))
                .thenReturn(Optional.of(asset("k_lesson.mp4", "lesson.mp4", "video", 140L)));
        when(minio.statObject(MediaAssetService.MEDIA_BUCKET, "k_lesson.mp4"))
                .thenReturn(new MinioService.ObjectInfo(140L, "etag", "video/mp4"));
        when(mediaUploadService.triggerAnalysis(any(), any(), any(), any(), any(), any(),
                any(), any(), any(), any())).thenReturn(java.util.Map.of("jobId", "job-1", "status", "QUEUED"));

        var result = service.confirmUpload(confirm("k_lesson.mp4"), OWNER);
        assertEquals("job-1", result.get("jobId"));
        verify(minio, never()).removeObject(anyString(), anyString());
    }

    @Test
    void legacySessionWithoutDeclaredSizeOnlyChecksMeasuredValue() {
        // 升级前创建、升级后 confirm 的会话 sizeBytes 为 null：
        // 此时只能按实测值判断，不能因为"没有声明值"就拒绝（那会让在途上传全部失败）。
        when(assetRepository.findByIdAndOwnerId("asset-1", OWNER))
                .thenReturn(Optional.of(asset("k_lesson.mp4", "lesson.mp4", "video", null)));
        when(minio.statObject(MediaAssetService.MEDIA_BUCKET, "k_lesson.mp4"))
                .thenReturn(new MinioService.ObjectInfo(140L, "etag", "video/mp4"));
        when(mediaUploadService.triggerAnalysis(any(), any(), any(), any(), any(), any(),
                any(), any(), any(), any())).thenReturn(java.util.Map.of("jobId", "job-2", "status", "QUEUED"));

        var result = service.confirmUpload(confirm("k_lesson.mp4"), OWNER);
        assertEquals("job-2", result.get("jobId"));
    }
}
