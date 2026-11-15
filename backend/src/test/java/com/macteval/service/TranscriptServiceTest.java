package com.macteval.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.macteval.model.entity.AnalysisJobEntity;
import com.macteval.model.entity.LessonSessionEntity;
import com.macteval.model.entity.LessonTranscriptEntity;
import com.macteval.repository.LessonSessionRepository;
import com.macteval.repository.LessonTranscriptRepository;
import com.macteval.security.ResourceGuard;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.security.access.AccessDeniedException;

import java.util.Map;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class TranscriptServiceTest {

    private LessonTranscriptRepository transcriptRepository;
    private LessonSessionRepository lessonSessionRepository;
    private TranscriptService service;

    @BeforeEach
    void setUp() {
        transcriptRepository = mock(LessonTranscriptRepository.class);
        lessonSessionRepository = mock(LessonSessionRepository.class);
        service = new TranscriptService(transcriptRepository, lessonSessionRepository, new ObjectMapper(), new ResourceGuard());
    }

    @Test
    void ingestSucceededAudioJobPersistsTranscript() throws Exception {
        AnalysisJobEntity job = new AnalysisJobEntity();
        job.setId("job-a");
        job.setType(AnalysisJobEntity.JobType.AUDIO_ANALYSIS);
        job.setStatus(AnalysisJobEntity.JobStatus.SUCCEEDED);
        job.setParams("{\"lessonSessionId\":\"lesson-1\"}");
        job.setResultMetadata("{\"status\":\"succeeded\",\"text\":\"大家好，今天讲课堂评价\",\"language\":\"zh\","
                + "\"language_probability\":0.99,\"segments\":[{\"start\":0.0,\"end\":3.0,\"text\":\"大家好\"}],"
                + "\"model_version\":\"faster-whisper-small\"}");

        service.ingestCompletedAudioJob(job);

        org.mockito.ArgumentCaptor<LessonTranscriptEntity> captor =
                org.mockito.ArgumentCaptor.forClass(LessonTranscriptEntity.class);
        verify(transcriptRepository).save(captor.capture());
        assertEquals("lesson-1", captor.getValue().getLessonSessionId());
        assertEquals("大家好，今天讲课堂评价", captor.getValue().getText());
        assertEquals("zh", captor.getValue().getLanguage());
        assertEquals(0.99, captor.getValue().getLanguageProbability());
        assertEquals("job-a", captor.getValue().getJobId());
        assertEquals("[{\"start\":0.0,\"end\":3.0,\"text\":\"大家好\"}]", captor.getValue().getSegments());
        assertEquals("faster-whisper-small", captor.getValue().getModelVersion());
    }

    @Test
    void ingestSkipsFailedJob() {
        AnalysisJobEntity job = new AnalysisJobEntity();
        job.setId("job-f");
        job.setType(AnalysisJobEntity.JobType.AUDIO_ANALYSIS);
        job.setStatus(AnalysisJobEntity.JobStatus.FAILED);

        service.ingestCompletedAudioJob(job);

        verify(transcriptRepository, never()).save(any());
    }

    @Test
    void ingestSkipsNonAudioJob() {
        AnalysisJobEntity job = new AnalysisJobEntity();
        job.setId("job-v");
        job.setType(AnalysisJobEntity.JobType.VIDEO_ANALYSIS);
        job.setStatus(AnalysisJobEntity.JobStatus.SUCCEEDED);

        service.ingestCompletedAudioJob(job);

        verify(transcriptRepository, never()).save(any());
    }

    @Test
    void ingestSkipsWhenTextMissing() {
        AnalysisJobEntity job = new AnalysisJobEntity();
        job.setId("job-e");
        job.setType(AnalysisJobEntity.JobType.AUDIO_ANALYSIS);
        job.setStatus(AnalysisJobEntity.JobStatus.SUCCEEDED);
        job.setParams("{\"lessonSessionId\":\"lesson-1\"}");
        job.setResultMetadata("{\"status\":\"succeeded\"}");

        service.ingestCompletedAudioJob(job);

        verify(transcriptRepository, never()).save(any());
    }

    @Test
    void getByLessonThrowsWhenNoTranscript() {
        when(lessonSessionRepository.findById("lesson-1"))
                .thenReturn(Optional.of(session("lesson-1", "teacher-1")));
        when(transcriptRepository.findFirstByLessonSessionIdOrderByCreatedAtDescIdDesc("lesson-1"))
                .thenReturn(Optional.empty());

        org.junit.jupiter.api.Assertions.assertThrows(java.util.NoSuchElementException.class,
                () -> service.getByLesson("lesson-1", "teacher-1"));
    }

    @Test
    void getByLessonReturnsLatestTranscript() throws Exception {
        LessonTranscriptEntity e = new LessonTranscriptEntity();
        e.setId("t-1");
        e.setLessonSessionId("lesson-1");
        e.setText("转写文本");
        e.setLanguage("zh");
        e.setLanguageProbability(0.99);
        e.setSegments("[{\"start\":0.0,\"end\":3.0,\"text\":\"大家好\"}]");
        when(lessonSessionRepository.findById("lesson-1"))
                .thenReturn(Optional.of(session("lesson-1", "teacher-1")));
        when(transcriptRepository.findFirstByLessonSessionIdOrderByCreatedAtDescIdDesc("lesson-1"))
                .thenReturn(Optional.of(e));

        var dto = service.getByLesson("lesson-1", "teacher-1");

        assertEquals("转写文本", dto.text());
        assertEquals(1, dto.segments().size());
    }

    @Test
    void getByLessonRejectsNonOwner() {
        when(lessonSessionRepository.findById("lesson-1"))
                .thenReturn(Optional.of(session("lesson-1", "teacher-1")));

        assertThrows(AccessDeniedException.class,
                () -> service.getByLesson("lesson-1", "teacher-2"));
    }

    @Test
    void ingestPersistsSilentTranscript() {
        AnalysisJobEntity job = new AnalysisJobEntity();
        job.setId("job-silent");
        job.setType(AnalysisJobEntity.JobType.AUDIO_ANALYSIS);
        job.setStatus(AnalysisJobEntity.JobStatus.SUCCEEDED);
        job.setParams("{\"lessonSessionId\":\"lesson-1\"}");
        job.setResultMetadata("{\"status\":\"succeeded\",\"text\":\"\",\"segments\":[],"
                + "\"language\":\"zh\",\"model_version\":\"faster-whisper-small\"}");

        service.ingestCompletedAudioJob(job);

        org.mockito.ArgumentCaptor<LessonTranscriptEntity> captor =
                org.mockito.ArgumentCaptor.forClass(LessonTranscriptEntity.class);
        verify(transcriptRepository).save(captor.capture());
        assertEquals("", captor.getValue().getText());
        assertEquals("[]", captor.getValue().getSegments());
    }

    private LessonSessionEntity session(String id, String teacherId) {
        LessonSessionEntity s = new LessonSessionEntity();
        s.setId(id);
        s.setTeacherId(teacherId);
        return s;
    }
}
