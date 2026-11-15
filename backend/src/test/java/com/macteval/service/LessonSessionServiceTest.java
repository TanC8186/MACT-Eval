package com.macteval.service;

import com.macteval.dto.UpdateSessionRequest;
import com.macteval.model.entity.LessonSessionEntity;
import com.macteval.model.entity.StudentEntity;
import com.macteval.repository.ClassGroupRepository;
import com.macteval.repository.CourseRepository;
import com.macteval.repository.LessonSessionRepository;
import com.macteval.repository.StudentRepository;
import com.macteval.security.ResourceGuard;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.security.access.AccessDeniedException;

import java.time.LocalDateTime;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

class LessonSessionServiceTest {

    private LessonSessionRepository lessonSessionRepository;
    private CourseRepository courseRepository;
    private ClassGroupRepository classGroupRepository;
    private StudentRepository studentRepository;
    private LessonSessionService service;

    @BeforeEach
    void setUp() {
        lessonSessionRepository = mock(LessonSessionRepository.class);
        courseRepository = mock(CourseRepository.class);
        classGroupRepository = mock(ClassGroupRepository.class);
        studentRepository = mock(StudentRepository.class);
        service = new LessonSessionService(
                lessonSessionRepository, courseRepository, classGroupRepository,
                new ResourceGuard(), studentRepository,
                mock(com.macteval.repository.AttendanceRecordRepository.class),
                mock(com.macteval.repository.BehaviorObservationRepository.class),
                mock(com.macteval.repository.LessonTranscriptRepository.class),
                mock(com.macteval.repository.LessonWindowFeatureRepository.class),
                mock(com.macteval.repository.EvaluationReportRepository.class),
                mock(com.macteval.repository.SuggestionRepository.class));
    }

    @Test
    void updateSessionModifiesMutableFields() {
        LessonSessionEntity entity = session("lesson-1", "teacher-1");
        when(lessonSessionRepository.findById("lesson-1")).thenReturn(Optional.of(entity));

        var request = new UpdateSessionRequest(
                "新标题",
                LocalDateTime.of(2026, 9, 1, 8, 0),
                LocalDateTime.of(2026, 9, 1, 8, 0),
                LocalDateTime.of(2026, 9, 1, 9, 0),
                "掌握核心概念");

        var result = service.updateSession("lesson-1", request, "teacher-1");

        assertEquals("新标题", result.title());
        assertEquals("掌握核心概念", result.teachingObjectives());
    }

    @Test
    void updateSessionRejectsWhenNotOwner() {
        LessonSessionEntity entity = session("lesson-1", "teacher-1");
        when(lessonSessionRepository.findById("lesson-1")).thenReturn(Optional.of(entity));

        var request = new UpdateSessionRequest("新标题", null, null, null, null);

        assertThrows(AccessDeniedException.class,
                () -> service.updateSession("lesson-1", request, "teacher-2"));
    }

    @Test
    void updateStatusRejectsWhenNotOwner() {
        LessonSessionEntity entity = session("lesson-1", "teacher-1");
        when(lessonSessionRepository.findByIdForUpdate("lesson-1")).thenReturn(Optional.of(entity));

        assertThrows(AccessDeniedException.class,
                () -> service.updateStatus("lesson-1", "IN_PROGRESS", "teacher-2"));
    }

    @Test
    void updateStatusSetsActualStartTime() {
        LessonSessionEntity entity = session("lesson-1", "teacher-1");
        when(lessonSessionRepository.findByIdForUpdate("lesson-1")).thenReturn(Optional.of(entity));

        var result = service.updateStatus("lesson-1", "IN_PROGRESS", "teacher-1");

        assertEquals("IN_PROGRESS", result.status());
        assertNotNull(entity.getActualStartTime());
        assertNotNull(entity.getStartTime());
    }

    @Test
    void updateStatusRejectsIllegalCompletedToInProgress() {
        LessonSessionEntity entity = session("lesson-1", "teacher-1");
        entity.setStatus(LessonSessionEntity.SessionStatus.COMPLETED);
        when(lessonSessionRepository.findByIdForUpdate("lesson-1")).thenReturn(Optional.of(entity));

        assertThrows(com.macteval.exception.BusinessException.class,
                () -> service.updateStatus("lesson-1", "IN_PROGRESS", "teacher-1"));
        assertEquals(LessonSessionEntity.SessionStatus.COMPLETED, entity.getStatus());
    }

    @Test
    void updateStatusIsIdempotentAndKeepsActualStartTime() {
        LessonSessionEntity entity = session("lesson-1", "teacher-1");
        entity.setStatus(LessonSessionEntity.SessionStatus.IN_PROGRESS);
        var actualStart = LocalDateTime.of(2026, 9, 1, 8, 5);
        entity.setActualStartTime(actualStart);
        when(lessonSessionRepository.findByIdForUpdate("lesson-1")).thenReturn(Optional.of(entity));

        service.updateStatus("lesson-1", "IN_PROGRESS", "teacher-1");

        assertEquals(actualStart, entity.getActualStartTime());
    }

    @Test
    void listSessionsForStudentClassFiltersByClass() {
        StudentEntity student = new StudentEntity();
        student.setId("student-1");
        student.setClassId("class-1");
        when(studentRepository.findById("student-1")).thenReturn(Optional.of(student));
        LessonSessionEntity s1 = session("s-1", "teacher-1");
        s1.setClassId("class-1");
        when(lessonSessionRepository.findByClassId("class-1")).thenReturn(java.util.List.of(s1));

        var result = service.listSessionsForStudentClass("student-1");

        assertEquals(1, result.size());
    }

    private LessonSessionEntity session(String id, String teacherId) {
        LessonSessionEntity entity = new LessonSessionEntity();
        entity.setId(id);
        entity.setCourseId("course-1");
        entity.setClassId("class-1");
        entity.setTeacherId(teacherId);
        entity.setTitle("原标题");
        entity.setDate(LocalDateTime.now());
        entity.setStatus(LessonSessionEntity.SessionStatus.SCHEDULED);
        return entity;
    }
}
