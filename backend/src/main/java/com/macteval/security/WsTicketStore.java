package com.macteval.security;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Component;

import java.time.Duration;
import java.util.UUID;
import java.util.regex.Pattern;

/**
 * WebSocket 一次性票据（SEC-02，2026-10-28）。
 *
 * <p><b>要解决的问题</b>：浏览器只能给 WebSocket 传 URL 与子协议，不能设
 * Authorization 头。于是旧实现把 access JWT 放进查询串
 * （{@code /ws/classroom?lessonId=..&token=<JWT>}），而 nginx 默认访问日志记录
 * 完整 {@code $request}（含 query string）——24 小时有效的教师凭据就这样被写进
 * {@code docker compose logs teacher-web}，任何能读日志的人都能冒充该教师。</p>
 *
 * <p><b>方案</b>：票据是**另一个东西**，不是 JWT 的变体。它没有签名、没有声明，
 * 只是 Redis 里一条 60 秒的随机字符串 → (userId, role, lessonSessionId) 的映射。
 * 握手时原子取走（{@code getAndDelete}），重复使用必然失败。即使票据被写进
 * 访问日志，攻击者拿到的也只是已消费凭证：它 60 秒就过期，且只能开一个连接。</p>
 *
 * <p><b>为什么不直接用短 TTL 的 JWT</b>：JWT 是无状态的，无法"单次消费"。
 * 要么接受重放窗口，要么在 Redis 里再记一份已用 jti——那就已经是票据了，
 * 只是多了签名与解析开销。</p>
 */
@Slf4j
@Component
@RequiredArgsConstructor
public class WsTicketStore {

    /** 票据有效期。60 秒足够"签发 → 建连"，且远短于任何日志轮转周期。 */
    public static final Duration TICKET_TTL = Duration.ofSeconds(60);

    private static final String KEY_PREFIX = "auth:ws-ticket:";

    /**
     * 票据格式：32 个十六进制字符（UUID 去掉连字符）。
     * 严格校验格式不只是"好看"——握手入口要把用户提供的字符串拼进 Redis key，
     * 若不校验，任意字符串（含换行、空格、超长串）都会变成一次 Redis 查询，
     * 既浪费也给了日志注入口。
     */
    private static final Pattern TICKET_PATTERN = Pattern.compile("^[0-9a-f]{32}$");

    private final StringRedisTemplate redisTemplate;

    /** 票据绑定的身份与目标课堂；握手后放进 WebSocket session attributes。 */
    public record Ticket(String userId, String role, String lessonSessionId) { }

    /**
     * 为一个 (用户, 角色, 课堂) 组合签发票据。
     *
     * <p>课堂 ID 必须绑定：否则攻击者可以用自己的票据连别人的课堂，把
     * "认证"与"授权"拆成两步，而 {@code canAccessLesson} 只在连接建立后
     * 才拒绝——那之前连接已经建立，握手日志与前端状态都已污染。</p>
     */
    public String issue(String userId, String role, String lessonSessionId) {
        String ticket = UUID.randomUUID().toString().replace("-", "");
        // 值用 \u0001 分隔而非 JSON：这里只有三个短字符串，JSON 序列化会引入
        // 反序列化攻击面（哪怕值是我们自己写的），而分隔符方案没有解析器。
        String value = userId + '\u0001' + role + '\u0001' + lessonSessionId;
        redisTemplate.opsForValue().set(KEY_PREFIX + ticket, value, TICKET_TTL);
        return ticket;
    }

    /**
     * 原子消费票据。返回 {@code null} 表示票据无效/已用/过期。
     *
     * <p>{@code getAndDelete} 在 Redis 6.2+ 是单条原子命令（GETDEL），
     * 因此并发拿同一个票据只可能有一个调用方拿到值。用 get + delete 两步
     * 会让两个并发握手都读到值，然后都成功——那样"一次性"就只是说说。</p>
     */
    public Ticket consume(String ticket) {
        if (!isWellFormed(ticket)) {
            return null;
        }
        String value;
        try {
            value = redisTemplate.opsForValue().getAndDelete(KEY_PREFIX + ticket);
        } catch (Exception e) {
            // Redis 不可用时**拒绝**而不是放行：这是与登录节流相反的取舍。
            // 登录节流放行是因为"拒绝"等于让所有人无法登录（可用性更重要的
            // 已认证入口）；这里放行等于允许无票据连接课堂实时数据（安全性
            // 更重要的未认证入口）。方向不同的两个决定，不是不一致。
            log.warn("WS ticket lookup failed; rejecting handshake: {}", e.getMessage());
            return null;
        }
        if (value == null) {
            return null;
        }
        String[] parts = value.split("\u0001", -1);
        if (parts.length != 3) {
            // 值被外部写坏了（Redis 被手工改过/版本迁移）。宁可拒绝，
            // 也不要猜出部分字段后继续。
            log.warn("Malformed WS ticket payload; rejecting handshake");
            return null;
        }
        return new Ticket(parts[0], parts[1], parts[2]);
    }

    /** 票据是否形如我们签发的样子。握手入口先过这一关再查 Redis。 */
    public static boolean isWellFormed(String ticket) {
        return ticket != null && TICKET_PATTERN.matcher(ticket).matches();
    }
}
