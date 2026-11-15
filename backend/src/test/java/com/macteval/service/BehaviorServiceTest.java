package com.macteval.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.macteval.model.entity.AnalysisJobEntity;
import com.macteval.model.entity.BehaviorObservationEntity;
import com.macteval.model.entity.LessonSessionEntity;
import com.macteval.repository.BehaviorObservationRepository;
import com.macteval.repository.LessonSessionRepository;
import com.macteval.security.ResourceGuard;
import com.macteval.websocket.ClassroomWebSocketHandler;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.security.access.AccessDeniedException;

import java.util.List;
import java.util.Map;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class BehaviorServiceTest {

    private BehaviorObservationRepository behaviorRepository;
    private LessonSessionRepository lessonSessionRepository;
    private ClassroomWebSocketHandler webSocketHandler;
    private BehaviorService service;

    @BeforeEach
    void setUp() {
        behaviorRepository = mock(BehaviorObservationRepository.class);
        lessonSessionRepository = mock(LessonSessionRepository.class);
        webSocketHandler = mock(ClassroomWebSocketHandler.class);
        service = new BehaviorService(
                behaviorRepository, mock(com.macteval.repository.BehaviorEvidenceFrameRepository.class),
                lessonSessionRepository, new ObjectMapper(),
                new ResourceGuard(), webSocketHandler);
    }

    @Test
    void ingestSucceededVideoJobPersistsObservationsAndPushes() throws Exception {
        AnalysisJobEntity job = new AnalysisJobEntity();
        job.setId("job-v");
        job.setType(AnalysisJobEntity.JobType.VIDEO_ANALYSIS);
        job.setStatus(AnalysisJobEntity.JobStatus.SUCCEEDED);
        job.setParams("{\"lessonSessionId\":\"lesson-1\"}");
        job.setResultMetadata("{\"status\":\"succeeded\",\"observations\":["
                + "{\"window_start_ms\":0,\"window_end_ms\":60000,\"visible_seat_count\":40,"
                + "\"total_configured_seats\":45,\"coverage\":0.89,\"data_quality\":\"usable\","
                + "\"in_seat_count\":38,\"leave_seat_count\":2,\"hand_raised_count\":3,"
                + "\"standing_count\":2,\"upright_posture_count\":30,\"upright_proportion\":0.75}]}");

        // save 捕获落库实体用于断言；推送使用本批最后一个窗口（不再重新查询）
        java.util.List<BehaviorObservationEntity> saved = new java.util.ArrayList<>();
        org.mockito.Mockito.doAnswer(inv -> { saved.add(inv.getArgument(0)); return inv.getArgument(0); })
                .when(behaviorRepository).save(any(BehaviorObservationEntity.class));

        service.ingestCompletedVideoJob(job);

        assertEquals(1, saved.size());
        assertEquals("lesson-1", saved.get(0).getLessonSessionId());
        assertEquals(45, saved.get(0).getTotalConfiguredSeats());
        assertEquals("usable", saved.get(0).getDataQuality());
        assertEquals("0.75", saved.get(0).getAggregatedUprightProportion());
        org.mockito.ArgumentCaptor<Map> payloadCaptor = org.mockito.ArgumentCaptor.forClass(Map.class);
        verify(webSocketHandler).pushBehaviorUpdate(eq("lesson-1"), payloadCaptor.capture());
        assertEquals(0.75, ((Number) payloadCaptor.getValue().get("upright_proportion")).doubleValue());
        assertEquals(40, payloadCaptor.getValue().get("visible_seat_count"));
        assertEquals(45, payloadCaptor.getValue().get("total_configured_seats"));
        assertEquals(30, payloadCaptor.getValue().get("upright_posture_count"));
    }

    @Test
    void replayedObservationUpdatesExistingRowInsteadOfCreatingDuplicate() throws Exception {
        AnalysisJobEntity job = new AnalysisJobEntity();
        job.setId("job-replay");
        job.setType(AnalysisJobEntity.JobType.VIDEO_ANALYSIS);
        job.setStatus(AnalysisJobEntity.JobStatus.SUCCEEDED);
        job.setParams("{\"lessonSessionId\":\"lesson-1\",\"windowDuration\":60}");
        job.setResultMetadata("{\"status\":\"succeeded\",\"observations\":["
                + "{\"window_index\":0,\"window_start_ms\":0,\"window_end_ms\":60000,"
                + "\"upright_proportion\":0.82,\"data_quality\":\"usable\"}]}");

        BehaviorObservationEntity existing = new BehaviorObservationEntity();
        existing.setId("existing-row");
        existing.setJobId("job-replay");
        existing.setWindowIndex(0);
        when(behaviorRepository.findByJobIdAndWindowIndex("job-replay", 0))
                .thenReturn(Optional.of(existing));

        service.ingestCompletedVideoJob(job);

        verify(behaviorRepository).save(existing);
        assertEquals("existing-row", existing.getId(), "重放必须复用原行 ID");
        assertEquals(0, existing.getWindowIndex());
        assertEquals("0.82", existing.getAggregatedUprightProportion());
        assertEquals(0L, existing.getWindowStartOffsetMs());
        assertEquals(60000L, existing.getWindowEndOffsetMs());
        assertNotNull(existing.getMediaAnchor());
    }

    @Test
    void unsupportedPostureStaysNullInStorageDtoAndWebSocket() {
        AnalysisJobEntity job = new AnalysisJobEntity();
        job.setId("locator-only");
        job.setType(AnalysisJobEntity.JobType.VIDEO_ANALYSIS);
        job.setStatus(AnalysisJobEntity.JobStatus.SUCCEEDED);
        job.setParams("{\"lessonSessionId\":\"lesson-1\"}");
        job.setResultMetadata("{\"status\":\"succeeded\",\"observations\":[{"
                + "\"window_start_ms\":0,\"window_end_ms\":60000,\"visible_seat_count\":30,"
                + "\"coverage\":0.67,\"data_quality\":\"unavailable\",\"upright_proportion\":null}]}");
        org.mockito.ArgumentCaptor<BehaviorObservationEntity> saved =
                org.mockito.ArgumentCaptor.forClass(BehaviorObservationEntity.class);

        service.ingestCompletedVideoJob(job);

        verify(behaviorRepository).save(saved.capture());
        org.junit.jupiter.api.Assertions.assertNull(saved.getValue().getAggregatedUprightProportion());
        org.junit.jupiter.api.Assertions.assertNull(saved.getValue().getStandingCount());
        org.mockito.ArgumentCaptor<Map> payload = org.mockito.ArgumentCaptor.forClass(Map.class);
        verify(webSocketHandler).pushBehaviorUpdate(eq("lesson-1"), payload.capture());
        org.junit.jupiter.api.Assertions.assertTrue(payload.getValue().containsKey("upright_proportion"));
        org.junit.jupiter.api.Assertions.assertNull(payload.getValue().get("upright_proportion"));
        when(lessonSessionRepository.findById("lesson-1")).thenReturn(Optional.of(session("lesson-1", "teacher-1")));
        when(behaviorRepository.findByLessonSessionIdOrderByWindowStartAsc("lesson-1"))
                .thenReturn(List.of(saved.getValue()));
        org.junit.jupiter.api.Assertions.assertNull(service.getByLesson("lesson-1", "teacher-1").getFirst().uprightProportion());
    }

    @Test
    void ingestPersistsEvidenceFramesAndClearsStaleOnReplay() throws Exception {
        AnalysisJobEntity job = new AnalysisJobEntity();
        job.setId("job-frames");
        job.setType(AnalysisJobEntity.JobType.VIDEO_ANALYSIS);
        job.setStatus(AnalysisJobEntity.JobStatus.SUCCEEDED);
        job.setParams("{\"lessonSessionId\":\"lesson-1\"}");
        job.setResultMetadata("{\"status\":\"succeeded\",\"observations\":[],"
                + "\"evidence_frames\":[{\"window_index\":0,\"timestamp_ms\":12000,"
                + "\"bucket\":\"analysis-artifacts\",\"object_name\":\"lessons/l/jobs/j/window-0-overview.jpg\","
                + "\"code\":\"overview\",\"label\":\"课堂全景\","
                + "\"metadata\":{\"detectionSource\":\"behavior_model\",\"modelDigest\":\"sha256-test\","
                + "\"detections\":[{\"subjectId\":\"target-1\",\"code\":\"hand_raising\","
                + "\"confidence\":0.87,\"bbox\":[0.1,0.2,0.3,0.4]}]}}]}");

        com.macteval.repository.BehaviorEvidenceFrameRepository frameRepo =
                mock(com.macteval.repository.BehaviorEvidenceFrameRepository.class);
        BehaviorService frameService = new BehaviorService(
                behaviorRepository, frameRepo, lessonSessionRepository,
                new ObjectMapper(), new ResourceGuard(), webSocketHandler);

        frameService.ingestCompletedVideoJob(job);

        verify(frameRepo).deleteByJobId("job-frames");
        org.mockito.ArgumentCaptor<com.macteval.model.entity.BehaviorEvidenceFrameEntity> captor =
                org.mockito.ArgumentCaptor.forClass(com.macteval.model.entity.BehaviorEvidenceFrameEntity.class);
        verify(frameRepo).save(captor.capture());
        assertEquals("lesson-1", captor.getValue().getLessonSessionId());
        assertEquals(0, captor.getValue().getWindowIndex());
        assertEquals("overview", captor.getValue().getCode());
        assertEquals("课堂全景", captor.getValue().getLabel());
        var metadata = new ObjectMapper().readTree(captor.getValue().getMetadata());
        assertEquals("sha256-test", metadata.path("modelDigest").asText());
        assertEquals("target-1", metadata.path("detections").get(0).path("subjectId").asText());
        assertEquals(0.87, metadata.path("detections").get(0).path("confidence").asDouble());
        assertEquals(4, metadata.path("detections").get(0).path("bbox").size());
    }

    @Test
    void ingestSkipsFailedJob() {
        AnalysisJobEntity job = new AnalysisJobEntity();
        job.setId("job-f");
        job.setType(AnalysisJobEntity.JobType.VIDEO_ANALYSIS);
        job.setStatus(AnalysisJobEntity.JobStatus.FAILED);

        service.ingestCompletedVideoJob(job);

        verify(behaviorRepository, never()).save(any());
    }

    @Test
    void ingestSkipsObservationWithMissingWindowTimes() throws Exception {
        AnalysisJobEntity job = new AnalysisJobEntity();
        job.setId("job-m");
        job.setType(AnalysisJobEntity.JobType.VIDEO_ANALYSIS);
        job.setStatus(AnalysisJobEntity.JobStatus.SUCCEEDED);
        job.setCreatedAt(java.time.LocalDateTime.of(2026, 8, 1, 8, 0));
        job.setParams("{\"lessonSessionId\":\"lesson-1\"}");
        job.setResultMetadata("{\"status\":\"succeeded\",\"observations\":["
                + "{\"visible_seat_count\":40,\"total_configured_seats\":45,\"coverage\":0.89,"
                + "\"data_quality\":\"usable\",\"in_seat_count\":38,\"upright_posture_count\":30,"
                + "\"upright_proportion\":0.75}]}");

        service.ingestCompletedVideoJob(job);

        verify(behaviorRepository, org.mockito.Mockito.never()).save(any());
        verify(webSocketHandler, org.mockito.Mockito.never())
                .pushBehaviorUpdate(anyString(), any(Map.class));
    }

    @Test
    void getByLessonReturnsObservations() throws Exception {
        BehaviorObservationEntity e = new BehaviorObservationEntity();
        e.setId("b-1");
        e.setLessonSessionId("lesson-1");
        e.setWindowStart(java.time.LocalDateTime.of(2026, 8, 1, 8, 0));
        e.setWindowEnd(java.time.LocalDateTime.of(2026, 8, 1, 8, 1));
        e.setTotalConfiguredSeats(45);
        e.setCoverage(0.9);
        e.setDataQuality("usable");
        e.setAggregatedUprightProportion("0.75");
        when(lessonSessionRepository.findById("lesson-1"))
                .thenReturn(Optional.of(session("lesson-1", "teacher-1")));
        when(behaviorRepository.findByLessonSessionIdOrderByWindowStartAsc("lesson-1"))
                .thenReturn(List.of(e));

        var dtos = service.getByLesson("lesson-1", "teacher-1");

        assertEquals(1, dtos.size());
        assertEquals(0.75, dtos.getFirst().uprightProportion());
    }

    @Test
    void getByLessonRejectsNonOwner() {
        when(lessonSessionRepository.findById("lesson-1"))
                .thenReturn(Optional.of(session("lesson-1", "teacher-1")));

        assertThrows(AccessDeniedException.class,
                () -> service.getByLesson("lesson-1", "teacher-2"));
    }

    private LessonSessionEntity session(String id, String teacherId) {
        LessonSessionEntity s = new LessonSessionEntity();
        s.setId(id);
        s.setTeacherId(teacherId);
        return s;
    }
}
