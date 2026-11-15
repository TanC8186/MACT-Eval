package com.macteval.config;

import com.macteval.security.WsTicketStore;
import org.junit.jupiter.api.Test;
import org.springframework.http.HttpStatus;
import org.springframework.http.server.ServerHttpRequest;
import org.springframework.http.server.ServerHttpResponse;
import org.springframework.web.socket.WebSocketHandler;

import java.net.URI;
import java.util.HashMap;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

/**
 * SEC-02 回归：握手拦截器只认票据，且票据与课堂绑定。
 *
 * <p>这里的断言全部落在"连接是否被允许"以及"session attributes 里放了什么"，
 * 而不是"某个方法被调用了"。属性内容很重要：{@code ClassroomWebSocketHandler}
 * 之后要用它们做对象级授权，属性写错等于授权依据错。</p>
 */
class JwtHandshakeInterceptorTest {

    private static ServerHttpRequest request(String query) {
        ServerHttpRequest req = mock(ServerHttpRequest.class);
        when(req.getURI()).thenReturn(URI.create("ws://localhost/ws/classroom?" + query));
        return req;
    }

    private static final class Captured {
        final ServerHttpResponse response = mock(ServerHttpResponse.class);
        final Map<String, Object> attributes = new HashMap<>();
    }

    @Test
    void validTicketForMatchingLessonIsAccepted() {
        WsTicketStore store = mock(WsTicketStore.class);
        String ticket = "a".repeat(32);
        when(store.consume(ticket)).thenReturn(
                new WsTicketStore.Ticket("user-1", "teacher", "lesson-1"));

        JwtHandshakeInterceptor interceptor =
                new JwtHandshakeInterceptor(mock(com.macteval.security.JwtTokenProvider.class), store, false);
        Captured ctx = new Captured();

        boolean allowed = interceptor.beforeHandshake(
                request("lessonId=lesson-1&ticket=" + ticket),
                ctx.response, mock(WebSocketHandler.class), ctx.attributes);

        assertTrue(allowed);
        assertEquals("user-1", ctx.attributes.get("userId"));
        assertEquals("teacher", ctx.attributes.get("role"));
    }

    @Test
    void ticketBoundToAnotherLessonIsForbidden() {
        WsTicketStore store = mock(WsTicketStore.class);
        String ticket = "b".repeat(32);
        // 票据是给 lesson-1 签的，但请求要连 lesson-2。
        when(store.consume(ticket)).thenReturn(
                new WsTicketStore.Ticket("user-1", "teacher", "lesson-1"));

        JwtHandshakeInterceptor interceptor =
                new JwtHandshakeInterceptor(mock(com.macteval.security.JwtTokenProvider.class), store, false);
        Captured ctx = new Captured();

        boolean allowed = interceptor.beforeHandshake(
                request("lessonId=lesson-2&ticket=" + ticket),
                ctx.response, mock(WebSocketHandler.class), ctx.attributes);

        assertFalse(allowed);
        // 403 而不是 401：这是"认证通过但目标不允许"，与"凭证无效"分开，
        // 但两者都不建立连接。
        org.mockito.Mockito.verify(ctx.response).setStatusCode(HttpStatus.FORBIDDEN);
        assertNull(ctx.attributes.get("userId"));
    }

    @Test
    void unknownTicketIsUnauthorized() {
        WsTicketStore store = mock(WsTicketStore.class);
        when(store.consume(org.mockito.ArgumentMatchers.anyString())).thenReturn(null);

        JwtHandshakeInterceptor interceptor =
                new JwtHandshakeInterceptor(mock(com.macteval.security.JwtTokenProvider.class), store, false);
        Captured ctx = new Captured();

        boolean allowed = interceptor.beforeHandshake(
                request("lessonId=lesson-1&ticket=" + "c".repeat(32)),
                ctx.response, mock(WebSocketHandler.class), ctx.attributes);

        assertFalse(allowed);
        org.mockito.Mockito.verify(ctx.response).setStatusCode(HttpStatus.UNAUTHORIZED);
    }

    @Test
    void legacyQueryTokenIsRejectedWhenLegacySupportIsOff() {
        com.macteval.security.JwtTokenProvider provider =
                mock(com.macteval.security.JwtTokenProvider.class);
        // 即便 token 完全有效，默认配置下查询串令牌也不得通过——
        // 那是把 24 小时 JWT 写进访问日志的那条路径。
        when(provider.validateToken(org.mockito.ArgumentMatchers.anyString(),
                org.mockito.ArgumentMatchers.anyString())).thenReturn(true);

        JwtHandshakeInterceptor interceptor =
                new JwtHandshakeInterceptor(provider, mock(WsTicketStore.class), false);
        Captured ctx = new Captured();

        boolean allowed = interceptor.beforeHandshake(
                request("lessonId=lesson-1&token=some-valid-jwt"),
                ctx.response, mock(WebSocketHandler.class), ctx.attributes);

        assertFalse(allowed);
        org.mockito.Mockito.verify(ctx.response).setStatusCode(HttpStatus.UNAUTHORIZED);
        // 令牌分支根本不该被调用：连"校验通过"的机会都不给。
        org.mockito.Mockito.verify(provider, org.mockito.Mockito.never())
                .validateToken(org.mockito.ArgumentMatchers.anyString(),
                        org.mockito.ArgumentMatchers.anyString());
    }

    @Test
    void missingLessonIdIsBadRequestRatherThanUnauthorized() {
        // 区分原因有意义：没带 lessonId 是调用方的 bug，不是凭证问题。
        // 前端据此能显示"参数缺失"而不是"登录过期"。
        JwtHandshakeInterceptor interceptor = new JwtHandshakeInterceptor(
                mock(com.macteval.security.JwtTokenProvider.class), mock(WsTicketStore.class), false);
        Captured ctx = new Captured();

        boolean allowed = interceptor.beforeHandshake(
                request("ticket=" + "d".repeat(32)),
                ctx.response, mock(WebSocketHandler.class), ctx.attributes);

        assertFalse(allowed);
        org.mockito.Mockito.verify(ctx.response).setStatusCode(HttpStatus.BAD_REQUEST);
    }

    @Test
    void legacySupportFlagStillRequiresAValidAccessToken() {
        // 开关打开不等于放行一切：令牌仍要真的有效、且不是"必须改密"的账号。
        com.macteval.security.JwtTokenProvider provider =
                mock(com.macteval.security.JwtTokenProvider.class);
        when(provider.validateToken("good-jwt", "access")).thenReturn(true);
        when(provider.getUserIdFromToken("good-jwt")).thenReturn("user-9");
        when(provider.getRoleFromToken("good-jwt")).thenReturn("teacher");
        when(provider.isPasswordChangeRequired("good-jwt")).thenReturn(false);

        JwtHandshakeInterceptor interceptor = new JwtHandshakeInterceptor(
                provider, mock(WsTicketStore.class), true);

        // 需要一个 ServletServerHttpRequest 才能读 getParameter；用真实实现包一层。
        org.springframework.mock.web.MockHttpServletRequest servlet =
                new org.springframework.mock.web.MockHttpServletRequest("GET", "/api/ws/classroom");
        servlet.setQueryString("lessonId=lesson-1&token=good-jwt");
        servlet.addParameter("lessonId", "lesson-1");
        servlet.addParameter("token", "good-jwt");
        org.springframework.http.server.ServletServerHttpRequest real =
                new org.springframework.http.server.ServletServerHttpRequest(servlet);

        Captured ctx = new Captured();
        boolean allowed = interceptor.beforeHandshake(
                real, ctx.response, mock(WebSocketHandler.class), ctx.attributes);

        assertTrue(allowed);
        assertEquals("user-9", ctx.attributes.get("userId"));
    }
}
