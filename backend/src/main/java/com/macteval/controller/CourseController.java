package com.macteval.controller;

import com.macteval.dto.*;
import com.macteval.security.ResourceGuard;
import com.macteval.service.CourseService;
import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
@RequestMapping("/courses")
@RequiredArgsConstructor
public class CourseController {

    private final CourseService courseService;
    private final ResourceGuard resourceGuard;

    @GetMapping
    public ResponseEntity<ApiResponse<List<CourseDTO>>> listCourses(
            @RequestParam(required = false) String teacherId,
            Authentication auth) {
        boolean isStudent = auth != null && auth.getAuthorities().stream()
                .anyMatch(a -> a.getAuthority().equals("ROLE_student"));
        if (isStudent) {
            return ApiResponse.ok(courseService.listCoursesForStudent(auth.getName()));
        }
        boolean isAdmin = auth != null && auth.getAuthorities().stream()
                .anyMatch(a -> "ROLE_admin".equals(a.getAuthority()));
        // 非管理员不得通过 teacherId 水平越权读取他人课程
        String tid = isAdmin && teacherId != null ? teacherId : (auth != null ? auth.getName() : null);
        return ApiResponse.ok(courseService.listCourses(tid));
    }

    @GetMapping("/page")
    public ResponseEntity<ApiResponse<PageResponse<CourseDTO>>> listCoursesPage(
            @RequestParam(defaultValue = "0") int page,
            @RequestParam(defaultValue = "20") int size,
            Authentication auth) {
        String role = auth.getAuthorities().stream()
                .anyMatch(a -> "ROLE_admin".equals(a.getAuthority())) ? "admin"
                : auth.getAuthorities().stream()
                        .anyMatch(a -> "ROLE_student".equals(a.getAuthority())) ? "student"
                : "teacher";
        return ApiResponse.ok(courseService.listCoursesPage(auth.getName(), role, page, size));
    }

    @GetMapping("/{id}")
    public ResponseEntity<ApiResponse<CourseDTO>> getCourse(
            @PathVariable String id, Authentication auth) {
        String role = auth.getAuthorities().stream()
                .anyMatch(a -> "ROLE_admin".equals(a.getAuthority())) ? "admin"
                : auth.getAuthorities().stream()
                        .anyMatch(a -> "ROLE_student".equals(a.getAuthority())) ? "student"
                : "teacher";
        return ApiResponse.ok(courseService.getCourseForUser(id, auth.getName(), role));
    }

    @PostMapping
    @PreAuthorize("hasAnyRole('teacher','admin')")
    public ResponseEntity<ApiResponse<CourseDTO>> createCourse(
            @Valid @RequestBody CourseCreateRequest request,
            Authentication auth) {
        String teacherId = auth != null ? auth.getName() : "unknown";
        return ApiResponse.ok(courseService.createCourse(request, teacherId));
    }

    @PutMapping("/{id}")
    @PreAuthorize("hasAnyRole('teacher','admin')")
    public ResponseEntity<ApiResponse<CourseDTO>> updateCourse(
            @PathVariable String id,
            @Valid @RequestBody CourseCreateRequest request,
            Authentication auth) {
        resourceGuard.assertTeacherOwnsCourse(auth.getName(), courseService.getCourse(id).teacherId());
        return ApiResponse.ok(courseService.updateCourse(id, request));
    }

    @DeleteMapping("/{id}")
    @PreAuthorize("hasAnyRole('teacher','admin')")
    public ResponseEntity<ApiResponse<String>> deleteCourse(
            @PathVariable String id, Authentication auth) {
        resourceGuard.assertTeacherOwnsCourse(auth.getName(), courseService.getCourse(id).teacherId());
        courseService.deleteCourse(id);
        return ApiResponse.ok("删除成功");
    }
}
