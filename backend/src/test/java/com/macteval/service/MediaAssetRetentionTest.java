package com.macteval.service;

import com.macteval.model.entity.MediaAssetEntity;
import com.macteval.repository.BehaviorEvidenceFrameRepository;
import com.macteval.repository.LessonSessionRepository;
import com.macteval.repository.MediaAssetRepository;
import com.macteval.security.ResourceGuard;
import org.junit.jupiter.api.Test;

import java.time.LocalDateTime;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/** OPS04: a failed MinIO deletion must not be marked as expired (it would never retry). */
class MediaAssetRetentionTest {

    @Test
    void failedObjectDeletionKeepsAssetForNextRetry() {
        MediaAssetRepository assetRepository = mock(MediaAssetRepository.class);
        BehaviorEvidenceFrameRepository frameRepository = mock(BehaviorEvidenceFrameRepository.class);
        MinioService minio = mock(MinioService.class);

        MediaAssetEntity asset = new MediaAssetEntity();
        asset.setId("asset-1");
        asset.setBucket("classroom-raw-media");
        asset.setObjectKey("video.mp4");
        asset.setStatus(MediaAssetEntity.Status.PROCESSING.name());
        asset.setExpiresAt(LocalDateTime.now().minusDays(1));

        when(assetRepository.findByStatusInAndExpiresAtBefore(any(), any()))
                .thenReturn(List.of(asset));
        when(frameRepository.findByCreatedAtBefore(any())).thenReturn(List.of());
        when(assetRepository.findByStatusAndCreatedAtBefore(any(), any())).thenReturn(List.of());
        when(minio.removeObject("classroom-raw-media", "video.mp4")).thenReturn(false);

        MediaAssetService service = new MediaAssetService(
                assetRepository, frameRepository, mock(LessonSessionRepository.class),
                mock(ResourceGuard.class), minio, mock(MediaUploadService.class),
                mock(MediaUploadPolicy.class));

        service.cleanupExpiredAssets();

        assertEquals(MediaAssetEntity.Status.PROCESSING.name(), asset.getStatus());
        verify(assetRepository, never()).save(any());
    }
}
