package com.macteval.security;

import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.servlet.http.HttpServletResponse;
import lombok.RequiredArgsConstructor;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.HttpMethod;
import org.springframework.http.MediaType;
import org.springframework.security.config.annotation.method.configuration.EnableMethodSecurity;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configuration.EnableWebSecurity;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.security.web.authentication.UsernamePasswordAuthenticationFilter;
import org.springframework.security.web.header.writers.ReferrerPolicyHeaderWriter;
import org.springframework.security.web.header.writers.CrossOriginOpenerPolicyHeaderWriter;
import org.springframework.security.web.header.writers.CrossOriginResourcePolicyHeaderWriter;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Map;

@Configuration
@EnableWebSecurity
@EnableMethodSecurity
@RequiredArgsConstructor
public class SecurityConfig {

    private final JwtAuthenticationFilter jwtAuthFilter;
    private final PasswordChangeRequiredFilter passwordChangeRequiredFilter;
    private final ObjectMapper objectMapper;

    @Bean
    public SecurityFilterChain securityFilterChain(HttpSecurity http) throws Exception {
        http
            .csrf(csrf -> csrf.disable())
            .cors(cors -> {})
            // 安全响应头（SEC-01，2026-10-28）。
            //
            // 责任划分：**静态页面**的 CSP 由 teacher-web 的 nginx 发（只有它能
            // 覆盖 index.html 与 assets）；这里的是**针对 API 响应**的更严格策略。
            // 浏览器对多条 CSP 取交集而不是覆盖，因此反代 /api/ 时 nginx 用
            // proxy_hide_header 去掉这里这份，保证浏览器只收到一条页面策略。
            //
            // 为什么 API 也要 CSP：JSON 响应若被直接当导航目标打开，或通过
            // <iframe> 嵌入，没有 default-src 'none' 就等于允许它加载任意子资源。
            // frame-ancestors 'none' 同时是点击劫持的兜底（与 X-Frame-Options 并存，
            // 后者供不认 CSP 的旧客户端）。
            .headers(headers -> {
                // 每一条都写成独立语句而不是长链：HeadersConfigurer 的若干
                // Customizer 重载返回的是子配置对象而不是自身，链式写法会在
                // 中间某一环断掉（`permissionsPolicy(...)` 返回
                // PermissionsPolicyConfig，接着调 crossOriginOpenerPolicy 编译失败）。
                headers.contentSecurityPolicy(csp -> csp
                        .policyDirectives("default-src 'none'; frame-ancestors 'none'; sandbox"));
                // 显式写出而非依赖框架默认值：默认值随 Spring Security 大版本变过，
                // 而这些都属于"缺了不报错、只是不安全"的类型。
                headers.contentTypeOptions(contentType -> { });
                headers.frameOptions(frame -> frame.deny());
                headers.referrerPolicy(referrer -> referrer
                        .policy(ReferrerPolicyHeaderWriter.ReferrerPolicy.NO_REFERRER));
                headers.permissionsPolicy(permissions -> permissions
                        .policy("camera=(), microphone=(), geolocation=(), payment=(), usb=()"));
                headers.crossOriginOpenerPolicy(coop -> coop
                        .policy(CrossOriginOpenerPolicyHeaderWriter.CrossOriginOpenerPolicy.SAME_ORIGIN));
                headers.crossOriginResourcePolicy(corp -> corp
                        .policy(CrossOriginResourcePolicyHeaderWriter.CrossOriginResourcePolicy.SAME_ORIGIN));
            })
            .sessionManagement(session -> session.sessionCreationPolicy(SessionCreationPolicy.STATELESS))
            .authorizeHttpRequests(auth -> auth
                .requestMatchers("/auth/login", "/auth/register", "/auth/refresh", "/auth/logout").permitAll()
                .requestMatchers("/ws/**").permitAll()
                .requestMatchers("/jobs/*/complete", "/jobs/*/claim", "/jobs/*/heartbeat").permitAll()
                // 内网状态查询：身份由 X-Service-Token 校验（与 claim/heartbeat 一致），
                // 因此不要求 JWT；缺少/错误令牌由 ServiceTokenValidator 拒绝 403。
                .requestMatchers(HttpMethod.GET, "/jobs/*/internal-status").permitAll()
                .requestMatchers("/health").permitAll()
                .requestMatchers(HttpMethod.GET, "/demo/**").permitAll()
                .requestMatchers("/admin/**").hasRole("admin")
                .anyRequest().authenticated()
            )
            .exceptionHandling(ex -> ex
                .authenticationEntryPoint((request, response, authException) ->
                        writeError(response, 401, "未认证或登录已过期"))
                .accessDeniedHandler((request, response, accessDeniedException) ->
                        writeError(response, 403, "无权访问该资源"))
            )
            .addFilterBefore(jwtAuthFilter, UsernamePasswordAuthenticationFilter.class)
            .addFilterAfter(passwordChangeRequiredFilter, JwtAuthenticationFilter.class);

        return http.build();
    }

    private void writeError(HttpServletResponse response, int code, String message) throws IOException {
        response.setStatus(code);
        response.setContentType(MediaType.APPLICATION_JSON_VALUE);
        response.setCharacterEncoding(StandardCharsets.UTF_8.name());
        // Emit the ApiResponse shape {code, message, data:null} verbatim.
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("code", code);
        body.put("message", message);
        body.put("data", null);
        response.getWriter().write(objectMapper.writeValueAsString(body));
    }

    @Bean
    public PasswordEncoder passwordEncoder() {
        return new BCryptPasswordEncoder();
    }
}
