package com.macteval.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.macteval.dto.ReportDetailDTO;
import com.macteval.model.entity.BehaviorEvidenceFrameEntity;
import com.macteval.model.entity.EvaluationReportEntity;
import com.macteval.model.entity.LessonSessionEntity;
import com.macteval.repository.*;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.springframework.security.access.AccessDeniedException;

import java.util.List;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

class ReportEvidenceServiceTest {
    private final ReportService reports = mock(ReportService.class);
    private final BehaviorEvidenceFrameRepository frames = mock(BehaviorEvidenceFrameRepository.class);
    private final MinioService minio = mock(MinioService.class);
    private final ReportEvidenceService service = new ReportEvidenceService(reports, frames, minio, new ObjectMapper());

    @Test
    void historicalReportUsesItsVideoJobAndFreshSignedUrls() {
        ReportDetailDTO current = mock(ReportDetailDTO.class), previous = mock(ReportDetailDTO.class);
        when(current.version()).thenReturn(3);
        when(previous.analysisVideoJobId()).thenReturn("old-job");
        when(reports.getReport("lesson", "teacher")).thenReturn(current);
        when(reports.getVersionDetail("lesson", 2, "teacher")).thenReturn(previous);
        var frame = frame();
        when(frames.findByLessonSessionIdAndJobIdOrderByWindowIndexAsc("lesson", "old-job")).thenReturn(List.of(frame));
        when(minio.generateDownloadUrl("analysis-artifacts", "frame.jpg", 30)).thenReturn("url-first", "url-refreshed");

        var first = service.getFrames("lesson", 2, "teacher");
        var second = service.getFrames("lesson", 2, "teacher");

        assertEquals("url-first", first.getFirst().url());
        assertEquals("url-refreshed", second.getFirst().url());
        assertEquals("behavior_model", first.getFirst().metadata().get("detectionSource"));
        assertEquals("举手", first.getFirst().label());
        verify(frames, never()).findByLessonSessionIdOrderByWindowIndexAsc(anyString());
    }

    @Test
    void unauthorizedUserCannotReadOrSignAnyFrames() {
        when(reports.getReport("lesson", "other-teacher")).thenThrow(new AccessDeniedException("denied"));
        assertThrows(AccessDeniedException.class, () -> service.getFrames("lesson", null, "other-teacher"));
        verifyNoInteractions(frames, minio);
    }

    @Test
    void reportWithoutVideoDoesNotFallBackToLatestOrOtherJobs() {
        when(reports.getReport("lesson", "teacher")).thenReturn(mock(ReportDetailDTO.class));
        assertTrue(service.getFrames("lesson", null, "teacher").isEmpty());
        verifyNoInteractions(frames, minio);
    }

    @Test
    void framesAreOrderedByWindowAndMissingTimestampsGoLast() {
        var report = mock(ReportDetailDTO.class);
        when(report.analysisVideoJobId()).thenReturn("old-job");
        when(reports.getReport("lesson", "teacher")).thenReturn(report);
        // windowIndex 3 的帧没有时间戳：必须排在末尾，不能当作 0ms 冒充课堂开头。
        var unknownTime = frame("frame-unknown", 3, null);
        var tenSeconds = frame("frame-10s", 1, 10_000L);
        var twentySeconds = frame("frame-20s", 2, 20_000L);
        when(frames.findByLessonSessionIdAndJobIdOrderByWindowIndexAsc("lesson", "old-job"))
                .thenReturn(List.of(unknownTime, twentySeconds, tenSeconds));
        when(minio.generateDownloadUrl(anyString(), anyString(), anyLong())).thenReturn("url");

        var result = service.getFrames("lesson", null, "teacher");

        assertEquals(List.of(1, 2, 3), result.stream().map(frame -> frame.windowIndex()).toList());
        assertEquals("frame-unknown", result.getLast().id());
        assertNull(result.getLast().timestampMs());
    }

    @Test
    void signingFailureAndLegacyMetadataDoNotBreakReport() {
        var report = mock(ReportDetailDTO.class);
        when(report.analysisVideoJobId()).thenReturn("old-job");
        when(reports.getReport("lesson", "teacher")).thenReturn(report);
        var frame = frame();
        frame.setMetadata("invalid-json");
        when(frames.findByLessonSessionIdAndJobIdOrderByWindowIndexAsc("lesson", "old-job")).thenReturn(List.of(frame));
        when(minio.generateDownloadUrl(anyString(), anyString(), anyLong())).thenThrow(new IllegalStateException("offline"));
        var result = service.getFrames("lesson", null, "teacher");
        assertNull(result.getFirst().url());
        assertTrue(result.getFirst().metadata().isEmpty());
    }

    private BehaviorEvidenceFrameEntity frame() {
        return frame("frame-1", 0, 12000L);
    }

    private BehaviorEvidenceFrameEntity frame(String id, Integer windowIndex, Long timestampMs) {
        var frame = new BehaviorEvidenceFrameEntity();
        frame.setId(id); frame.setLessonSessionId("lesson"); frame.setJobId("old-job");
        frame.setWindowIndex(windowIndex); frame.setTimestampMs(timestampMs); frame.setCode("hand_raising");
        frame.setLabel("举手"); frame.setBucket("analysis-artifacts"); frame.setObjectKey("frame.jpg");
        frame.setMetadata("{\"detectionSource\":\"behavior_model\",\"detections\":[]}");
        return frame;
    }

    /**
     * 属主校验的空值语义：传 null 不再等于「跳过校验」，无属主课堂也不再对普通教师放行。
     */
    @Nested
    class OwnerCheckSemantics {

        private final EvaluationReportRepository reportRepository = mock(EvaluationReportRepository.class);
        private final LessonSessionRepository sessionRepository = mock(LessonSessionRepository.class);
        private final ReportService reportService = new ReportService(
                reportRepository, mock(EvaluationReportVersionRepository.class), sessionRepository,
                mock(CourseRepository.class), mock(ClassGroupRepository.class),
                mock(BehaviorObservationRepository.class),
                mock(SuggestionRepository.class), mock(AnalysisJobRepository.class),
                new SuggestionEngine(new ObjectMapper()), mock(StudentRepository.class),
                mock(LessonWindowFeatureRepository.class), new ObjectMapper());

        @Test
        void teacherIdIsRequiredInsteadOfSilentlyBypassingTheCheck() {
            when(reportRepository.findByLessonSessionId("lesson")).thenReturn(Optional.of(report("teacher-1")));
            when(sessionRepository.findById("lesson")).thenReturn(Optional.of(session("teacher-1")));

            assertThrows(IllegalArgumentException.class,
                    () -> reportService.getReport("lesson", null));
            assertThrows(IllegalArgumentException.class,
                    () -> reportService.getReport("lesson", "  "));
        }

        @Test
        void sessionWithoutOwnerIsNotReadableByOrdinaryTeacher() {
            when(reportRepository.findByLessonSessionId("lesson")).thenReturn(Optional.of(report(null)));
            when(sessionRepository.findById("lesson")).thenReturn(Optional.of(session(null)));

            assertThrows(AccessDeniedException.class,
                    () -> reportService.getReport("lesson", "teacher-1"));
        }

        @Test
        void owningTeacherStillReadsOwnReport() {
            when(reportRepository.findByLessonSessionId("lesson")).thenReturn(Optional.of(report("teacher-1")));
            when(sessionRepository.findById("lesson")).thenReturn(Optional.of(session("teacher-1")));

            assertEquals("lesson", reportService.getReport("lesson", "teacher-1").lessonSessionId());
        }

        @Test
        void internalCallerMustOptOutExplicitlyRatherThanPassNull() {
            when(sessionRepository.findById("lesson")).thenReturn(Optional.of(session("teacher-1")));

            assertThrows(IllegalArgumentException.class,
                    () -> reportService.getVersionHistory("lesson", null));
        }

        private LessonSessionEntity session(String teacherId) {
            LessonSessionEntity session = new LessonSessionEntity();
            session.setId("lesson");
            session.setCourseId("course-1");
            session.setClassId("class-1");
            session.setTeacherId(teacherId);
            session.setTitle("测试课堂");
            return session;
        }

        private EvaluationReportEntity report(String teacherId) {
            EvaluationReportEntity report = new EvaluationReportEntity();
            report.setId("report-1");
            report.setLessonSessionId("lesson");
            report.setTeacherId(teacherId);
            report.setStatus("draft");
            report.setDataSource("EMPTY");
            return report;
        }
    }
}
