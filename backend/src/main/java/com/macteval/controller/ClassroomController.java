package com.macteval.controller;

import com.macteval.dto.ApiResponse;
import com.macteval.repository.LessonSessionRepository;
import com.macteval.repository.StudentRepository;
import lombok.RequiredArgsConstructor;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.Map;

/**
 * 教室管理控制器 — 学生列表、状态查询、教室配置。
 */
@RestController
@RequestMapping("/classroom")
@RequiredArgsConstructor
public class ClassroomController {

    private final StudentRepository studentRepository;
    private final LessonSessionRepository lessonSessionRepository;

    @GetMapping("/students")
    @PreAuthorize("hasAnyRole('teacher','admin')")
    public ResponseEntity<ApiResponse<List<Map<String, Object>>>> listStudents(
            @RequestParam(defaultValue = "") String classId,
            Authentication auth) {
        boolean isAdmin = auth.getAuthorities().stream()
                .anyMatch(a -> "ROLE_admin".equals(a.getAuthority()));
        if (classId.isBlank()) {
            if (!isAdmin) {
                return ApiResponse.error(400, "教师查询学生名单时必须指定班级");
            }
            // 管理员可查看全部；教师必须按班级查询并校验该班级属于自己的课堂。
        } else if (!isAdmin) {
            boolean teachesClass = lessonSessionRepository.findByTeacherId(auth.getName()).stream()
                    .anyMatch(session -> classId.equals(session.getClassId()));
            if (!teachesClass) {
                return ApiResponse.error(403, "无权查看其他教师班级的学生名单");
            }
        }
        var students = classId.isBlank()
                ? studentRepository.findAll()
                : studentRepository.findByClassId(classId);

        var result = students.stream()
                .map(s -> {
                    // Map.of 不接受 null；导入学生允许不填座位，必须保留 null 而不是 500。
                    Map<String, Object> row = new java.util.LinkedHashMap<>();
                    row.put("student_id", s.getStudentId() != null ? s.getStudentId() : s.getId());
                    row.put("name", s.getName());
                    row.put("seat_row", s.getSeatRow());
                    row.put("seat_col", s.getSeatCol());
                    return row;
                })
                .toList();
        return ApiResponse.ok(result);
    }

    @GetMapping("/{classroomId}/status")
    @PreAuthorize("hasAnyRole('teacher','admin')")
    public ResponseEntity<ApiResponse<Map<String, Object>>> getStatus(
            @PathVariable String classroomId,
            @RequestParam(defaultValue = "") String lessonId) {
        return ApiResponse.error(
                501,
                "教室实时状态采集尚未接通，当前不返回固定人数或模拟状态"
        );
    }

    @PostMapping("/config")
    @PreAuthorize("hasRole('admin')")
    public ResponseEntity<ApiResponse<Map<String, Object>>> configure(
            @RequestBody Map<String, Object> config) {
        return ApiResponse.error(
                501,
                "教室配置持久化尚未实现，当前不会将请求内容伪装为保存结果"
        );
    }
}
