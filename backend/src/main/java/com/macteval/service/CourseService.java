package com.macteval.service;

import com.macteval.dto.*;
import org.springframework.data.domain.PageRequest;
import org.springframework.data.domain.Pageable;
import org.springframework.data.domain.Sort;
import com.macteval.model.entity.CourseEntity;
import com.macteval.repository.CourseRepository;
import com.macteval.repository.LessonSessionRepository;
import com.macteval.repository.StudentRepository;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;
import java.util.UUID;

@Slf4j
@Service
@RequiredArgsConstructor
public class CourseService {

    private final CourseRepository courseRepository;
    private final LessonSessionRepository lessonSessionRepository;
    private final StudentRepository studentRepository;

    @Transactional(readOnly = true)
    public List<CourseDTO> listCourses(String teacherId) {
        List<CourseEntity> courses = teacherId != null && !teacherId.isBlank()
                ? courseRepository.findByTeacherId(teacherId)
                : courseRepository.findAll();
        return courses.stream().map(this::toDTO).toList();
    }

    @Transactional(readOnly = true)
    public PageResponse<CourseDTO> listCoursesPage(String userId, String role, int page, int size) {
        int pageSize = Math.max(1, Math.min(size, 100));
        int pageIndex = Math.max(0, page);
        Pageable pageable = PageRequest.of(pageIndex, pageSize,
                Sort.by(Sort.Direction.ASC, "name"));
        if ("admin".equalsIgnoreCase(role) || userId == null || userId.isBlank()) {
            var result = courseRepository.findAll(pageable);
            return new PageResponse<>(result.getContent().stream().map(this::toDTO).toList(),
                    pageIndex, pageSize, result.getTotalElements());
        }
        if ("student".equalsIgnoreCase(role)) {
            List<CourseDTO> all = listCoursesForStudent(userId);
            int from = Math.min(pageIndex * pageSize, all.size());
            int to = Math.min(from + pageSize, all.size());
            return new PageResponse<>(all.subList(from, to), pageIndex, pageSize, all.size());
        }
        var result = courseRepository.findByTeacherId(userId, pageable);
        return new PageResponse<>(result.getContent().stream().map(this::toDTO).toList(),
                pageIndex, pageSize, result.getTotalElements());
    }

    @Transactional(readOnly = true)
    public List<CourseDTO> listCoursesForStudent(String studentUserId) {
        var student = studentRepository.findById(studentUserId)
                .orElseThrow(() -> new RuntimeException("学生账号不存在"));
        if (student.getClassId() == null || student.getClassId().isBlank()) {
            return List.of();
        }

        return lessonSessionRepository.findByClassId(student.getClassId()).stream()
                .map(session -> session.getCourseId())
                .distinct()
                .map(courseRepository::findById)
                .flatMap(java.util.Optional::stream)
                .map(this::toDTO)
                .sorted((left, right) -> left.name().compareToIgnoreCase(right.name()))
                .toList();
    }

    @Transactional(readOnly = true)
    public CourseDTO getCourse(String id) {
        CourseEntity course = courseRepository.findById(id)
                .orElseThrow(() -> new java.util.NoSuchElementException("课程不存在"));
        return toDTO(course);
    }

    /**
     * 课程详情授权：教师仅限自己的课程；学生仅限自己班级已开课的课程；管理员放行。
     */
    @Transactional(readOnly = true)
    public CourseDTO getCourseForUser(String id, String userId, String role) {
        CourseEntity course = courseRepository.findById(id)
                .orElseThrow(() -> new java.util.NoSuchElementException("课程不存在"));
        if ("admin".equalsIgnoreCase(role)) {
            return toDTO(course);
        }
        if ("teacher".equalsIgnoreCase(role)) {
            if (userId == null || !userId.equals(course.getTeacherId())) {
                throw new org.springframework.security.access.AccessDeniedException("无权访问其他教师的课程");
            }
            return toDTO(course);
        }
        if ("student".equalsIgnoreCase(role)) {
            var student = studentRepository.findById(userId)
                    .orElseThrow(() -> new java.util.NoSuchElementException("学生账号不存在"));
            if (student.getClassId() == null || student.getClassId().isBlank()) {
                throw new org.springframework.security.access.AccessDeniedException("学生未关联班级");
            }
            boolean enrolled = lessonSessionRepository.findByClassId(student.getClassId()).stream()
                    .anyMatch(s -> id.equals(s.getCourseId()));
            if (!enrolled) {
                throw new org.springframework.security.access.AccessDeniedException("只能访问自己班级的课程");
            }
            return toDTO(course);
        }
        throw new org.springframework.security.access.AccessDeniedException("无权访问该课程");
    }

    @Transactional
    public CourseDTO createCourse(CourseCreateRequest request, String teacherId) {
        CourseEntity course = new CourseEntity();
        course.setId(UUID.randomUUID().toString());
        course.setName(request.name());
        course.setCode(request.code());
        course.setDescription(request.description());
        course.setTeacherId(teacherId);
        course.setDepartment(request.department());
        courseRepository.save(course);
        log.info("Course created: {} by teacher {}", course.getId(), teacherId);
        return toDTO(course);
    }

    @Transactional
    public CourseDTO updateCourse(String id, CourseCreateRequest request) {
        CourseEntity course = courseRepository.findById(id)
                .orElseThrow(() -> new RuntimeException("课程不存在"));
        course.setName(request.name());
        course.setCode(request.code());
        course.setDescription(request.description());
        course.setDepartment(request.department());
        courseRepository.save(course);
        return toDTO(course);
    }

    @Transactional
    public void deleteCourse(String id) {
        long sessionCount = lessonSessionRepository.findByCourseId(id).size();
        if (sessionCount > 0) {
            throw new com.macteval.exception.BusinessException(
                    "该课程下仍有 " + sessionCount + " 个课堂，请先归档或删除课堂后再删除课程");
        }
        if (!courseRepository.existsById(id)) {
            throw new java.util.NoSuchElementException("课程不存在");
        }
        courseRepository.deleteById(id);
        log.info("Course deleted: {}", id);
    }

    private CourseDTO toDTO(CourseEntity entity) {
        return new CourseDTO(
                entity.getId(), entity.getName(), entity.getCode(),
                entity.getDescription(), entity.getTeacherId(),
                entity.getDepartment(),
                entity.getCreatedAt(), entity.getUpdatedAt()
        );
    }
}
