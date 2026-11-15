package com.macteval.security;

import org.junit.jupiter.api.Test;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.ValueOperations;

import java.time.Duration;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * SEC-02 回归：一次性 WebSocket 票据。
 *
 * <p>每组断言对应一种真实的攻击或故障，而不是"方法被调用了"：
 * 重放、伪造、跨课堂、Redis 不可用。最后一条尤其重要——这里是**拒绝**而不是
 * 放行，与登录节流的方向相反，必须被固定下来，否则将来有人"统一一下错误处理"
 * 就会把它改成 fail-open。</p>
 */
class WsTicketStoreTest {

    @SuppressWarnings("unchecked")
    private static ValueOperations<String, String> ops(StringRedisTemplate redis) {
        return (ValueOperations<String, String>) redis.opsForValue();
    }

    @Test
    void issuedTicketIsConsumedExactlyOnce() {
        StringRedisTemplate redis = mock(StringRedisTemplate.class);
        ValueOperations<String, String> valueOps = mock(ValueOperations.class);
        when(redis.opsForValue()).thenReturn(valueOps);

        WsTicketStore store = new WsTicketStore(redis);
        String ticket = store.issue("user-1", "teacher", "lesson-1");

        assertEquals(32, ticket.length());
        assertTrue(WsTicketStore.isWellFormed(ticket));
        // 票据必须是随机的：两次签发不能相同。
        assertFalse(ticket.equals(store.issue("user-1", "teacher", "lesson-1")));

        // TTL 必须真的传下去（60 秒），否则 Redis 里的票据会按默认永不过期。
        verify(valueOps).set(eq("auth:ws-ticket:" + ticket),
                eq("user-1\u0001teacher\u0001lesson-1"), eq(Duration.ofSeconds(60)));

        // 第一次消费成功
        when(valueOps.getAndDelete("auth:ws-ticket:" + ticket))
                .thenReturn("user-1\u0001teacher\u0001lesson-1");
        WsTicketStore.Ticket consumed = store.consume(ticket);
        assertNotNull(consumed);
        assertEquals("user-1", consumed.userId());
        assertEquals("teacher", consumed.role());
        assertEquals("lesson-1", consumed.lessonSessionId());

        // 第二次：Redis 里已不存在，必须返回 null（重放）
        when(valueOps.getAndDelete("auth:ws-ticket:" + ticket)).thenReturn(null);
        assertNull(store.consume(ticket));
    }

    @Test
    void malformedTicketNeverReachesRedis() {
        StringRedisTemplate redis = mock(StringRedisTemplate.class);
        ValueOperations<String, String> valueOps = mock(ValueOperations.class);
        when(redis.opsForValue()).thenReturn(valueOps);
        WsTicketStore store = new WsTicketStore(redis);

        // 非 32 位十六进制一律挡在本地，不产生一次 Redis 查询：
        // 否则任意输入都能被当作 key 前缀去扫 Redis，既是放大攻击面，
        // 也把日志注入口留在前缀拼接处。
        assertNull(store.consume(null));
        assertNull(store.consume(""));
        assertNull(store.consume("not-a-ticket"));
        assertNull(store.consume("0".repeat(31)));
        assertNull(store.consume("0".repeat(33)));
        assertNull(store.consume("Z".repeat(32)));
        assertNull(store.consume("a".repeat(31) + "\n"));

        verify(valueOps, never()).getAndDelete(anyString());
    }

    @Test
    void redisFailureRejectsInsteadOfAllowingAnonymousHandshake() {
        StringRedisTemplate redis = mock(StringRedisTemplate.class);
        ValueOperations<String, String> valueOps = mock(ValueOperations.class);
        when(redis.opsForValue()).thenReturn(valueOps);
        when(valueOps.getAndDelete(anyString())).thenThrow(new RuntimeException("redis down"));

        WsTicketStore store = new WsTicketStore(redis);

        // fail-closed：Redis 挂了就拒绝握手，而不是放行一个无凭证连接。
        // 若这里变成返回非 null 或跳过校验，拿到任意 32 位字符串的人都能读课堂实时数据。
        assertNull(store.consume("a".repeat(32)));
    }

    @Test
    void poisonedPayloadIsRejectedRatherThanPartiallyParsed() {
        StringRedisTemplate redis = mock(StringRedisTemplate.class);
        ValueOperations<String, String> valueOps = mock(ValueOperations.class);
        when(redis.opsForValue()).thenReturn(valueOps);
        WsTicketStore store = new WsTicketStore(redis);

        String ticket = "b".repeat(32);
        // 少一个字段：若实现只按 split 取前两段，就会"猜出"一个 userId 然后放行。
        when(valueOps.getAndDelete("auth:ws-ticket:" + ticket)).thenReturn("user-1\u0001teacher");
        assertNull(store.consume(ticket));

        // 多一个字段：同理，不能被静默忽略。
        when(valueOps.getAndDelete("auth:ws-ticket:" + ticket))
                .thenReturn("user-1\u0001teacher\u0001lesson-1\u0001extra");
        assertNull(store.consume(ticket));
    }

    @Test
    void wellFormedCheckAcceptsOnlyLowercaseHex32() {
        assertTrue(WsTicketStore.isWellFormed("0123456789abcdef0123456789abcdef"));
        assertFalse(WsTicketStore.isWellFormed("0123456789ABCDEF0123456789ABCDEF"));
        assertFalse(WsTicketStore.isWellFormed(null));
        assertFalse(WsTicketStore.isWellFormed(""));
    }
}
