package com.macteval.controller;

import com.macteval.dto.ApiResponse;
import com.macteval.dto.WeChatBindRequest;
import com.macteval.dto.WeChatBindResponse;
import com.macteval.model.entity.UserEntity;
import com.macteval.repository.UserRepository;
import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;

/**
 * WeChat account integration endpoints.
 *
 * <p>OAuth code exchange is not implemented yet. QR-code generation and
 * binding therefore return HTTP 501 instead of creating mock open IDs.
 */
@Slf4j
@RestController
@RequestMapping("/wechat")
@RequiredArgsConstructor
public class WeChatController {

    private static final String WECHAT_UNAVAILABLE =
            "微信授权尚未接入，当前不会生成或保存模拟 openId";

    private final UserRepository userRepository;

    @GetMapping("/qr-code")
    public ResponseEntity<ApiResponse<Map<String, String>>> getQrCode() {
        return ApiResponse.error(501, WECHAT_UNAVAILABLE);
    }

    @PostMapping("/bind")
    public ResponseEntity<ApiResponse<WeChatBindResponse>> bind(
            @Valid @RequestBody WeChatBindRequest request,
            Authentication authentication) {
        return ApiResponse.error(501, WECHAT_UNAVAILABLE);
    }

    @PostMapping("/unbind")
    public ResponseEntity<ApiResponse<String>> unbind(Authentication authentication) {
        if (authentication == null || !authentication.isAuthenticated()) {
            return ApiResponse.error(401, "未认证");
        }

        UserEntity user = userRepository.findById(authentication.getName()).orElse(null);
        if (user == null) {
            return ApiResponse.error(404, "用户不存在");
        }

        user.setWechatOpenId(null);
        user.setWechatBound(false);
        userRepository.save(user);
        log.info("User {} unbound WeChat", user.getId());
        return ApiResponse.ok("解绑成功");
    }
}
