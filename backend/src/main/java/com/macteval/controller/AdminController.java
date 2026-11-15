package com.macteval.controller;

import com.macteval.dto.ApiResponse;
import com.macteval.dto.BatchImportRequest;
import com.macteval.dto.StudentImportResult;
import com.macteval.repository.ClassGroupRepository;
import com.macteval.repository.CourseRepository;
import com.macteval.repository.LessonSessionRepository;
import com.macteval.repository.StudentRepository;
import com.macteval.service.StudentImportService;
import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.web.bind.annotation.*;

import java.util.Map;

/**
 * 管理员控制器 — 批量导入、用户管理、数据管理。
 */
@Slf4j
@RestController
@RequestMapping("/admin")
@RequiredArgsConstructor
@PreAuthorize("hasRole('admin')")
public class AdminController {

    private final StudentRepository studentRepository;
    private final ClassGroupRepository classGroupRepository;
    private final CourseRepository courseRepository;
    private final LessonSessionRepository lessonSessionRepository;
    private final StudentImportService studentImportService;

    /**
     * 批量导入学生（按学号幂等；逐行结构化结果）。
     */
    @PostMapping("/students/import")
    public ResponseEntity<ApiResponse<StudentImportResult>> batchImportStudents(
            @Valid @RequestBody BatchImportRequest request) {
        return ApiResponse.ok(studentImportService.importStudents(request));
    }

    /**
     * 获取系统统计概览
     */
    @GetMapping("/stats")
    public ResponseEntity<ApiResponse<Map<String, Object>>> getStats() {
        long studentCount = studentRepository.count();
        return ApiResponse.ok(Map.of(
                "studentCount", studentCount,
                "classCount", classGroupRepository.count(),
                "courseCount", courseRepository.count(),
                "lessonCount", lessonSessionRepository.count()
        ));
    }
}
