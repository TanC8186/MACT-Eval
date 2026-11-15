package com.macteval.service;

import com.macteval.dto.AttendanceConfirmRequest;
import com.macteval.dto.AttendanceDTO;
import com.macteval.dto.CheckinRequest;
import com.macteval.model.entity.AttendanceRecordEntity;
import com.macteval.model.entity.LessonSessionEntity;
import com.macteval.model.entity.StudentEntity;
import com.macteval.repository.AttendanceRecordRepository;
import com.macteval.repository.LessonSessionRepository;
import com.macteval.repository.StudentRepository;
import com.macteval.security.ResourceGuard;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.security.access.AccessDeniedException;

import java.util.List;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class AttendanceServiceTest {

    private AttendanceRecordRepository attendanceRepository;
    private StudentRepository studentRepository;
    private LessonSessionRepository lessonSessionRepository;
    private AttendanceService service;

    @BeforeEach
    void setUp() {
        attendanceRepository = mock(AttendanceRecordRepository.class);
        studentRepository = mock(StudentRepository.class);
        lessonSessionRepository = mock(LessonSessionRepository.class);
        service = new AttendanceService(
                attendanceRepository, studentRepository, lessonSessionRepository, new ResourceGuard());
    }

    @Test
    void initializeRecordsDerivesClassFromSession() {
        when(lessonSessionRepository.findById("lesson-1"))
                .thenReturn(Optional.of(session("lesson-1", "class-1", "teacher-1")));
        when(studentRepository.findByClassId("class-1"))
                .thenReturn(List.of(student("s-1", "class-1"), student("s-2", "class-1")));

        service.initializeRecords("lesson-1", "teacher-1");

        // 按名册逐人执行 ON CONFLICT DO NOTHING：已有记录不会被覆盖，
        // 缺失记录被补齐（不再因为“存在任意一条”就跳过整班）。
        verify(attendanceRepository, times(2)).insertPendingIfAbsent(any(), any(), any());
    }

    @Test
    void initializeRecordsRejectsNonOwner() {
        when(lessonSessionRepository.findById("lesson-1"))
                .thenReturn(Optional.of(session("lesson-1", "class-1", "teacher-1")));

        assertThrows(AccessDeniedException.class,
                () -> service.initializeRecords("lesson-1", "teacher-2"));
    }

    @Test
    void initializeRecordsRejectsSessionWithoutClass() {
        LessonSessionEntity s = session("lesson-1", null, "teacher-1");
        when(lessonSessionRepository.findById("lesson-1")).thenReturn(Optional.of(s));

        assertThrows(com.macteval.exception.BusinessException.class,
                () -> service.initializeRecords("lesson-1", "teacher-1"));
    }

    @Test
    void confirmRecordsRejectsNonOwner() {
        when(lessonSessionRepository.findById("lesson-1"))
                .thenReturn(Optional.of(session("lesson-1", "class-1", "teacher-1")));
        var request = new AttendanceConfirmRequest("lesson-1",
                List.of(new AttendanceConfirmRequest.AttendanceItem("r-1", "PRESENT", null)));

        assertThrows(AccessDeniedException.class,
                () -> service.confirmRecords(request, "teacher-2"));
    }

    @Test
    void confirmRecordsAppliesStatusAndConfirmation() {
        when(lessonSessionRepository.findById("lesson-1"))
                .thenReturn(Optional.of(session("lesson-1", "class-1", "teacher-1")));
        AttendanceRecordEntity rec = record("r-1", "s-1", AttendanceRecordEntity.AttendanceStatus.PENDING);
        when(attendanceRepository.findById("r-1")).thenReturn(Optional.of(rec));
        when(attendanceRepository.findByLessonSessionId("lesson-1")).thenReturn(List.of(rec));
        when(studentRepository.findById("s-1")).thenReturn(Optional.of(student("s-1", "class-1")));

        var request = new AttendanceConfirmRequest("lesson-1",
                List.of(new AttendanceConfirmRequest.AttendanceItem("r-1", "EXCUSED", "病假")));
        var result = service.confirmRecords(request, "teacher-1");

        assertEquals(AttendanceRecordEntity.AttendanceStatus.EXCUSED.name(), rec.getStatus().name());
        assertEquals(true, rec.isTeacherConfirmed());
        assertEquals("病假", rec.getTeacherComment());
        verify(attendanceRepository).saveAll(List.of(rec));
    }

    @Test
    void confirmRecordsSkipsRecordFromAnotherSession() {
        when(lessonSessionRepository.findById("lesson-1"))
                .thenReturn(Optional.of(session("lesson-1", "class-1", "teacher-1")));
        AttendanceRecordEntity other = record("r-9", "s-9", AttendanceRecordEntity.AttendanceStatus.PENDING);
        other.setLessonSessionId("lesson-OTHER");
        when(attendanceRepository.findById("r-9")).thenReturn(Optional.of(other));
        when(attendanceRepository.findByLessonSessionId("lesson-1")).thenReturn(List.of());

        var request = new AttendanceConfirmRequest("lesson-1",
                List.of(new AttendanceConfirmRequest.AttendanceItem("r-9", "PRESENT", null)));

        assertThrows(com.macteval.exception.BusinessException.class,
                () -> service.confirmRecords(request, "teacher-1"));
        verify(attendanceRepository, org.mockito.Mockito.never()).saveAll(any());
    }

    @Test
    void getRecordsRejectsNonOwner() {
        when(lessonSessionRepository.findById("lesson-1"))
                .thenReturn(Optional.of(session("lesson-1", "class-1", "teacher-1")));

        assertThrows(AccessDeniedException.class,
                () -> service.getRecords("lesson-1", "teacher-2"));
    }

    @Test
    void getSummaryCountsByStatus() {
        when(lessonSessionRepository.findById("lesson-1"))
                .thenReturn(Optional.of(session("lesson-1", "class-1", "teacher-1")));
        AttendanceRecordEntity present = record("r-1", "s-1", AttendanceRecordEntity.AttendanceStatus.PRESENT);
        AttendanceRecordEntity late = record("r-2", "s-2", AttendanceRecordEntity.AttendanceStatus.LATE);
        AttendanceRecordEntity absent = record("r-3", "s-3", AttendanceRecordEntity.AttendanceStatus.ABSENT);
        AttendanceRecordEntity excused = record("r-4", "s-4", AttendanceRecordEntity.AttendanceStatus.EXCUSED);
        AttendanceRecordEntity pending = record("r-5", "s-5", AttendanceRecordEntity.AttendanceStatus.PENDING);
        when(attendanceRepository.findByLessonSessionId("lesson-1"))
                .thenReturn(List.of(present, late, absent, excused, pending));

        var summary = service.getSummary("lesson-1", "teacher-1");

        assertEquals(5, summary.totalStudents());
        assertEquals(1, summary.present());
        assertEquals(1, summary.late());
        assertEquals(1, summary.absent());
        assertEquals(1, summary.excused());
        assertEquals(1, summary.pending());
    }

    @Test
    void checkinStillAllowedForOwnStudent() {
        when(lessonSessionRepository.findById("lesson-1"))
                .thenReturn(Optional.of(session("lesson-1", "class-1", "teacher-1")));
        when(studentRepository.findById("s-1"))
                .thenReturn(Optional.of(student("s-1", "class-1")));
        AttendanceRecordEntity existing = record("r-1", "s-1", AttendanceRecordEntity.AttendanceStatus.PENDING);
        when(attendanceRepository.findByLessonSessionIdAndStudentId("lesson-1", "s-1"))
                .thenReturn(List.of(existing));

        var dto = service.checkin(new CheckinRequest("lesson-1", "s-1"), "s-1");

        assertEquals(AttendanceRecordEntity.AttendanceStatus.PRESENT.name(), dto.status());
        verify(attendanceRepository).save(existing);
        assertEquals(false, existing.isTeacherConfirmed());
    }

    @Test
    void checkinRejectsStudentFromAnotherClass() {
        when(lessonSessionRepository.findById("lesson-1"))
                .thenReturn(Optional.of(session("lesson-1", "class-1", "teacher-1")));
        when(studentRepository.findById("s-9"))
                .thenReturn(Optional.of(student("s-9", "class-OTHER")));

        assertThrows(AccessDeniedException.class,
                () -> service.checkin(new CheckinRequest("lesson-1", "s-9"), "s-9"));
    }

    @Test
    void checkinRejectsCompletedSession() {
        LessonSessionEntity s = session("lesson-1", "class-1", "teacher-1");
        s.setStatus(LessonSessionEntity.SessionStatus.COMPLETED);
        when(lessonSessionRepository.findById("lesson-1")).thenReturn(Optional.of(s));
        when(studentRepository.findById("s-1")).thenReturn(Optional.of(student("s-1", "class-1")));

        assertThrows(com.macteval.exception.BusinessException.class,
                () -> service.checkin(new CheckinRequest("lesson-1", "s-1"), "s-1"));
        verify(attendanceRepository, org.mockito.Mockito.never()).save(any());
    }

    @Test
    void checkinCannotOverwriteTeacherConfirmedAbsence() {
        when(lessonSessionRepository.findById("lesson-1"))
                .thenReturn(Optional.of(session("lesson-1", "class-1", "teacher-1")));
        when(studentRepository.findById("s-1"))
                .thenReturn(Optional.of(student("s-1", "class-1")));
        AttendanceRecordEntity existing = record("r-1", "s-1", AttendanceRecordEntity.AttendanceStatus.ABSENT);
        existing.setTeacherConfirmed(true);
        when(attendanceRepository.findByLessonSessionIdAndStudentId("lesson-1", "s-1"))
                .thenReturn(List.of(existing));

        assertThrows(com.macteval.exception.BusinessException.class,
                () -> service.checkin(new CheckinRequest("lesson-1", "s-1"), "s-1"));
        assertEquals(AttendanceRecordEntity.AttendanceStatus.ABSENT, existing.getStatus());
        assertTrue(existing.isTeacherConfirmed());
        verify(attendanceRepository, org.mockito.Mockito.never()).save(any());
    }

    @Test
    void checkinRejectsMissingClassAssignment() {
        LessonSessionEntity s = session("lesson-1", null, "teacher-1");
        when(lessonSessionRepository.findById("lesson-1")).thenReturn(Optional.of(s));
        when(studentRepository.findById("s-1")).thenReturn(Optional.of(student("s-1", null)));

        assertThrows(com.macteval.exception.BusinessException.class,
                () -> service.checkin(new CheckinRequest("lesson-1", "s-1"), "s-1"));
    }

    private LessonSessionEntity session(String id, String classId, String teacherId) {
        LessonSessionEntity s = new LessonSessionEntity();
        s.setId(id);
        s.setClassId(classId);
        s.setTeacherId(teacherId);
        s.setStatus(LessonSessionEntity.SessionStatus.IN_PROGRESS);
        return s;
    }

    private StudentEntity student(String id, String classId) {
        StudentEntity s = new StudentEntity();
        s.setId(id);
        s.setClassId(classId);
        s.setName("学生");
        return s;
    }

    private AttendanceRecordEntity record(String id, String studentId, AttendanceRecordEntity.AttendanceStatus status) {
        AttendanceRecordEntity r = new AttendanceRecordEntity();
        r.setId(id);
        r.setLessonSessionId("lesson-1");
        r.setStudentId(studentId);
        r.setStatus(status);
        r.setSource(AttendanceRecordEntity.AttendanceSource.TEACHER_CORRECTION);
        return r;
    }
}
