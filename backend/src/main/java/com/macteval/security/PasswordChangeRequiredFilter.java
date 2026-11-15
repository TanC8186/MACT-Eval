package com.macteval.security;

import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import lombok.RequiredArgsConstructor;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;

/**
 * 初始密码强制更换：管理员导入的学生账号首次登录只能读取资料、修改密码和登出。
 *
 * <p>restrict 依据 access token 中的 {@code pwd_change_required} 声明；修改密码
 * 成功后服务端返回新令牌（声明为 false），旧令牌在到期前仍被本过滤器拦截。</p>
 */
@Component
@RequiredArgsConstructor
public class PasswordChangeRequiredFilter extends OncePerRequestFilter {

    public static final String ATTRIBUTE = "mact.mustChangePassword";

    private static final Set<String> ALLOWED_PATHS = Set.of(
            "/auth/password",
            "/auth/profile",
            "/auth/logout",
            "/auth/refresh"
    );

    private final ObjectMapper objectMapper;

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response,
                                    FilterChain filterChain) throws ServletException, IOException {
        if (!Boolean.TRUE.equals(request.getAttribute(ATTRIBUTE))) {
            filterChain.doFilter(request, response);
            return;
        }
        String path = request.getRequestURI();
        String context = request.getContextPath();
        if (context != null && !context.isEmpty() && path.startsWith(context)) {
            path = path.substring(context.length());
        }
        if (ALLOWED_PATHS.contains(path)) {
            filterChain.doFilter(request, response);
            return;
        }

        response.setStatus(HttpServletResponse.SC_FORBIDDEN);
        response.setContentType(MediaType.APPLICATION_JSON_VALUE);
        response.setCharacterEncoding(StandardCharsets.UTF_8.name());
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("code", 428);
        body.put("message", "请先修改初始密码后再使用其他功能");
        body.put("data", null);
        response.getWriter().write(objectMapper.writeValueAsString(body));
    }
}
