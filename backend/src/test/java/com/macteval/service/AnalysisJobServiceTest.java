package com.macteval.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.macteval.model.entity.AnalysisJobEntity;
import com.macteval.repository.AnalysisJobRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.data.redis.core.RedisTemplate;
import org.springframework.data.redis.core.StreamOperations;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyMap;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class AnalysisJobServiceTest {

    private AnalysisJobRepository jobRepository;
    private RedisTemplate<String, String> redisTemplate;
    private BehaviorService behaviorService;
    private TranscriptService transcriptService;
    private WindowAlignmentService windowAlignmentService;
    private AnalysisJobService service;

    @BeforeEach
    void setUp() {
        jobRepository = mock(AnalysisJobRepository.class);
        redisTemplate = mock(RedisTemplate.class);
        StreamOperations<String, Object, Object> streamOps = mock(StreamOperations.class);
        when(redisTemplate.opsForStream()).thenReturn(streamOps);
        behaviorService = mock(BehaviorService.class);
        transcriptService = mock(TranscriptService.class);
        windowAlignmentService = mock(WindowAlignmentService.class);
        service = new AnalysisJobService(jobRepository, mock(com.macteval.repository.TaskOutboxRepository.class), redisTemplate, new ObjectMapper(), behaviorService, transcriptService, windowAlignmentService);
    }

    @Test
    void createJobTruncatesOverlongSourceHash() {
        when(jobRepository.save(org.mockito.ArgumentMatchers.any(AnalysisJobEntity.class)))
                .thenAnswer(inv -> inv.getArgument(0));

        String longSource = "x".repeat(300);
        var job = service.createJob(AnalysisJobEntity.JobType.VIDEO_ANALYSIS, java.util.Map.of(), longSource);

        assertEquals(255, job.getSourceHash().length());
        assertEquals("x".repeat(255), job.getSourceHash());
    }

    @Test
    void duplicateCallbackOnTerminalJobDoesNotReingest() {
        AnalysisJobEntity job = new AnalysisJobEntity();
        job.setId("job-done");
        job.setType(AnalysisJobEntity.JobType.VIDEO_ANALYSIS);
        job.setStatus(AnalysisJobEntity.JobStatus.SUCCEEDED);
        job.setParams("{\"lessonSessionId\":\"lesson-1\"}");
        job.setResultMetadata("{\"status\":\"succeeded\",\"observations\":[]}");
        when(jobRepository.findByIdForUpdate("job-done")).thenReturn(java.util.Optional.of(job));

        service.completeJob("job-done", Map.of("status", "succeeded"));

        verify(behaviorService, org.mockito.Mockito.never()).ingestCompletedVideoJob(org.mockito.ArgumentMatchers.any());
        verify(jobRepository, org.mockito.Mockito.never()).save(org.mockito.ArgumentMatchers.any());
    }

    @Test
    void completeAudioJobTriggersWindowAlignment() throws Exception {
        AnalysisJobEntity job = new AnalysisJobEntity();
        job.setId("job-audio-1");
        job.setType(AnalysisJobEntity.JobType.AUDIO_ANALYSIS);
        job.setStatus(AnalysisJobEntity.JobStatus.RUNNING);
        job.setParams("{\"lessonSessionId\":\"ls-1\",\"objectName\":\"f1_audio.mp3\",\"bucket\":\"classroom-raw-media\"}");
        job.setResultMetadata("{\"status\":\"succeeded\",\"text\":\"今天上课\",\"segments\":[]}");
        when(jobRepository.findByIdForUpdate("job-audio-1")).thenReturn(java.util.Optional.of(job));

        service.completeJob("job-audio-1", Map.of("status", "succeeded", "text", "今天上课", "segments", List.of()));

        verify(windowAlignmentService).alignLesson("ls-1", null, "job-audio-1");
    }

    @Test
    void completeVideoJobCallsBehaviorIngest() throws Exception {
        AnalysisJobEntity job = new AnalysisJobEntity();
        job.setId("job-v");
        job.setType(AnalysisJobEntity.JobType.VIDEO_ANALYSIS);
        job.setStatus(AnalysisJobEntity.JobStatus.RUNNING);
        job.setParams("{\"lessonSessionId\":\"lesson-1\"}");
        job.setResultMetadata("{\"status\":\"succeeded\",\"observations\":[]}");
        when(jobRepository.findByIdForUpdate("job-v")).thenReturn(java.util.Optional.of(job));
        when(jobRepository.save(job)).thenReturn(job);

        service.completeJob("job-v", Map.of("status", "succeeded"));

        verify(behaviorService).ingestCompletedVideoJob(job);
    }

    /**
     * 对齐失败仍保持任务 SUCCEEDED（报告要引用它的 analysisVideoJobId），
     * 但必须留下 alignment_state=PENDING，恢复器才能发现并只重建派生窗口（R03）。
     *
     * <p>原用例把 stub 打在单参重载 {@code alignLesson(String)} 上，服务实际调用的是
     * 三参重载——对齐从未真正失败，断言等于空转。这里改为 stub 三参重载并断言阶段标记。
     */
    @Test
    void completeVideoJobKeepsSucceededButMarksAlignmentPendingWhenAlignmentFails() throws Exception {
        AnalysisJobEntity job = new AnalysisJobEntity();
        job.setId("job-align-fail");
        job.setType(AnalysisJobEntity.JobType.VIDEO_ANALYSIS);
        job.setStatus(AnalysisJobEntity.JobStatus.RUNNING);
        job.setParams("{\"lessonSessionId\":\"ls-fail\"}");
        job.setResultMetadata("{\"status\":\"succeeded\"}");
        when(jobRepository.findByIdForUpdate("job-align-fail")).thenReturn(java.util.Optional.of(job));
        when(jobRepository.save(job)).thenReturn(job);
        doThrow(new RuntimeException("boom"))
                .when(windowAlignmentService).alignLesson(anyString(), any(), any());

        service.completeJob("job-align-fail", Map.of("status", "succeeded"));

        assertEquals(AnalysisJobEntity.JobStatus.SUCCEEDED, job.getStatus());
        assertEquals(AnalysisJobService.INGEST_INGESTED, job.getIngestState());
        assertEquals(AnalysisJobService.ALIGNMENT_PENDING, job.getAlignmentState(),
                "对齐失败必须留下待补偿阶段，否则恢复器发现不了");
        assertNotNull(job.getNextRecoveryAt());
        assertTrue(job.getErrorMessage().contains("boom"), job.getErrorMessage());
        verify(jobRepository).save(job);
    }

    @Test
    void completeAudioJobCallsTranscriptIngest() throws Exception {
        AnalysisJobEntity job = new AnalysisJobEntity();
        job.setId("job-a");
        job.setType(AnalysisJobEntity.JobType.AUDIO_ANALYSIS);
        job.setStatus(AnalysisJobEntity.JobStatus.RUNNING);
        job.setParams("{\"lessonSessionId\":\"lesson-1\"}");
        job.setResultMetadata("{\"status\":\"succeeded\",\"text\":\"转写文本\"}");
        when(jobRepository.findByIdForUpdate("job-a")).thenReturn(java.util.Optional.of(job));
        when(jobRepository.save(job)).thenReturn(job);

        service.completeJob("job-a", Map.of("status", "succeeded"));

        verify(transcriptService).ingestCompletedAudioJob(job);
    }
}
