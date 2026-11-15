package com.macteval.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.macteval.model.entity.AnalysisJobEntity;
import com.macteval.model.entity.CourseEntity;
import com.macteval.model.entity.LessonSessionEntity;
import com.macteval.model.entity.StudentEntity;
import com.macteval.model.entity.SuggestionEntity;
import com.macteval.repository.AnalysisJobRepository;
import com.macteval.repository.ClassGroupRepository;
import com.macteval.repository.CourseRepository;
import com.macteval.repository.LessonSessionRepository;
import com.macteval.repository.StudentRepository;
import com.macteval.repository.SuggestionRepository;
import com.macteval.controller.LessonSessionController;
import com.macteval.security.ResourceGuard;
import org.junit.jupiter.api.Test;
import org.springframework.data.domain.Pageable;
import org.springframework.security.access.AccessDeniedException;

import java.util.List;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * BE01 对象级授权回归：列表、详情与变更入口都不能只靠“已登录”。
 */
class AccessControlTest {

    @Test
    void lessonListRejectsForeignCourseId() {
        LessonSessionRepository lessonRepo = mock(LessonSessionRepository.class);
        CourseRepository courseRepo = mock(CourseRepository.class);
        LessonSessionService service = new LessonSessionService(
                lessonRepo, courseRepo, mock(ClassGroupRepository.class),
                new ResourceGuard(), mock(StudentRepository.class),
                mock(com.macteval.repository.AttendanceRecordRepository.class),
                mock(com.macteval.repository.BehaviorObservationRepository.class),
                mock(com.macteval.repository.LessonTranscriptRepository.class),
                mock(com.macteval.repository.LessonWindowFeatureRepository.class),
                mock(com.macteval.repository.EvaluationReportRepository.class),
                mock(com.macteval.repository.SuggestionRepository.class));

        CourseEntity course = new CourseEntity();
        course.setId("course-B");
        course.setTeacherId("teacher-B");
        when(courseRepo.findById("course-B")).thenReturn(Optional.of(course));

        assertThrows(AccessDeniedException.class,
                () -> service.listSessions("teacher-A", "course-B", false));
        verify(lessonRepo, never()).findByCourseId("course-B");
    }

    @Test
    void adminSessionListWithoutTeacherIdUsesGlobalBranch() {
        LessonSessionRepository lessonRepo = mock(LessonSessionRepository.class);
        LessonSessionService service = mock(LessonSessionService.class);
        LessonSessionController controller = new LessonSessionController(service);
        var admin = new org.springframework.security.authentication.UsernamePasswordAuthenticationToken(
                "admin-1", null, List.of(new org.springframework.security.core.authority.SimpleGrantedAuthority("ROLE_admin")));

        controller.listSessions(null, null, admin);

        verify(service).listSessions(null, null, true);
    }

    @Test
    void lessonDetailRejectsStudentFromAnotherClass() {
        LessonSessionRepository lessonRepo = mock(LessonSessionRepository.class);
        StudentRepository studentRepo = mock(StudentRepository.class);
        LessonSessionService service = new LessonSessionService(
                lessonRepo, mock(CourseRepository.class), mock(ClassGroupRepository.class),
                new ResourceGuard(), studentRepo,
                mock(com.macteval.repository.AttendanceRecordRepository.class),
                mock(com.macteval.repository.BehaviorObservationRepository.class),
                mock(com.macteval.repository.LessonTranscriptRepository.class),
                mock(com.macteval.repository.LessonWindowFeatureRepository.class),
                mock(com.macteval.repository.EvaluationReportRepository.class),
                mock(com.macteval.repository.SuggestionRepository.class));

        LessonSessionEntity session = new LessonSessionEntity();
        session.setId("lesson-1");
        session.setTeacherId("teacher-A");
        session.setClassId("class-1");
        when(lessonRepo.findById("lesson-1")).thenReturn(Optional.of(session));

        StudentEntity student = new StudentEntity();
        student.setId("student-B");
        student.setClassId("class-2");
        when(studentRepo.findById("student-B")).thenReturn(Optional.of(student));

        assertThrows(AccessDeniedException.class,
                () -> service.getSessionForUser("lesson-1", "student-B", "student"));
    }

    @Test
    void lessonCreateRejectsForeignCourse() {
        LessonSessionRepository lessonRepo = mock(LessonSessionRepository.class);
        CourseRepository courseRepo = mock(CourseRepository.class);
        LessonSessionService service = new LessonSessionService(
                lessonRepo, courseRepo, mock(ClassGroupRepository.class),
                new ResourceGuard(), mock(StudentRepository.class),
                mock(com.macteval.repository.AttendanceRecordRepository.class),
                mock(com.macteval.repository.BehaviorObservationRepository.class),
                mock(com.macteval.repository.LessonTranscriptRepository.class),
                mock(com.macteval.repository.LessonWindowFeatureRepository.class),
                mock(com.macteval.repository.EvaluationReportRepository.class),
                mock(com.macteval.repository.SuggestionRepository.class));

        CourseEntity course = new CourseEntity();
        course.setId("course-B");
        course.setTeacherId("teacher-B");
        when(courseRepo.findById("course-B")).thenReturn(Optional.of(course));

        var request = new com.macteval.dto.LessonSessionCreateRequest(
                "course-B", "class-1", "标题", null, null, null, null);
        assertThrows(AccessDeniedException.class,
                () -> service.createSession(request, "teacher-A", false));
        verify(lessonRepo, never()).save(any());
    }

    @Test
    void jobDetailRejectsForeignOwner() {
        AnalysisJobRepository jobRepo = mock(AnalysisJobRepository.class);
        AnalysisJobService service = new AnalysisJobService(
                jobRepo, mock(com.macteval.repository.TaskOutboxRepository.class),
                mock(org.springframework.data.redis.core.RedisTemplate.class),
                new ObjectMapper(), mock(BehaviorService.class), mock(TranscriptService.class),
                mock(WindowAlignmentService.class));

        AnalysisJobEntity job = new AnalysisJobEntity();
        job.setId("job-1");
        job.setOwnerId("teacher-A");
        when(jobRepo.findById("job-1")).thenReturn(Optional.of(job));

        assertThrows(AccessDeniedException.class,
                () -> service.getJobStatus("job-1", "teacher-B", false));
    }

    @Test
    void jobListScopesToOwnerForTeachers() {
        AnalysisJobRepository jobRepo = mock(AnalysisJobRepository.class);
        AnalysisJobService service = new AnalysisJobService(
                jobRepo, mock(com.macteval.repository.TaskOutboxRepository.class),
                mock(org.springframework.data.redis.core.RedisTemplate.class),
                new ObjectMapper(), mock(BehaviorService.class), mock(TranscriptService.class),
                mock(WindowAlignmentService.class));
        when(jobRepo.findByOwnerIdOrderByCreatedAtDesc(eq("teacher-A"), any(Pageable.class)))
                .thenReturn(List.of());

        service.listJobs("teacher-A", false, 50, null);

        verify(jobRepo).findByOwnerIdOrderByCreatedAtDesc(eq("teacher-A"), any(Pageable.class));
        verify(jobRepo, never()).findAllByOrderByCreatedAtDesc(any(Pageable.class));
    }

    @Test
    void jobListIsGlobalForAdmin() {
        AnalysisJobRepository jobRepo = mock(AnalysisJobRepository.class);
        AnalysisJobService service = new AnalysisJobService(
                jobRepo, mock(com.macteval.repository.TaskOutboxRepository.class),
                mock(org.springframework.data.redis.core.RedisTemplate.class),
                new ObjectMapper(), mock(BehaviorService.class), mock(TranscriptService.class),
                mock(WindowAlignmentService.class));
        when(jobRepo.findAllByOrderByCreatedAtDesc(any(Pageable.class))).thenReturn(List.of());

        assertEquals(0, service.listJobs("admin-1", true, 50, null).size());
        verify(jobRepo).findAllByOrderByCreatedAtDesc(any(Pageable.class));
    }

    @Test
    void suggestionChangeRejectsForeignLesson() {
        SuggestionRepository suggestionRepo = mock(SuggestionRepository.class);
        LessonSessionRepository lessonRepo = mock(LessonSessionRepository.class);
        SuggestionService service = new SuggestionService(suggestionRepo, lessonRepo);

        SuggestionEntity suggestion = new SuggestionEntity();
        suggestion.setId("s-1");
        suggestion.setLessonSessionId("lesson-B");
        when(suggestionRepo.findById("s-1")).thenReturn(Optional.of(suggestion));

        LessonSessionEntity session = new LessonSessionEntity();
        session.setId("lesson-B");
        session.setTeacherId("teacher-B");
        when(lessonRepo.findById("lesson-B")).thenReturn(Optional.of(session));

        assertThrows(AccessDeniedException.class,
                () -> service.changeStatus("s-1", SuggestionService.Action.ADOPT, "teacher-A", false));
        verify(suggestionRepo, never()).save(any());
    }
}
