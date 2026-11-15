package com.macteval.controller;

import com.macteval.dto.*;
import com.macteval.service.LessonSessionService;
import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/sessions")
@RequiredArgsConstructor
public class LessonSessionController {

    private final LessonSessionService lessonSessionService;

    @GetMapping
    public ResponseEntity<ApiResponse<List<LessonSessionDTO>>> listSessions(
            @RequestParam(required = false) String teacherId,
            @RequestParam(required = false) String courseId,
            Authentication auth) {
        if (auth != null && isStudent(auth)) {
            return ApiResponse.ok(lessonSessionService.listSessionsForStudentClass(auth.getName()));
        }
        boolean isAdmin = auth != null && auth.getAuthorities().stream()
                .anyMatch(a -> "ROLE_admin".equals(a.getAuthority()));
        // 非管理员不得通过 teacherId 水平越权读取他人课堂。
        // 管理员未指定 teacherId 时必须传 null，才能进入 service 的全局分支；
        // 传管理员自己的 id 会误查“该管理员作为教师”的课堂（通常为空）。
        String tid;
        if (isAdmin) {
            tid = (teacherId != null && !teacherId.isBlank()) ? teacherId : null;
        } else {
            tid = auth != null ? auth.getName() : null;
        }
        return ApiResponse.ok(lessonSessionService.listSessions(tid, courseId, isAdmin));
    }

    private boolean isStudent(Authentication auth) {
        return auth.getAuthorities().stream()
                .anyMatch(a -> "ROLE_student".equals(a.getAuthority()));
    }

    @GetMapping("/page")
    public ResponseEntity<ApiResponse<PageResponse<LessonSessionDTO>>> listSessionsPage(
            @RequestParam(required = false) String courseId,
            @RequestParam(defaultValue = "0") int page,
            @RequestParam(defaultValue = "10") int size,
            Authentication auth) {
        if (auth != null && isStudent(auth)) {
            // 学生端课堂数量有限，复用班级列表后分页。
            var all = lessonSessionService.listSessionsForStudentClass(auth.getName());
            int pageSize = Math.max(1, Math.min(size, 100));
            int from = Math.min(Math.max(0, page) * pageSize, all.size());
            int to = Math.min(from + pageSize, all.size());
            return ApiResponse.ok(new PageResponse<>(all.subList(from, to),
                    Math.max(0, page), pageSize, all.size()));
        }
        boolean isAdmin = auth != null && auth.getAuthorities().stream()
                .anyMatch(a -> "ROLE_admin".equals(a.getAuthority()));
        return ApiResponse.ok(lessonSessionService.listSessionsPage(
                auth != null ? auth.getName() : null, courseId, isAdmin, page, size));
    }

    @GetMapping("/{id}")
    public ResponseEntity<ApiResponse<LessonSessionDTO>> getSession(
            @PathVariable String id, Authentication auth) {
        return ApiResponse.ok(lessonSessionService.getSessionForUser(
                id, auth.getName(), roleOf(auth)));
    }

    @PostMapping
    @PreAuthorize("hasAnyRole('teacher','admin')")
    public ResponseEntity<ApiResponse<LessonSessionDTO>> createSession(
            @Valid @RequestBody LessonSessionCreateRequest request,
            Authentication auth) {
        return ApiResponse.ok(lessonSessionService.createSession(
                request, auth.getName(), hasRole(auth, "ROLE_admin")));
    }

    private boolean hasRole(Authentication auth, String role) {
        return auth != null && auth.getAuthorities().stream()
                .anyMatch(a -> role.equals(a.getAuthority()));
    }

    private String roleOf(Authentication auth) {
        if (hasRole(auth, "ROLE_admin")) return "admin";
        if (hasRole(auth, "ROLE_student")) return "student";
        return "teacher";
    }

    @PatchMapping("/{id}/status")
    @PreAuthorize("hasAnyRole('teacher','admin')")
    public ResponseEntity<ApiResponse<LessonSessionDTO>> updateStatus(
            @PathVariable String id,
            @RequestBody Map<String, String> body,
            Authentication auth) {
        return ApiResponse.ok(lessonSessionService.updateStatus(
                id, body.get("status"), auth.getName(), hasRole(auth, "ROLE_admin")));
    }

    /** 管理员异常重开已结束课堂（教师不可用）。 */
    @PostMapping("/{id}/reopen")
    @PreAuthorize("hasRole('admin')")
    public ResponseEntity<ApiResponse<LessonSessionDTO>> reopen(
            @PathVariable String id, Authentication auth) {
        return ApiResponse.ok(lessonSessionService.reopenSession(id, auth.getName()));
    }

    @PutMapping("/{id}")
    @PreAuthorize("hasAnyRole('teacher','admin')")
    public ResponseEntity<ApiResponse<LessonSessionDTO>> updateSession(
            @PathVariable String id,
            @Valid @RequestBody UpdateSessionRequest request,
            Authentication auth) {
        return ApiResponse.ok(lessonSessionService.updateSession(
                id, request, auth.getName()));
    }

    @DeleteMapping("/{id}")
    @PreAuthorize("hasAnyRole('teacher','admin')")
    public ResponseEntity<ApiResponse<String>> deleteSession(
            @PathVariable String id, Authentication auth) {
        lessonSessionService.deleteSession(id, auth.getName(), hasRole(auth, "ROLE_admin"));
        return ApiResponse.ok("删除成功");
    }
}
