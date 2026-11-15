package com.macteval.controller;

import com.macteval.repository.LessonSessionRepository;
import com.macteval.repository.StudentRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.authority.SimpleGrantedAuthority;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

class ClassroomControllerTest {

    private StudentRepository studentRepository;
    private ClassroomController controller;

    @BeforeEach
    void setUp() {
        studentRepository = mock(StudentRepository.class);
        controller = new ClassroomController(studentRepository, mock(LessonSessionRepository.class));
    }

    private org.springframework.security.core.Authentication adminAuth() {
        return new UsernamePasswordAuthenticationToken(
                "admin-1", null, List.of(new SimpleGrantedAuthority("ROLE_admin")));
    }

    @Test
    void emptyClassReturnsEmptyListInsteadOfDemoStudents() {
        when(studentRepository.findByClassId("class-001")).thenReturn(List.of());

        var response = controller.listStudents("class-001", adminAuth());

        assertEquals(200, response.getStatusCode().value());
        assertTrue(response.getBody().data().isEmpty());
    }

    @Test
    void rosterKeepsNullSeatInsteadOfReturning500() {
        com.macteval.model.entity.StudentEntity student = new com.macteval.model.entity.StudentEntity();
        student.setId("s-1");
        student.setStudentId("S001");
        student.setName("无座位学生");
        student.setSeatRow(null);
        student.setSeatCol(null);
        when(studentRepository.findByClassId("class-001")).thenReturn(List.of(student));

        var response = controller.listStudents("class-001", adminAuth());

        assertEquals(200, response.getStatusCode().value());
        assertEquals(1, response.getBody().data().size());
        assertEquals(null, response.getBody().data().getFirst().get("seat_row"));
    }

    @Test
    void statusReturnsNotImplementedInsteadOfFixedStudentCount() {
        var response = controller.getStatus("room-001", "lesson-001");

        assertEquals(501, response.getStatusCode().value());
    }

    @Test
    void configReturnsNotImplementedInsteadOfEchoingUnpersistedInput() {
        var response = controller.configure(Map.of("camera", "front"));

        assertEquals(501, response.getStatusCode().value());
    }
}
