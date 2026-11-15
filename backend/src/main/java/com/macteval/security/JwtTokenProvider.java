package com.macteval.security;

import io.jsonwebtoken.*;
import io.jsonwebtoken.security.Keys;
import jakarta.annotation.PostConstruct;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import javax.crypto.SecretKey;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.UUID;

@Slf4j
@Component
public class JwtTokenProvider {

    public static final String CLAIM_TOKEN_TYPE = "token_type";
    public static final String CLAIM_ROLE = "role";
    public static final String CLAIM_PASSWORD_CHANGE_REQUIRED = "pwd_change_required";
    public static final String TOKEN_TYPE_ACCESS = "access";
    public static final String TOKEN_TYPE_REFRESH = "refresh";

    private static final String DEFAULT_SECRET = "your-secret-key-at-least-256-bits-long-change-in-production";
    private static final int MIN_SECRET_LENGTH_BYTES = 32;

    /** 兼容单密钥配置；多密钥未配置时仍走它，因此既有部署不需要改任何东西。 */
    @Value("${mact.jwt.secret}")
    private String jwtSecret;

    /**
     * 逗号分隔的密钥列表，第一个用于签发、全部用于验证。
     * 若这里留空则回退到单值 mact.jwt.secret——回退不是便利，而是升级路径：
     * 已上线环境先不改配置也能跑，轮换时再造双密钥。
     */
    @Value("${mact.jwt.secrets:}")
    private String jwtSecrets;

    /** 旧密钥的强制退役时刻（ISO-8601）。到点后启动失败，逼运维把它从列表里删掉。 */
    @Value("${mact.jwt.retired-at:}")
    private String jwtRetiredAt;

    @Value("${mact.jwt.expiration}")
    private long jwtExpirationMs;

    @Value("${mact.jwt.refresh-expiration}")
    private long refreshExpirationMs;

    /** 签发只用一个键：否则排障时无法回答"这个 token 是谁签的"。 */
    private SecretKey signingKey;

    /** 验证用全部键：轮换期间新签发的与轮换前签发的都必须放行。 */
    private List<SecretKey> verificationKeys;

    @PostConstruct
    public void init() {
        List<String> configured = resolveConfiguredSecrets();
        boolean multiKeyMode = configured.size() > 1 || !isBlank(jwtSecrets);
        List<SecretKey> keys = new ArrayList<>(configured.size());
        for (int i = 0; i < configured.size(); i++) {
            // 单密钥模式下标签必须与属性名一字不差：现有运维告警与文档都按这个字符串检索。
            String label = multiKeyMode ? "mact.jwt.secrets[" + (i + 1) + "]" : "mact.jwt.secret";
            keys.add(buildKey(configured.get(i), label, multiKeyMode));
        }
        this.signingKey = keys.get(0);
        this.verificationKeys = List.copyOf(keys);
        checkRetiredAt(keys.size());
        if (keys.size() > 1) {
            log.info("JWT signing key initialized from the first of {} configured keys; "
                    + "the remaining ones are verification-only", keys.size());
        } else {
            log.info("JWT secret validated and signing key initialized");
        }
    }

    public String generateAccessToken(String userId, String role) {
        return generateAccessToken(userId, role, false);
    }

    public String generateAccessToken(String userId, String role, boolean mustChangePassword) {
        Date now = new Date();
        return Jwts.builder()
                .subject(userId)
                .id(UUID.randomUUID().toString())
                .claim(CLAIM_ROLE, role)
                .claim(CLAIM_TOKEN_TYPE, TOKEN_TYPE_ACCESS)
                .claim(CLAIM_PASSWORD_CHANGE_REQUIRED, mustChangePassword)
                .issuedAt(now)
                .expiration(new Date(now.getTime() + jwtExpirationMs))
                .signWith(signingKey)
                .compact();
    }

    /** 初始密码账号的 access token 只允许改密/读取资料/登出。 */
    public boolean isPasswordChangeRequired(String token) {
        try {
            return Boolean.TRUE.equals(
                    getClaims(token).get(CLAIM_PASSWORD_CHANGE_REQUIRED, Boolean.class));
        } catch (JwtException | IllegalArgumentException e) {
            return false;
        }
    }

    public String generateRefreshToken(String userId) {
        Date now = new Date();
        return Jwts.builder()
                .subject(userId)
                .id(UUID.randomUUID().toString())
                .claim(CLAIM_TOKEN_TYPE, TOKEN_TYPE_REFRESH)
                .issuedAt(now)
                .expiration(new Date(now.getTime() + refreshExpirationMs))
                .signWith(signingKey)
                .compact();
    }

    public String getUserIdFromToken(String token) {
        return getClaims(token).getSubject();
    }

    public String getRoleFromToken(String token) {
        return getClaims(token).get(CLAIM_ROLE, String.class);
    }

    public String getJtiFromToken(String token) {
        return getClaims(token).getId();
    }

    public boolean isRefreshToken(String token) {
        try {
            return TOKEN_TYPE_REFRESH.equals(getClaims(token).get(CLAIM_TOKEN_TYPE, String.class));
        } catch (JwtException | IllegalArgumentException e) {
            return false;
        }
    }

    public boolean validateToken(String token, String expectedType) {
        try {
            Claims claims = getClaims(token);
            return expectedType.equals(claims.get(CLAIM_TOKEN_TYPE, String.class));
        } catch (JwtException | IllegalArgumentException e) {
            // 只记异常摘要，绝不记 token 本体或 payload：手上有日志的人不该因此拿到可重放的凭据。
            log.warn("Invalid JWT token: {}", e.getMessage());
            return false;
        }
    }

    /**
     * 逐个密钥试解析，任一成功即通过。
     *
     * <p>到期异常必须立刻上报而不是继续换密钥重试：签名验过之后才轮到检查 exp，
     * 若在失败分支里把它吞掉，一个"用旧键签发且已过期"的 token 会被当成签名不匹配，
     * 于是 401 的原因从"过期请重新登录"变成"签名错误"，排障方向整个偏掉。</p>
     */
    private Claims getClaims(String token) {
        JwtException lastFailure = null;
        for (SecretKey candidate : verificationKeys) {
            try {
                return Jwts.parser()
                        .verifyWith(candidate)
                        .build()
                        .parseSignedClaims(token)
                        .getPayload();
            } catch (ExpiredJwtException e) {
                throw e;
            } catch (JwtException e) {
                lastFailure = e;
            }
        }
        // verificationKeys 构造期非空且不可变，走到这里必有一次失败原因。
        throw lastFailure;
    }

    /**
     * 解析待用密钥列表：优先 mact.jwt.secrets，留空则回退到单值 mact.jwt.secret。
     */
    private List<String> resolveConfiguredSecrets() {
        if (isBlank(jwtSecrets)) {
            return List.of(jwtSecret == null ? "" : jwtSecret);
        }
        // limit=-1 保留末尾空项，否则 "k1," 这种手误会被静默吞掉。
        String[] parts = jwtSecrets.split(",", -1);
        List<String> secrets = new ArrayList<>(parts.length);
        for (int i = 0; i < parts.length; i++) {
            String candidate = parts[i].trim();
            if (candidate.isEmpty()) {
                // 空项一律判死：吞掉它意味着运维以为自己配了两个键，实际只生效一个，
                // 而轮换期"旧键还在"这件事正是靠列表内容来证明的。
                throw new IllegalStateException(
                        "mact.jwt.secrets contains an empty entry at position " + (i + 1)
                                + ". Remove the stray comma, or provide every secret via the "
                                + "MACT_JWT_SECRETS (comma-separated) / MACT_JWT_SECRET environment variables.");
            }
            secrets.add(candidate);
        }
        return secrets;
    }

    /** 逐条密钥做与单密钥时代完全相同的两道体检：内置默认值、长度不足。 */
    private SecretKey buildKey(String secret, String propertyLabel, boolean multiKeyMode) {
        byte[] secretBytes = secret.getBytes(StandardCharsets.UTF_8);
        if (DEFAULT_SECRET.equals(secret)) {
            throw new IllegalStateException(
                    propertyLabel + " is still set to the built-in default value. "
                            + (multiKeyMode
                                    ? "Set strong, unique secrets via the MACT_JWT_SECRETS (comma-separated) "
                                            + "environment variable, or a single one via MACT_JWT_SECRET."
                                    : "Set a strong, unique secret via the MACT_JWT_SECRET environment variable."));
        }
        if (secretBytes.length < MIN_SECRET_LENGTH_BYTES) {
            throw new IllegalStateException(
                    propertyLabel + " must be at least 32 bytes (256 bits), current length: " + secretBytes.length
                            + ". Set a strong secret via the MACT_JWT_SECRET environment variable.");
        }
        return Keys.hmacShaKeyFor(secretBytes);
    }

    /**
     * retired-at 一旦落在过去就拒绝启动。
     *
     * <p>不选"到期自动忽略旧键"是因为那样"我们忘了删旧键"这件事会永远隐形，
     * 而一个还挂在验证列表里的旧键就是一个可被伪造的窗口。启动失败逼人做一次显式操作。</p>
     */
    private void checkRetiredAt(int keyCount) {
        if (isBlank(jwtRetiredAt)) {
            return;
        }
        Instant retiredAt;
        try {
            retiredAt = OffsetDateTime.parse(jwtRetiredAt.trim()).toInstant();
        } catch (DateTimeParseException e) {
            // 写错格式同样不能放行：静默忽略等于把这道闸门关掉。
            // 时区偏移必须写出来（Z 或 +08:00）：默认按 UTC 猜会让退役时刻最多偏 14 小时，
            // 而这道闸门挡的正是"旧键多活了一段"，猜错方向等于放行。
            throw new IllegalStateException(
                    "mact.jwt.retired-at is not a valid ISO-8601 instant with an explicit offset: '"
                            + jwtRetiredAt + "'. Use e.g. 2026-11-06T00:00:00Z or "
                            + "2026-11-06T08:00:00+08:00, or leave it empty to disable the deadline.",
                    e);
        }
        if (!retiredAt.isAfter(Instant.now())) {
            // 两种情况都拒绝启动，但提示必须分开：列表里只剩一个键时旧键大概已经删了，
            // 还让人"去删旧键"会让运维在配置里乱找，而真正没做的是清空 retired-at。
            String action = keyCount > 1
                    ? "Delete the old key from mact.jwt.secrets (keep only the current key in the first "
                            + "position) and clear mact.jwt.retired-at, then restart."
                    : "Only one key is configured, so the old key looks already gone; clear "
                            + "mact.jwt.retired-at and restart. If an old key is still configured, delete "
                            + "the old key as well.";
            throw new IllegalStateException(
                    "mact.jwt.retired-at (" + retiredAt + ") is already in the past, so the retired JWT key "
                            + "must no longer verify anything. " + action
                            + " Refusing to start: an unused old key stays a token-forgery window for as long "
                            + "as it is loaded, and silently ignoring a stale deadline would keep "
                            + "\"we forgot to delete it\" permanently invisible. Keys configured: "
                            + keyCount + ".");
        }
        log.warn("JWT old key is scheduled to be removed at {}; after that instant the service will refuse "
                + "to start until the old key is deleted from mact.jwt.secrets", retiredAt);
    }

    private static boolean isBlank(String value) {
        return value == null || value.isBlank();
    }
}
