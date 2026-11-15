package com.macteval.security;

import io.jsonwebtoken.Jwts;
import io.jsonwebtoken.security.Keys;
import io.jsonwebtoken.security.SignatureException;
import org.junit.jupiter.api.Test;
import org.springframework.test.util.ReflectionTestUtils;

import javax.crypto.SecretKey;
import java.nio.charset.StandardCharsets;
import java.time.Instant;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 双密钥 JWT 轮换的回归测试。
 *
 * <p>每条断言对应轮换里一个真实会出事的环节：旧键不能立刻失效（否则在线用户全掉线）、
 * 新签发必须由第一个键签（否则事故中无法回答"谁签的"）、退役到点必须拒绝启动
 * （否则"忘删旧键"永远隐形，而旧键留着就是伪造窗口）。
 *
 * <p>不启 Spring 上下文：{@code @Value} 注入的四个字段在这里直接用
 * {@link ReflectionTestUtils} 填入，与 {@code CorsOriginConfigTest} 的做法一致，
 * 这样断言的是 {@code init()} 的判定逻辑本身，不是 Spring 的属性绑定。</p>
 */
class JwtTokenProviderMultiKeyTest {

    private static final String K1 = "k1-0123456789abcdef0123456789abcdef-key";
    private static final String K2 = "k2-0123456789abcdef0123456789abcdef-key";
    private static final String K3 = "k3-0123456789abcdef0123456789abcdef-key";
    private static final String DEFAULT_SECRET = "your-secret-key-at-least-256-bits-long-change-in-production";

    private static JwtTokenProvider provider(String singleSecret, String multiSecrets, String retiredAt) {
        JwtTokenProvider provider = new JwtTokenProvider();
        ReflectionTestUtils.setField(provider, "jwtSecret", singleSecret);
        ReflectionTestUtils.setField(provider, "jwtSecrets", multiSecrets);
        ReflectionTestUtils.setField(provider, "jwtRetiredAt", retiredAt);
        ReflectionTestUtils.setField(provider, "jwtExpirationMs", 86_400_000L);
        ReflectionTestUtils.setField(provider, "refreshExpirationMs", 604_800_000L);
        provider.init();
        return provider;
    }

    private static SecretKey key(String secret) {
        return Keys.hmacShaKeyFor(secret.getBytes(StandardCharsets.UTF_8));
    }

    /** 用指定密钥单独验证，命中即说明"这个 token 确实是这把钥匙签的"。 */
    private static boolean verifiesUnder(String token, String secret) {
        try {
            Jwts.parser().verifyWith(key(secret)).build().parseSignedClaims(token);
            return true;
        } catch (SignatureException e) {
            return false;
        }
    }

    @Test
    void tokenSignedBySecondKeyVerifies() {
        JwtTokenProvider provider = provider(null, K1 + "," + K2, "");
        // 用非首位的键手工签一个 token，模拟"轮换前签发的旧 token"。
        String legacy = Jwts.builder()
                .subject("user-legacy")
                .claim("token_type", "access")
                .signWith(key(K2))
                .compact();

        assertTrue(provider.validateToken(legacy, JwtTokenProvider.TOKEN_TYPE_ACCESS));
        assertEquals("user-legacy", provider.getUserIdFromToken(legacy));
    }

    @Test
    void oldKeyStillVerifiesAfterDemotionToSecondPosition() {
        // T0 阶段：只有 k1。
        String tokenFromK1 = provider(null, K1, "").generateAccessToken("user-1", "teacher");
        // T0 之后：k2 插到首位，k1 退居第二——旧 token 必须继续有效，否则在线用户掉线。
        JwtTokenProvider rotated = provider(null, K2 + "," + K1, "");

        assertTrue(rotated.validateToken(tokenFromK1, JwtTokenProvider.TOKEN_TYPE_ACCESS));
        assertEquals("user-1", rotated.getUserIdFromToken(tokenFromK1));
    }

    @Test
    void newTokensAreSignedByFirstKeyOnly() {
        JwtTokenProvider provider = provider(null, K1 + "," + K2, "");
        String accessToken = provider.generateAccessToken("user-1", "teacher");
        String refreshToken = provider.generateRefreshToken("user-1");

        // 签发必须确定：只认第一把钥匙，才能回答"这个 token 是谁签的"。
        assertTrue(verifiesUnder(accessToken, K1), "access token 必须由首位密钥签发");
        assertFalse(verifiesUnder(accessToken, K2), "access token 不应由备用密钥签发");
        assertTrue(verifiesUnder(refreshToken, K1), "refresh token 必须由首位密钥签发");
        assertFalse(verifiesUnder(refreshToken, K2), "refresh token 不应由备用密钥签发");
    }

    @Test
    void expiredTokenSignedBySecondKeyIsStillRejected() {
        JwtTokenProvider provider = provider(null, K1 + "," + K2, "");
        String expiredByK2 = Jwts.builder()
                .subject("user-legacy")
                .claim("token_type", "access")
                .issuedAt(new java.util.Date(System.currentTimeMillis() - 7_200_000L))
                .expiration(new java.util.Date(System.currentTimeMillis() - 3_600_000L))
                .signWith(key(K2))
                .compact();

        // 第二个键"签名对但已过期"时，多键循环必须把到期原样抛出，不能被当成
        // "这把钥匙不匹配"继续试下一把——否则 401 会被误读成签名问题。
        assertThrows(io.jsonwebtoken.ExpiredJwtException.class, () -> provider.getUserIdFromToken(expiredByK2));
        assertFalse(provider.validateToken(expiredByK2, JwtTokenProvider.TOKEN_TYPE_ACCESS));
    }

    @Test
    void expiredTokenSignedByUnknownKeyIsRejectedBySignatureNotExpiry() {
        JwtTokenProvider provider = provider(null, K1 + "," + K2, "");
        // "已过期 + 密钥不在列表里"：如果哪天有人把到期检查挪到验签之前，
        // 这个 token 会以"过期"为由被拒，看着像正常 401，实则签名从未被验证过——
        // 攻击者只要构造过期 token 就能探到一条绕过验签的旁路。
        String expiredForged = Jwts.builder()
                .subject("attacker")
                .claim("token_type", "access")
                .issuedAt(new java.util.Date(System.currentTimeMillis() - 7_200_000L))
                .expiration(new java.util.Date(System.currentTimeMillis() - 3_600_000L))
                .signWith(key(K3))
                .compact();

        assertFalse(provider.validateToken(expiredForged, JwtTokenProvider.TOKEN_TYPE_ACCESS));
        // 必须死在签名上，而不是死在到期上。
        org.junit.jupiter.api.Assertions.assertThrows(SignatureException.class,
                () -> provider.getUserIdFromToken(expiredForged));
    }

    @Test
    void tokenSignedByUnknownKeyIsRejected() {
        JwtTokenProvider provider = provider(null, K1 + "," + K2, "");
        String forged = Jwts.builder()
                .subject("user-1")
                .claim("token_type", "access")
                .signWith(key(K3))
                .compact();

        // 列表外的键签的任何东西都必须拒绝；否则"任一成功即通过"会退化成"任意成功"。
        assertFalse(provider.validateToken(forged, JwtTokenProvider.TOKEN_TYPE_ACCESS));
    }

    @Test
    void retiredAtInThePastRefusesStartup() {
        String past = Instant.now().minusSeconds(3600).toString();
        IllegalStateException error = assertThrows(IllegalStateException.class,
                () -> provider(null, K1 + "," + K2, past));

        String message = error.getMessage();
        // 提示必须直接给出动作：删除旧密钥。含糊的"配置有误"会让人去改时间戳而不是删键。
        assertTrue(message.contains("Delete the old key"), "消息应点名删除旧密钥，实际: " + message);
        assertTrue(message.contains("mact.jwt.retired-at"), "消息应点名 retired-at，实际: " + message);

        // 只剩一把钥匙时仍然拒绝启动，但不能让人去找"要删的旧键"——那时真正没做的是清空时间戳。
        IllegalStateException singleKey = assertThrows(IllegalStateException.class,
                () -> provider(K1, "", past));
        assertTrue(singleKey.getMessage().contains("clear"), singleKey.getMessage());
        assertTrue(singleKey.getMessage().contains("Only one key is configured"), singleKey.getMessage());
    }

    @Test
    void retiredAtInTheFutureStillStarts() {
        String future = Instant.now().plusSeconds(86_400).toString();
        // 未到点必须能启动，否则轮换窗口内的重启会把全部节点挡在门外。
        JwtTokenProvider provider = provider(null, K1 + "," + K2, future);
        assertTrue(provider.validateToken(
                provider.generateAccessToken("user-1", "teacher"), JwtTokenProvider.TOKEN_TYPE_ACCESS));
    }

    @Test
    void singleSecretFallbackStillWorks() {
        // 没配 mact.jwt.secrets 时回退到 mact.jwt.secret：已上线环境不改配置也必须能跑。
        JwtTokenProvider provider = provider(K1, "", "");
        String token = provider.generateAccessToken("user-1", "teacher");

        assertTrue(provider.validateToken(token, JwtTokenProvider.TOKEN_TYPE_ACCESS));
        assertEquals("teacher", provider.getRoleFromToken(token));
        assertTrue(verifiesUnder(token, K1));
    }

    @Test
    void defaultSecretGuardStillFires() {
        IllegalStateException single = assertThrows(IllegalStateException.class,
                () -> provider(DEFAULT_SECRET, "", ""));
        assertTrue(single.getMessage().contains("built-in default"), single.getMessage());
        assertTrue(single.getMessage().contains("MACT_JWT_SECRET"), single.getMessage());

        // 多密钥模式里混入内置默认值同样要挡：换了个属性名不等于换了钥匙。
        IllegalStateException multi = assertThrows(IllegalStateException.class,
                () -> provider(null, K1 + "," + DEFAULT_SECRET, ""));
        assertTrue(multi.getMessage().contains("built-in default"), multi.getMessage());
        assertTrue(multi.getMessage().contains("mact.jwt.secrets[2]"), multi.getMessage());
    }

    @Test
    void shortSecretGuardStillFires() {
        String shortSecret = "too-short";
        IllegalStateException single = assertThrows(IllegalStateException.class,
                () -> provider(shortSecret, "", ""));
        assertTrue(single.getMessage().contains("at least 32 bytes"), single.getMessage());
        assertTrue(single.getMessage().contains("MACT_JWT_SECRET"), single.getMessage());
        assertTrue(single.getMessage().contains("current length: " + shortSecret.length()), single.getMessage());

        IllegalStateException multi = assertThrows(IllegalStateException.class,
                () -> provider(null, K1 + "," + shortSecret, ""));
        assertTrue(multi.getMessage().contains("at least 32 bytes"), multi.getMessage());
    }

    @Test
    void emptyEntryInSecretListIsRejected() {
        // "k1," 这种手误若被静默吞掉，运维会以为旧键还挂着，实际列表里只剩一个键，
        // 轮换期的"旧 token 还能用"就成了假象。
        IllegalStateException error = assertThrows(IllegalStateException.class,
                () -> provider(null, K1 + ",", ""));
        assertTrue(error.getMessage().contains("empty entry"), error.getMessage());
    }

    @Test
    void malformedRetiredAtIsRejected() {
        IllegalStateException error = assertThrows(IllegalStateException.class,
                () -> provider(null, K1 + "," + K2, "next-week"));
        assertTrue(error.getMessage().contains("ISO-8601"), error.getMessage());

        // 不带时区的写法也必须拒绝：按 UTC 猜偏差可达 14 小时，而这道闸门挡的
        // 正是"旧键多活一段"，猜错方向就是放行。
        IllegalStateException noOffset = assertThrows(IllegalStateException.class,
                () -> provider(null, K1 + "," + K2, "2026-11-06T00:00:00"));
        assertTrue(noOffset.getMessage().contains("explicit offset"), noOffset.getMessage());
    }

    @Test
    void retiredAtWithOffsetIsAccepted() {
        // +08:00 形式必须能用，否则国内运维按本地时间写退役时刻会被拒。
        String future = java.time.OffsetDateTime.now(java.time.ZoneOffset.ofHours(8))
                .plusDays(8).toString();
        JwtTokenProvider provider = provider(null, K1 + "," + K2, future);
        assertTrue(provider.validateToken(
                provider.generateAccessToken("user-1", "teacher"), JwtTokenProvider.TOKEN_TYPE_ACCESS));
    }
}
