package com.macteval.controller;

import com.macteval.dto.*;
import com.macteval.exception.BusinessException;
import com.macteval.exception.InvalidCredentialsException;
import com.macteval.security.WsTicketStore;
import com.macteval.service.AuthService;
import com.macteval.service.WsTicketService;
import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;

import java.util.Map;

@RestController
@RequestMapping("/auth")
@RequiredArgsConstructor
public class AuthController {

    private final AuthService authService;
    private final WsTicketService wsTicketService;

    @PostMapping("/login")
    public ResponseEntity<ApiResponse<AuthResponse>> login(@Valid @RequestBody LoginRequest request) {
        // 业务异常交给 GlobalExceptionHandler 映射，避免 NPE 等被误报成 401
        AuthResponse response = authService.login(request);
        return ApiResponse.ok(response);
    }

    @PostMapping("/register")
    public ResponseEntity<ApiResponse<AuthResponse>> register(@Valid @RequestBody RegisterRequest request) {
        AuthResponse response = authService.register(request);
        return ApiResponse.ok(response);
    }

    @PostMapping("/refresh")
    public ResponseEntity<ApiResponse<AuthResponse>> refresh(@Valid @RequestBody RefreshTokenRequest request) {
        AuthResponse response = authService.refreshToken(request);
        return ApiResponse.ok(response);
    }

    @GetMapping("/profile")
    public ResponseEntity<ApiResponse<UserProfileDTO>> profile(Authentication authentication) {
        if (authentication == null) {
            throw new InvalidCredentialsException("未认证");
        }
        UserProfileDTO profile = authService.getProfile(authentication.getName());
        return ApiResponse.ok(profile);
    }

    @PutMapping("/profile")
    public ResponseEntity<ApiResponse<UserProfileDTO>> updateProfile(
            @Valid @RequestBody ProfileUpdateRequest request,
            Authentication authentication) {
        return ApiResponse.ok(authService.updateProfile(authentication.getName(), request));
    }

    @PutMapping("/password")
    public ResponseEntity<ApiResponse<AuthResponse>> changePassword(
            @Valid @RequestBody PasswordChangeRequest request,
            Authentication authentication) {
        return ApiResponse.ok(authService.changePassword(authentication.getName(), request));
    }

    /**
     * 签发 WebSocket 一次性票据（SEC-02，2026-10-28）。
     *
     * <p>浏览器不能给 WebSocket 设 Authorization 头，所以旧实现把 access JWT
     * 放进查询串，结果被 nginx 访问日志记录。票据是 60 秒、单次消费、绑定课堂的
     * 随机串，即使落进日志也只能开一个连接。</p>
     *
     * <p>本接口需要登录：SecurityConfig 的 anyRequest().authenticated() 覆盖它
     * （不在 permitAll 列表里），因此 authentication.getName() 一定是有效用户 ID。</p>
     */
    @PostMapping("/ws-ticket")
    public ResponseEntity<ApiResponse<Map<String, Object>>> wsTicket(
            @Valid @RequestBody WsTicketRequest request,
            Authentication authentication) {
        if (authentication == null) {
            throw new InvalidCredentialsException("未认证");
        }
        String role = authentication.getAuthorities().stream()
                .findFirst()
                .map(a -> a.getAuthority().replaceFirst("^ROLE_", ""))
                .orElse("teacher");
        String ticket = wsTicketService.issue(
                request.lessonSessionId(), authentication.getName(), role);
        // 回传 ttlSeconds：前端必须"拿到即用"，不能缓存复用（票据单次消费）。
        return ApiResponse.ok(Map.of(
                "ticket", ticket,
                "ttlSeconds", WsTicketStore.TICKET_TTL.toSeconds()));
    }

    @PostMapping("/logout")
    public ResponseEntity<ApiResponse<String>> logout(
            @Valid @RequestBody RefreshTokenRequest request) {
        authService.logout(request);
        return ApiResponse.ok("已退出登录");
    }
}
