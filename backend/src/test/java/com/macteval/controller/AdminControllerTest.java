package com.macteval.controller;

import com.macteval.dto.BatchImportRequest;
import com.macteval.model.entity.ClassGroupEntity;
import com.macteval.model.entity.StudentEntity;
import com.macteval.repository.ClassGroupRepository;
import com.macteval.repository.CourseRepository;
import com.macteval.repository.LessonSessionRepository;
import com.macteval.repository.StudentRepository;
import com.macteval.repository.UserRepository;
import com.macteval.service.StudentImportService;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.springframework.security.crypto.password.PasswordEncoder;

import java.util.List;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class AdminControllerTest {

    @Test
    void statsUseRepositoryCountsInsteadOfFixedZeros() {
        StudentRepository students = mock(StudentRepository.class);
        ClassGroupRepository classes = mock(ClassGroupRepository.class);
        CourseRepository courses = mock(CourseRepository.class);
        LessonSessionRepository lessons = mock(LessonSessionRepository.class);
        when(students.count()).thenReturn(30L);
        when(classes.count()).thenReturn(2L);
        when(courses.count()).thenReturn(4L);
        when(lessons.count()).thenReturn(12L);

        AdminController controller = new AdminController(
                students, classes, courses, lessons, mock(StudentImportService.class));
        var response = controller.getStats();
        var data = response.getBody().data();

        assertEquals(30L, data.get("studentCount"));
        assertEquals(2L, data.get("classCount"));
        assertEquals(4L, data.get("courseCount"));
        assertEquals(12L, data.get("lessonCount"));
    }

    @Test
    void batchImportSetsInitialPasswordAndRequiresChange() {
        StudentRepository students = mock(StudentRepository.class);
        ClassGroupRepository classes = mock(ClassGroupRepository.class);
        PasswordEncoder encoder = mock(PasswordEncoder.class);
        UserRepository users = mock(UserRepository.class);

        when(classes.findById("class-1")).thenReturn(Optional.of(new ClassGroupEntity()));
        when(students.findByStudentId("S001")).thenReturn(Optional.empty());
        when(users.findByPhone("13800000000")).thenReturn(Optional.empty());
        when(encoder.encode("123456")).thenReturn("hashed-123456");

        StudentImportService importService = new StudentImportService(students, users, classes, encoder);
        AdminController controller = new AdminController(
                students, classes, mock(CourseRepository.class),
                mock(LessonSessionRepository.class), importService);

        var request = new BatchImportRequest(
                "class-1",
                List.of(new BatchImportRequest.StudentEntry("S001", "张三", "13800000000", 1, 1)));

        var response = controller.batchImportStudents(request);

        ArgumentCaptor<StudentEntity> captor = ArgumentCaptor.forClass(StudentEntity.class);
        verify(students).saveAndFlush(captor.capture());
        assertEquals("hashed-123456", captor.getValue().getPassword());
        assertEquals("S001", captor.getValue().getStudentId());
        assertEquals("class-1", captor.getValue().getClassId());
        assertTrue(captor.getValue().isMustChangePassword());
        assertEquals(1, response.getBody().data().imported());
    }
}
