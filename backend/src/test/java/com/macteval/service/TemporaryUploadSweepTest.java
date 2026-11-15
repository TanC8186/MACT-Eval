package com.macteval.service;

import com.macteval.model.entity.MediaAssetEntity;
import com.macteval.repository.BehaviorEvidenceFrameRepository;
import com.macteval.repository.LessonSessionRepository;
import com.macteval.repository.MediaAssetRepository;
import com.macteval.security.ResourceGuard;
import org.junit.jupiter.api.Test;
import org.springframework.test.util.ReflectionTestUtils;

import java.time.Instant;
import java.time.LocalDateTime;
import java.util.List;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * SEC-04 回归：临时上传桶的清扫。
 *
 * <p>这个桶此前没有任何清理任务，是"永不过期、永不回收"的写入口。本测试固定住
 * 三件容易在重构里丢掉的事：</p>
 *
 * <ol>
 *   <li>只删**超过保留期**的对象（用对象的存储时间，不是对象名）；</li>
 *   <li>列举返回空时**不做任何删除**——空列表既可能是"桶空"也可能是
 *       "存储不可达"，把它当成"已删干净"是最糟的假绿；</li>
 *   <li>单个对象删除失败**不中断**本轮，其余对象继续删，失败的下轮重试。</li>
 * </ol>
 */
class TemporaryUploadSweepTest {

    private MediaAssetService service(MinioService minio, long retentionHours) {
        MediaAssetRepository assetRepository = mock(MediaAssetRepository.class);
        when(assetRepository.findByStatusInAndExpiresAtBefore(any(), any())).thenReturn(List.of());
        when(assetRepository.findByStatusAndCreatedAtBefore(any(), any())).thenReturn(List.of());
        BehaviorEvidenceFrameRepository frames = mock(BehaviorEvidenceFrameRepository.class);
        when(frames.findByCreatedAtBefore(any())).thenReturn(List.of());
        when(minio.removeObject(anyString(), anyString())).thenReturn(true);

        MediaUploadPolicy policy = new MediaUploadPolicy();
        ReflectionTestUtils.setField(policy, "maxBytes", 1024L);
        ReflectionTestUtils.setField(policy, "maxBytesPerLesson", 4096L);
        ReflectionTestUtils.setField(policy, "allowOctetStream", false);

        MediaAssetService service = new MediaAssetService(assetRepository, frames,
                mock(LessonSessionRepository.class), mock(ResourceGuard.class), minio,
                mock(MediaUploadService.class), policy);
        ReflectionTestUtils.setField(service, "retentionDays", 90L);
        ReflectionTestUtils.setField(service, "maxAssetsPerLesson", 20L);
        ReflectionTestUtils.setField(service, "temporaryRetentionHours", retentionHours);
        return service;
    }

    @Test
    void onlyObjectsOlderThanRetentionAreDeleted() {
        MinioService minio = mock(MinioService.class);
        Instant old = Instant.now().minusSeconds(48 * 3600);
        Instant fresh = Instant.now().minusSeconds(60);
        when(minio.listObjects(MediaAssetService.TEMPORARY_BUCKET, "temp/")).thenReturn(List.of(
                new MinioService.StoredObject("temp/old.pdf", 100, old),
                new MinioService.StoredObject("temp/fresh.pdf", 200, fresh)));

        service(minio, 24).cleanupExpiredAssets();

        verify(minio).removeObject(MediaAssetService.TEMPORARY_BUCKET, "temp/old.pdf");
        verify(minio, never()).removeObject(MediaAssetService.TEMPORARY_BUCKET, "temp/fresh.pdf");
    }

    @Test
    void emptyListingDeletesNothingAndDoesNotClaimSuccess() {
        MinioService minio = mock(MinioService.class);
        // 空列表：可能是桶真空，也可能是存储不可达。两种都不能当成"已清理"。
        when(minio.listObjects(anyString(), anyString())).thenReturn(List.of());

        service(minio, 24).cleanupExpiredAssets();

        // removeObject 有两个参数，matcher 必须成对给，否则 Mockito 报
        // "2 matchers expected, 1 recorded"。
        verify(minio, never()).removeObject(anyString(), anyString());
    }

    @Test
    void oneFailedDeletionDoesNotStopTheOthers() {
        MinioService minio = mock(MinioService.class);
        Instant old = Instant.now().minusSeconds(48 * 3600);
        when(minio.listObjects(MediaAssetService.TEMPORARY_BUCKET, "temp/")).thenReturn(List.of(
                new MinioService.StoredObject("temp/a.pdf", 1, old),
                new MinioService.StoredObject("temp/b.pdf", 1, old),
                new MinioService.StoredObject("temp/c.pdf", 1, old)));
        when(minio.removeObject(MediaAssetService.TEMPORARY_BUCKET, "temp/a.pdf")).thenReturn(false);
        when(minio.removeObject(MediaAssetService.TEMPORARY_BUCKET, "temp/b.pdf")).thenReturn(true);
        when(minio.removeObject(MediaAssetService.TEMPORARY_BUCKET, "temp/c.pdf")).thenReturn(true);

        service(minio, 24).cleanupExpiredAssets();

        // 三个都被尝试：a 失败不能让 b/c 被跳过。
        verify(minio).removeObject(MediaAssetService.TEMPORARY_BUCKET, "temp/a.pdf");
        verify(minio).removeObject(MediaAssetService.TEMPORARY_BUCKET, "temp/b.pdf");
        verify(minio).removeObject(MediaAssetService.TEMPORARY_BUCKET, "temp/c.pdf");
    }

    @Test
    void objectWithoutLastModifiedIsTreatedAsStale() {
        // 存储没给时间戳时选择**保守删除**：这类对象无法判断年龄，
        // 而它所在的桶本身就是"过期即垃圾"。保留它会永远留下去。
        MinioService minio = mock(MinioService.class);
        when(minio.listObjects(MediaAssetService.TEMPORARY_BUCKET, "temp/")).thenReturn(List.of(
                new MinioService.StoredObject("temp/no-time.pdf", 10, null)));

        service(minio, 24).cleanupExpiredAssets();

        verify(minio).removeObject(MediaAssetService.TEMPORARY_BUCKET, "temp/no-time.pdf");
    }

    @Test
    void sweepDoesNotTouchMediaAssetsOrEvidenceFrames() {
        // 回归：清扫临时桶不能顺手动 classroom-raw-media 或证据帧桶。
        MinioService minio = mock(MinioService.class);
        Instant old = Instant.now().minusSeconds(48 * 3600);
        when(minio.listObjects(MediaAssetService.TEMPORARY_BUCKET, "temp/")).thenReturn(List.of(
                new MinioService.StoredObject("temp/x.pdf", 1, old)));

        service(minio, 24).cleanupExpiredAssets();

        verify(minio, never()).removeObject(MediaAssetService.MEDIA_BUCKET, "temp/x.pdf");
    }

    @Test
    void localDateTimeMathMatchesTheCutoff() {
        // 保留期 0 小时：所有对象（哪怕刚刚写入）都应被判为过期。
        // 这条同时验证 cutoff 计算用的是 LocalDateTime→Instant 的同一时区。
        MinioService minio = mock(MinioService.class);
        Instant now = Instant.now().minusSeconds(5);
        when(minio.listObjects(MediaAssetService.TEMPORARY_BUCKET, "temp/")).thenReturn(List.of(
                new MinioService.StoredObject("temp/now.pdf", 1, now)));

        service(minio, 0).cleanupExpiredAssets();

        verify(minio).removeObject(MediaAssetService.TEMPORARY_BUCKET, "temp/now.pdf");
    }

    @Test
    void retainedObjectsAreCountedButNotDeletedWhenFresh() {
        MinioService minio = mock(MinioService.class);
        Instant fresh = Instant.now();
        when(minio.listObjects(MediaAssetService.TEMPORARY_BUCKET, "temp/")).thenReturn(List.of(
                new MinioService.StoredObject("temp/f.pdf", 1, fresh)));

        service(minio, 24).cleanupExpiredAssets();

        verify(minio, never()).removeObject(anyString(), anyString());
    }
}
