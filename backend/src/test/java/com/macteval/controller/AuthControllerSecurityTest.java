package com.macteval.controller;

import com.macteval.dto.ApiResponse;
import com.macteval.dto.RefreshTokenRequest;
import com.macteval.security.JwtTokenProvider;
import com.macteval.security.SecurityConfig;
import com.macteval.service.AuthService;
import com.macteval.service.WsTicketService;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.boot.test.mock.mockito.MockBean;
import org.springframework.context.annotation.Import;
import org.springframework.data.redis.connection.RedisConnectionFactory;
import org.springframework.http.MediaType;
import org.springframework.security.test.context.support.WithMockUser;
import org.springframework.test.web.servlet.MockMvc;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.verify;
import static org.springframework.security.test.web.servlet.request.SecurityMockMvcRequestPostProcessors.csrf;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

// @WebMvcTest 只加载 @EnableWebMvc 配置，不会加载生产 SecurityConfig；
// 显式 @Import 它以验证真实的 permitAll 过滤链。
@WebMvcTest(AuthController.class)
@Import(SecurityConfig.class)
class AuthControllerSecurityTest {

    @Autowired
    private MockMvc mockMvc;

    @MockBean
    private AuthService authService;

    // AuthController 新增 /auth/ws-ticket 后多了一个构造参数（SEC-02，2026-10-28）。
    // @WebMvcTest 只装配 Web 层，service 必须显式 mock，否则上下文启动失败。
    @MockBean
    private WsTicketService wsTicketService;

    @MockBean
    private JwtTokenProvider jwtTokenProvider;

    @MockBean
    private RedisConnectionFactory redisConnectionFactory;

    @Test
    void logoutWithoutAuthenticationIsPermitted() throws Exception {
        // 生产环境由 Servlet 容器剥离 context-path(/api)；MockMvc 切片不会自动应用，
        // 显式设置以让 SecurityConfig 的 /auth/logout 匹配器按生产语义命中。
        mockMvc.perform(post("/api/auth/logout")
                        .contextPath("/api")
                        .with(csrf())
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"refreshToken\":\"some-jti\"}"))
                .andExpect(status().isOk());

        verify(authService).logout(any(RefreshTokenRequest.class));
    }
}
