package com.macteval.service;

import com.macteval.model.entity.AnalysisJobEntity;
import com.macteval.model.entity.LessonSessionEntity;
import com.macteval.repository.LessonSessionRepository;
import com.macteval.security.ResourceGuard;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.security.access.AccessDeniedException;

import java.util.Map;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class MediaUploadServiceTest {

    private LessonSessionRepository lessonSessionRepository;
    private AnalysisJobService analysisJobService;
    private MinioService minioService;
    private MediaUploadService service;

    @BeforeEach
    void setUp() {
        lessonSessionRepository = mock(LessonSessionRepository.class);
        analysisJobService = mock(AnalysisJobService.class);
        minioService = mock(MinioService.class);
        service = new MediaUploadService(lessonSessionRepository, analysisJobService, new ResourceGuard(), minioService);
    }

    @Test
    void triggerVideoCreatesVideoJobWithParams() {
        when(lessonSessionRepository.findById("lesson-1"))
                .thenReturn(Optional.of(session("lesson-1", "teacher-1")));
        when(minioService.objectExists("classroom-raw-media", "f1.mp4")).thenReturn(true);
        AnalysisJobEntity job = new AnalysisJobEntity();
        job.setId("job-v");
        job.setStatus(AnalysisJobEntity.JobStatus.QUEUED);
        when(analysisJobService.createJob(any(), any(), any(), any(), any())).thenReturn(job);

        var result = service.triggerAnalysis(
                "video", "f1.mp4", "课堂视频.mp4", "lesson-1", 45, 2, 60, null, "teacher-1");

        assertEquals("job-v", result.get("jobId"));
        org.mockito.ArgumentCaptor<Map> paramsCaptor = org.mockito.ArgumentCaptor.forClass(Map.class);
        verify(analysisJobService).createJob(
                eq(AnalysisJobEntity.JobType.VIDEO_ANALYSIS), paramsCaptor.capture(), eq("f1.mp4"),
                eq("teacher-1"), eq("lesson-1"));
        Map<String, Object> params = paramsCaptor.getValue();
        assertEquals("video", params.get("mediaType"));
        assertEquals("classroom-raw-media", params.get("bucket"));
        assertEquals("lesson-1", params.get("lessonSessionId"));
        assertEquals(45, params.get("totalSeats"));
        assertEquals(60, params.get("windowDuration"));
        // 默认不写入 minObservations，由 AI worker 使用默认值 5
        assertEquals(false, params.containsKey("minObservations"));
    }

    @Test
    void triggerVideoPassesExplicitMinObservations() {
        when(lessonSessionRepository.findById("lesson-1"))
                .thenReturn(Optional.of(session("lesson-1", "teacher-1")));
        when(minioService.objectExists("classroom-raw-media", "f1.mp4")).thenReturn(true);
        AnalysisJobEntity job = new AnalysisJobEntity();
        job.setId("job-v2");
        job.setStatus(AnalysisJobEntity.JobStatus.QUEUED);
        when(analysisJobService.createJob(any(), any(), any(), any(), any())).thenReturn(job);

        service.triggerAnalysis(
                "video", "f1.mp4", "v.mp4", "lesson-1", 45, 1, 30, 3, "teacher-1");

        org.mockito.ArgumentCaptor<Map> paramsCaptor = org.mockito.ArgumentCaptor.forClass(Map.class);
        verify(analysisJobService).createJob(
                eq(AnalysisJobEntity.JobType.VIDEO_ANALYSIS), paramsCaptor.capture(), eq("f1.mp4"),
                eq("teacher-1"), eq("lesson-1"));
        assertEquals(3, paramsCaptor.getValue().get("minObservations"));
    }

    @Test
    void triggerAudioCreatesAudioJobWithoutVideoParams() {
        when(lessonSessionRepository.findById("lesson-1"))
                .thenReturn(Optional.of(session("lesson-1", "teacher-1")));
        when(minioService.objectExists("classroom-raw-media", "f1.wav")).thenReturn(true);
        AnalysisJobEntity job = new AnalysisJobEntity();
        job.setId("job-a");
        job.setStatus(AnalysisJobEntity.JobStatus.QUEUED);
        when(analysisJobService.createJob(any(), any(), any(), any(), any())).thenReturn(job);

        var result = service.triggerAnalysis(
                "audio", "f1.wav", "课堂音频.wav", "lesson-1", null, null, null, null, "teacher-1");

        assertEquals("job-a", result.get("jobId"));
        org.mockito.ArgumentCaptor<Map> paramsCaptor = org.mockito.ArgumentCaptor.forClass(Map.class);
        verify(analysisJobService).createJob(
                eq(AnalysisJobEntity.JobType.AUDIO_ANALYSIS), paramsCaptor.capture(), eq("f1.wav"),
                eq("teacher-1"), eq("lesson-1"));
        Map<String, Object> params = paramsCaptor.getValue();
        assertEquals("audio", params.get("mediaType"));
        assertEquals("lesson-1", params.get("lessonSessionId"));
        assertEquals(false, params.containsKey("totalSeats"));
    }

    @Test
    void triggerRejectsNonOwner() {
        when(lessonSessionRepository.findById("lesson-1"))
                .thenReturn(Optional.of(session("lesson-1", "teacher-1")));

        assertThrows(AccessDeniedException.class,
                () -> service.triggerAnalysis("video", "f1.mp4", "v.mp4", "lesson-1", null, null, null, null, "teacher-2"));
    }

    @Test
    void triggerRejectsInvalidMediaType() {
        when(lessonSessionRepository.findById("lesson-1"))
                .thenReturn(Optional.of(session("lesson-1", "teacher-1")));

        assertThrows(IllegalArgumentException.class,
                () -> service.triggerAnalysis("other", "f1.mp4", "v.mp4", "lesson-1", null, null, null, null, "teacher-1"));
    }

    @Test
    void triggerRejectsWhenMediaObjectNotUploaded() {
        when(lessonSessionRepository.findById("lesson-1"))
                .thenReturn(Optional.of(session("lesson-1", "teacher-1")));
        // 对象不存在 → 拒绝创建任务
        when(minioService.objectExists("classroom-raw-media", "f1.mp4")).thenReturn(false);

        assertThrows(IllegalArgumentException.class,
                () -> service.triggerAnalysis("video", "f1.mp4", "v.mp4", "lesson-1", 45, 2, 60, null, "teacher-1"));
        // 不得创建任何任务
        verify(analysisJobService, org.mockito.Mockito.never()).createJob(any(), any(), any(), any(), any());
    }

    private LessonSessionEntity session(String id, String teacherId) {
        LessonSessionEntity s = new LessonSessionEntity();
        s.setId(id);
        s.setTeacherId(teacherId);
        return s;
    }
}
