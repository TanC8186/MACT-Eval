package com.macteval.service;

import com.macteval.model.entity.CourseEntity;
import com.macteval.model.entity.LessonSessionEntity;
import com.macteval.model.entity.StudentEntity;
import com.macteval.repository.CourseRepository;
import com.macteval.repository.LessonSessionRepository;
import com.macteval.repository.StudentRepository;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

class CourseServiceStudentTest {

    @Test
    void listCoursesForStudentUsesStudentClassSessions() {
        CourseRepository courseRepository = mock(CourseRepository.class);
        LessonSessionRepository lessonSessionRepository = mock(LessonSessionRepository.class);
        StudentRepository studentRepository = mock(StudentRepository.class);
        CourseService service = new CourseService(
                courseRepository,
                lessonSessionRepository,
                studentRepository
        );

        StudentEntity student = new StudentEntity();
        student.setId("student-1");
        student.setClassId("class-1");

        LessonSessionEntity firstSession = new LessonSessionEntity();
        firstSession.setCourseId("course-1");
        LessonSessionEntity duplicateSession = new LessonSessionEntity();
        duplicateSession.setCourseId("course-1");

        CourseEntity course = new CourseEntity();
        course.setId("course-1");
        course.setName("课堂评价");
        course.setTeacherId("teacher-1");

        when(studentRepository.findById("student-1")).thenReturn(Optional.of(student));
        when(lessonSessionRepository.findByClassId("class-1"))
                .thenReturn(List.of(firstSession, duplicateSession));
        when(courseRepository.findById("course-1")).thenReturn(Optional.of(course));

        var result = service.listCoursesForStudent("student-1");

        assertEquals(1, result.size());
        assertEquals("course-1", result.getFirst().id());
    }
}
