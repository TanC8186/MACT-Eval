package com.macteval.security;

import org.springframework.security.access.AccessDeniedException;
import org.springframework.security.core.Authentication;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.stereotype.Component;

/**
 * 资源属主断言工具 — 防止越权 (IDOR) 访问。
 *
 * <p>在 Controller / Service 层调用, 对「教师跨属主改删资源」「学生代他人操作」
 * 两类越权场景进行显式属主校验, 非属主直接抛出 {@link AccessDeniedException}(由
 * {@code GlobalExceptionHandler} 统一映射为 403)。</p>
 */
@Component
public class ResourceGuard {

    /**
     * 断言当前教师是资源(课程/课堂会话)的属主。
     *
     * @param teacherId       当前认证教师 ID ({@code auth.getName()})
     * @param courseTeacherId 资源所属课程的教师 ID
     * @throws AccessDeniedException 非属主时抛出
     */
    public void assertTeacherOwnsCourse(String teacherId, String courseTeacherId) {
        if (isAdmin()) {
            return;
        }
        if (teacherId == null || courseTeacherId == null || !teacherId.equals(courseTeacherId)) {
            throw new AccessDeniedException("无权操作其他教师的课程资源");
        }
    }

    private boolean isAdmin() {
        Authentication auth = SecurityContextHolder.getContext().getAuthentication();
        return auth != null && auth.getAuthorities().stream()
                .anyMatch(a -> "ROLE_admin".equals(a.getAuthority()));
    }

    /**
     * 断言学生只能操作自己的资源 (签到 / 答题等)。
     *
     * @param authenticatedId    当前认证用户 ID ({@code auth.getName()})
     * @param requestedStudentId 请求中携带的学生 ID
     * @throws AccessDeniedException 认证身份与请求中的 studentId 不一致时抛出
     */
    public void assertStudentOwns(String authenticatedId, String requestedStudentId) {
        if (authenticatedId == null || requestedStudentId == null || !authenticatedId.equals(requestedStudentId)) {
            throw new AccessDeniedException("只能操作自己的资源");
        }
    }
}
