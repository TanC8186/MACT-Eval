package com.macteval.security;

import com.macteval.exception.BusinessException;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Component;

import java.time.Duration;
import java.util.Locale;

/**
 * 登录失败节流：同一账号 15 分钟内连续失败 5 次后暂时拒绝登录。
 *
 * <p>Redis 不可用时选择“可用性优先”：记录/查询失败只记日志，不因节流
 * 基础设施故障锁死所有用户；登录本身仍受密码校验保护。</p>
 */
@Slf4j
@Component
@RequiredArgsConstructor
public class LoginThrottleService {

    static final int MAX_FAILURES = 5;
    static final Duration WINDOW = Duration.ofMinutes(15);
    private static final String KEY_PREFIX = "auth:login:fail:";

    private final StringRedisTemplate redisTemplate;

    public void assertAllowed(String username) {
        try {
            String value = redisTemplate.opsForValue().get(key(username));
            if (value != null && Integer.parseInt(value) >= MAX_FAILURES) {
                throw new BusinessException(429, "登录失败次数过多，请 15 分钟后重试");
            }
        } catch (BusinessException e) {
            throw e;
        } catch (Exception e) {
            log.warn("Login throttle lookup failed; allowing attempt: {}", e.getMessage());
        }
    }

    public void recordFailure(String username) {
        try {
            String key = key(username);
            Long count = redisTemplate.opsForValue().increment(key);
            if (count != null && count == 1L) {
                redisTemplate.expire(key, WINDOW);
            }
        } catch (Exception e) {
            log.warn("Login throttle increment failed: {}", e.getMessage());
        }
    }

    public void recordSuccess(String username) {
        try {
            redisTemplate.delete(key(username));
        } catch (Exception e) {
            log.warn("Login throttle reset failed: {}", e.getMessage());
        }
    }

    private String key(String username) {
        return KEY_PREFIX + (username == null ? "" : username.trim().toLowerCase(Locale.ROOT));
    }
}
