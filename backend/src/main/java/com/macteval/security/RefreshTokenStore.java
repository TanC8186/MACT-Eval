package com.macteval.security;

import lombok.RequiredArgsConstructor;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Component;

import java.time.Duration;
import java.util.Set;

/**
 * Server-side store for refresh token rotation and reuse detection.
 * A refresh token is only accepted while its jti is present in Redis.
 * consume() atomically removes the entry, so a replayed token is rejected.
 *
 * <p>每个用户的 jti 同时记录在一个集合中，改密时可一次性撤销该用户的
 * 全部 refresh token，而不是只影响当前设备。</p>
 */
@Component
@RequiredArgsConstructor
public class RefreshTokenStore {

    private static final String KEY_PREFIX = "auth:refresh:";
    private static final String USER_KEY_PREFIX = "auth:refresh:user:";

    private final StringRedisTemplate redisTemplate;

    public void store(String jti, String userId, Duration ttl) {
        if (jti == null || userId == null) {
            return;
        }
        redisTemplate.opsForValue().set(KEY_PREFIX + jti, userId, ttl);
        String userKey = USER_KEY_PREFIX + userId;
        redisTemplate.opsForSet().add(userKey, jti);
        redisTemplate.expire(userKey, ttl);
    }

    /**
     * Atomically consume the refresh token. Returns {@code true} only if the
     * token was present and valid; {@code false} for expired, already-used, or unknown tokens.
     */
    public boolean consume(String jti) {
        if (jti == null) {
            return false;
        }
        String userId = redisTemplate.opsForValue().get(KEY_PREFIX + jti);
        boolean deleted = Boolean.TRUE.equals(redisTemplate.delete(KEY_PREFIX + jti));
        if (userId != null) {
            redisTemplate.opsForSet().remove(USER_KEY_PREFIX + userId, jti);
        }
        return deleted;
    }

    /** 撤销指定用户的全部 refresh token（改密/管理员强制下线）。 */
    public void revokeAllForUser(String userId) {
        if (userId == null) return;
        String userKey = USER_KEY_PREFIX + userId;
        Set<String> jtis = redisTemplate.opsForSet().members(userKey);
        if (jtis != null) {
            for (String jti : jtis) {
                redisTemplate.delete(KEY_PREFIX + jti);
            }
        }
        redisTemplate.delete(userKey);
    }
}
