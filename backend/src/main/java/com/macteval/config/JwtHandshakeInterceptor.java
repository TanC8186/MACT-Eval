package com.macteval.config;

import com.macteval.security.JwtTokenProvider;
import com.macteval.security.WsTicketStore;
import jakarta.servlet.http.HttpServletRequest;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.http.server.ServerHttpRequest;
import org.springframework.http.server.ServerHttpResponse;
import org.springframework.http.server.ServletServerHttpRequest;
import org.springframework.stereotype.Component;
import org.springframework.web.socket.WebSocketHandler;
import org.springframework.web.socket.server.HandshakeInterceptor;

import java.net.URI;
import java.util.Map;

/**
 * WebSocket 握手鉴权。
 *
 * <p><b>认证凭据（SEC-02，2026-10-28）</b>：首选一次性票据
 * {@code ?ticket=<32 hex>}（见 {@link WsTicketStore}），票据在握手时被原子消费，
 * 60 秒过期。旧写法 {@code ?token=<JWT>} 已改为**默认关闭**——它会把 24 小时
 * 有效的 access JWT 明文送进 nginx 访问日志。</p>
 *
 * <p>仍然支持 {@code Authorization: Bearer} 头与旧查询串，但后者的可用性由
 * {@code mact.ws.legacy-token-enabled} 控制（默认 false），且每次使用都会打
 * WARN 日志。设成 true 只是为了给外部脚本一个明确的迁移窗口；到期后这个分支
 * 应整体删除，而不是长期保留。</p>
 *
 * <p>无论走哪条路径，最终放进 session attributes 的都是同三样：
 * {@code userId}、{@code role}、{@code lessonSessionId}。
 * {@code ClassroomWebSocketHandler} 仍会再做一次课堂级属主/班级校验——
 * 握手只证明"你是谁 + 你申请连哪个课堂"，不能替代对象级授权。</p>
 */
@Slf4j
@Component
public class JwtHandshakeInterceptor implements HandshakeInterceptor {

    private final JwtTokenProvider tokenProvider;
    private final WsTicketStore ticketStore;
    private final boolean legacyTokenEnabled;

    public JwtHandshakeInterceptor(
            JwtTokenProvider tokenProvider,
            WsTicketStore ticketStore,
            @Value("${mact.ws.legacy-token-enabled:false}") boolean legacyTokenEnabled) {
        this.tokenProvider = tokenProvider;
        this.ticketStore = ticketStore;
        this.legacyTokenEnabled = legacyTokenEnabled;
    }

    @Override
    public boolean beforeHandshake(
            ServerHttpRequest request,
            ServerHttpResponse response,
            WebSocketHandler wsHandler,
            Map<String, Object> attributes) {
        String lessonId = queryParam(request, "lessonId");
        if (lessonId == null || lessonId.isBlank()) {
            response.setStatusCode(HttpStatus.BAD_REQUEST);
            return false;
        }

        String ticket = queryParam(request, "ticket");
        if (ticket != null && !ticket.isBlank()) {
            WsTicketStore.Ticket consumed = ticketStore.consume(ticket);
            if (consumed == null) {
                // 过期、已用、伪造、或 Redis 不可用——一律 401，不区分原因：
                // 区分会告诉攻击者"这个票据存在但过期了"，而它的唯一合法用途
                // 就是一个连接。
                response.setStatusCode(HttpStatus.UNAUTHORIZED);
                return false;
            }
            // 票据绑定了目标课堂。若 URL 里的 lessonId 与票据不一致，
            // 说明有人拿 A 课堂的票据去连 B 课堂——直接拒绝，而不是
            // 让 handler 事后拒绝（那时连接已建立、日志已记录）。
            if (!lessonId.equals(consumed.lessonSessionId())) {
                response.setStatusCode(HttpStatus.FORBIDDEN);
                return false;
            }
            attributes.put("userId", consumed.userId());
            attributes.put("role", consumed.role());
            return true;
        }

        String token = legacyOrHeaderToken(request);
        if (token == null || !tokenProvider.validateToken(token, JwtTokenProvider.TOKEN_TYPE_ACCESS)) {
            response.setStatusCode(HttpStatus.UNAUTHORIZED);
            return false;
        }
        if (tokenProvider.isPasswordChangeRequired(token)) {
            // 初始密码账号在改密前不能通过 WebSocket 旁路读取课堂数据。
            response.setStatusCode(HttpStatus.FORBIDDEN);
            return false;
        }
        attributes.put("userId", tokenProvider.getUserIdFromToken(token));
        attributes.put("role", tokenProvider.getRoleFromToken(token));
        return true;
    }

    @Override
    public void afterHandshake(
            ServerHttpRequest request,
            ServerHttpResponse response,
            WebSocketHandler wsHandler,
            Exception exception) {
        // no-op
    }

    /**
     * 旧凭据来源：{@code Authorization} 头（始终允许，不落访问日志）与
     * {@code ?token=}（只在显式开启迁移窗口时允许）。
     */
    private String legacyOrHeaderToken(ServerHttpRequest request) {
        if (!(request instanceof ServletServerHttpRequest servletRequest)) {
            return null;
        }
        HttpServletRequest http = servletRequest.getServletRequest();
        String auth = http.getHeader("Authorization");
        if (auth != null && auth.startsWith("Bearer ")) {
            return auth.substring(7);
        }
        if (!legacyTokenEnabled) {
            return null;
        }
        String queryToken = http.getParameter("token");
        if (queryToken != null && !queryToken.isBlank()) {
            // 每次使用都留痕：迁移窗口到期后应有的日志是零。
            // 不打印令牌本身——那样等于把要消除的泄漏再复制一份到应用日志。
            log.warn("WS handshake used the deprecated ?token= query parameter; "
                    + "switch to POST /auth/ws-ticket. Legacy support can be removed once "
                    + "this line stops appearing.");
            return queryToken;
        }
        return null;
    }

    /** 从原始 URI 取查询参数，不依赖 servlet 的参数解析（握手请求可能未解析）。 */
    private static String queryParam(ServerHttpRequest request, String name) {
        URI uri = request.getURI();
        String query = uri == null ? null : uri.getRawQuery();
        if (query == null || query.isEmpty()) {
            return null;
        }
        for (String pair : query.split("&")) {
            int eq = pair.indexOf('=');
            String key = eq < 0 ? pair : pair.substring(0, eq);
            if (!name.equals(key)) {
                continue;
            }
            String raw = eq < 0 ? "" : pair.substring(eq + 1);
            return java.net.URLDecoder.decode(raw, java.nio.charset.StandardCharsets.UTF_8);
        }
        return null;
    }
}
