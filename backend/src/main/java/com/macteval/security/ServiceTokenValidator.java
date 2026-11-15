package com.macteval.security;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.security.access.AccessDeniedException;
import org.springframework.stereotype.Component;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

/**
 * 服务间令牌校验 — 供 ai-service 等内部服务回调受保护端点时使用。
 */
@Component
public class ServiceTokenValidator {

    private static final String DEV_DEFAULT = "mact-eval-dev-token";

    private final String token;

    public ServiceTokenValidator(@Value("${mact.service-token}") String token,
                                 @Value("${spring.profiles.active:}") String activeProfiles) {
        if (token == null || token.isBlank()) {
            throw new IllegalStateException(
                    "mact.service-token is blank. Set a strong token via MACT_SERVICE_TOKEN.");
        }
        boolean prod = activeProfiles != null && java.util.Arrays.stream(activeProfiles.split(","))
                .map(String::trim)
                .anyMatch(profile -> "prod".equalsIgnoreCase(profile));
        if (prod && DEV_DEFAULT.equals(token)) {
            throw new IllegalStateException(
                    "mact.service-token is still the development default in prod. "
                            + "Set MACT_SERVICE_TOKEN to a strong random value.");
        }
        this.token = token;
    }

    public void validate(String provided) {
        if (token == null || token.isBlank() || provided == null
                || !constantTimeEquals(token, provided)) {
            throw new AccessDeniedException("无效的服务令牌");
        }
    }

    private static boolean constantTimeEquals(String a, String b) {
        return MessageDigest.isEqual(
                a.getBytes(StandardCharsets.UTF_8),
                b.getBytes(StandardCharsets.UTF_8));
    }
}
