package com.macteval.security;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.boot.test.mock.mockito.MockBean;
import org.springframework.context.annotation.Import;
import org.springframework.data.redis.connection.RedisConnectionFactory;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;

import static org.hamcrest.Matchers.containsString;
import static org.hamcrest.Matchers.not;
import static org.springframework.security.test.web.servlet.request.SecurityMockMvcRequestPostProcessors.csrf;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.options;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.header;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/**
 * SEC-01 回归：后端 API 响应必须携带安全响应头。
 *
 * <p>审计当时的结论是"Spring Security 的 HeaderWriterFilter 默认会加
 * nosniff/X-Frame-Options，但**没有实测**"。本测试把"默认行为"变成断言：
 * 默认值随框架大版本变过，而这些头都是"缺了不报错、只是不安全"的类型，
 * 只靠读源码无法防止将来某次升级把它们悄悄关掉。</p>
 *
 * <p>故意用真实的 {@link SecurityConfig}（@Import 而非 mock）：要验证的就是
 * 那条过滤链本身，mock 掉它等于什么都没验证。</p>
 */
@WebMvcTest(controllers = SecurityHeadersProbeController.class)
@Import(SecurityConfig.class)
class SecurityHeadersTest {

    @Autowired
    private MockMvc mockMvc;

    @MockBean
    private JwtTokenProvider jwtTokenProvider;

    @MockBean
    private RedisConnectionFactory redisConnectionFactory;

    @MockBean
    private JwtAuthenticationFilter jwtAuthenticationFilter;

    @MockBean
    private PasswordChangeRequiredFilter passwordChangeRequiredFilter;

    // 不 mock ObjectMapper：它同时被 MVC 的消息转换器使用，
    // mock 掉会让 RouterFunctionMapping 在上下文启动时 NPE——
    // 这与被测的安全头毫无关系，只会把失败原因指向错误的地方。

    @Test
    void apiResponsesCarryLockdownCsp() throws Exception {
        // API 的 CSP 必须是 lockdown 形态：default-src 'none' + frame-ancestors 'none'。
        // 不是页面 CSP 的复制品——JSON 不需要执行任何脚本或加载任何子资源。
        mockMvc.perform(get("/api/security-probe").contextPath("/api"))
                .andExpect(header().string("Content-Security-Policy",
                        containsString("default-src 'none'")))
                .andExpect(header().string("Content-Security-Policy",
                        containsString("frame-ancestors 'none'")));
    }

    @Test
    void apiResponsesCarryNosniffAndDenyFraming() throws Exception {
        mockMvc.perform(get("/api/security-probe").contextPath("/api"))
                .andExpect(header().string("X-Content-Type-Options", "nosniff"))
                .andExpect(header().string("X-Frame-Options", "DENY"));
    }

    @Test
    void apiResponsesDoNotLeakReferrerOrGrantDevicePermissions() throws Exception {
        mockMvc.perform(get("/api/security-probe").contextPath("/api"))
                .andExpect(header().string("Referrer-Policy", "no-referrer"))
                // camera/microphone 必须显式 deny：教师端不用它们，
                // 但浏览器默认允许同源页面请求，等于留了一个摄像头入口。
                .andExpect(header().string("Permissions-Policy", containsString("camera=()")))
                .andExpect(header().string("Permissions-Policy", containsString("microphone=()")));
    }

    @Test
    void apiCspDoesNotPermitInlineScriptOrArbitraryHosts() throws Exception {
        // 反向断言：API 的 CSP 里出现 unsafe-inline/unsafe-eval 或通配来源，
        // 说明有人把页面 CSP 抄过来了。那会让 API 响应在被当页面打开时
        // 允许执行内联脚本。
        mockMvc.perform(get("/api/security-probe").contextPath("/api"))
                .andExpect(header().string("Content-Security-Policy",
                        not(containsString("unsafe-inline"))))
                .andExpect(header().string("Content-Security-Policy",
                        not(containsString("unsafe-eval"))))
                .andExpect(header().string("Content-Security-Policy",
                        not(containsString("*"))));
    }

    @Test
    void preflightIsAllowedFromConfiguredOriginAndRejectedFromRetiredPort() throws Exception {
        // CORS 与安全头一起验证：预检响应也必须带 nosniff，
        // 否则某些代理/浏览器会在预检失败后静默丢弃后续请求。
        mockMvc.perform(options("/api/security-probe")
                        .contextPath("/api")
                        .header("Origin", "http://localhost:5173")
                        .header("Access-Control-Request-Method", "GET"))
                .andExpect(status().isOk())
                .andExpect(header().string("X-Content-Type-Options", "nosniff"));

        mockMvc.perform(options("/api/security-probe")
                        .contextPath("/api")
                        .header("Origin", "http://localhost:3001")
                        .header("Access-Control-Request-Method", "GET"))
                .andExpect(status().isForbidden());
    }
}
