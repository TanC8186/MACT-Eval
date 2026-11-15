package com.macteval.security;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import lombok.RequiredArgsConstructor;
import org.slf4j.MDC;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.stereotype.Component;
import org.springframework.util.StringUtils;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;
import java.util.List;

@Component
@RequiredArgsConstructor
public class JwtAuthenticationFilter extends OncePerRequestFilter {

    private final JwtTokenProvider tokenProvider;

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response,
                                    FilterChain filterChain) throws ServletException, IOException {
        String token = extractToken(request);

        if (StringUtils.hasText(token)
                && tokenProvider.validateToken(token, JwtTokenProvider.TOKEN_TYPE_ACCESS)) {
            String userId = tokenProvider.getUserIdFromToken(token);
            String role = tokenProvider.getRoleFromToken(token);

            var authorities = List.of(new SimpleGrantedAuthority("ROLE_" + role));
            var authentication = new UsernamePasswordAuthenticationToken(
                    userId, null, authorities);
            SecurityContextHolder.getContext().setAuthentication(authentication);
            if (tokenProvider.isPasswordChangeRequired(token)) {
                request.setAttribute(PasswordChangeRequiredFilter.ATTRIBUTE, Boolean.TRUE);
            }
            // 日志关联：requestId 由 RequestIdFilter 写入；此处补 userId，
            // 请求结束后必须在 finally 清理，避免线程池复用污染下一条日志。
            MDC.put("userId", userId);
        }

        try {
            filterChain.doFilter(request, response);
        } finally {
            MDC.remove("userId");
        }
    }

    private String extractToken(HttpServletRequest request) {
        String bearerToken = request.getHeader("Authorization");
        if (StringUtils.hasText(bearerToken) && bearerToken.startsWith("Bearer ")) {
            return bearerToken.substring(7);
        }
        return null;
    }
}
