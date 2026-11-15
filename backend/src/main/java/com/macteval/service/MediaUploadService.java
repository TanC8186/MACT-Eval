package com.macteval.service;

import com.macteval.model.entity.AnalysisJobEntity;
import com.macteval.model.entity.LessonSessionEntity;
import com.macteval.repository.LessonSessionRepository;
import com.macteval.security.ResourceGuard;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.HashMap;
import java.util.Map;
import java.util.NoSuchElementException;
import java.util.Set;

@Service
@RequiredArgsConstructor
public class MediaUploadService {

    private static final String MEDIA_BUCKET = MediaAssetService.MEDIA_BUCKET;

    private final LessonSessionRepository lessonSessionRepository;
    private final AnalysisJobService analysisJobService;
    private final ResourceGuard resourceGuard;
    private final MinioService minioService;

    /**
     * 属主校验后创建 VIDEO/AUDIO 分析任务。job 参数键为 camelCase，与 ai-service worker 读取一致。
     *
     * @param minObservations 每窗最少可用观测数；null 时不写入，由 AI worker 使用默认值 5
     */
    @Transactional
    public Map<String, String> triggerAnalysis(
            String mediaType, String objectName, String fileName, String lessonSessionId,
            Integer totalSeats, Integer sampleInterval, Integer windowDuration,
            Integer minObservations,
            String authenticatedTeacherId) {
        return triggerAnalysis(mediaType, objectName, fileName, lessonSessionId,
                totalSeats, sampleInterval, windowDuration, minObservations,
                authenticatedTeacherId, null);
    }

    @Transactional
    public Map<String, String> triggerAnalysis(
            String mediaType, String objectName, String fileName, String lessonSessionId,
            Integer totalSeats, Integer sampleInterval, Integer windowDuration,
            Integer minObservations,
            String authenticatedTeacherId, String mediaAssetId) {
        if (!Set.of("video", "audio").contains(mediaType)) {
            throw new IllegalArgumentException("mediaType 仅支持 video 或 audio");
        }
        LessonSessionEntity session = lessonSessionRepository.findById(lessonSessionId)
                .orElseThrow(() -> new NoSuchElementException("课堂不存在"));
        resourceGuard.assertTeacherOwnsCourse(authenticatedTeacherId, session.getTeacherId());

        // 两段式上传确认：任务创建前必须确认对象已真实上传完成，避免产生 worker 永远取不到文件的僵尸任务。
        if (!minioService.objectExists(MEDIA_BUCKET, objectName)) {
            throw new IllegalArgumentException("媒体文件尚未上传完成，请先完成上传再发起分析: " + objectName);
        }

        Map<String, Object> params = new HashMap<>();
        params.put("mediaType", mediaType);
        params.put("objectName", objectName);
        params.put("bucket", MEDIA_BUCKET);
        params.put("originalFileName", fileName);
        params.put("lessonSessionId", lessonSessionId);
        if (mediaAssetId != null) {
            params.put("mediaAssetId", mediaAssetId);
        }
        if ("video".equals(mediaType)) {
            params.put("totalSeats", totalSeats != null ? totalSeats : 45);
            params.put("sampleInterval", sampleInterval != null ? sampleInterval : 2);
            params.put("windowDuration", windowDuration != null ? windowDuration : 60);
            if (minObservations != null) {
                if (minObservations < 1 || minObservations > 60) {
                    throw new IllegalArgumentException("minObservations 需在 1-60 之间");
                }
                params.put("minObservations", minObservations);
            }
        }

        AnalysisJobEntity.JobType type = "video".equals(mediaType)
                ? AnalysisJobEntity.JobType.VIDEO_ANALYSIS
                : AnalysisJobEntity.JobType.AUDIO_ANALYSIS;
        AnalysisJobEntity job = analysisJobService.createJob(
                type, params, objectName, authenticatedTeacherId, lessonSessionId);
        return Map.of("jobId", job.getId(), "status", job.getStatus().name());
    }
}
