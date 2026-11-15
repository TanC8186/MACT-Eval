package com.macteval.security;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 只为 {@code SecurityHeadersTest} 存在的探针端点（SEC-01，2026-10-28）。
 *
 * <p>为什么不直接用现有的 /health：那是 {@code permitAll} 的白名单端点，
 * 走的是不同的过滤链分支；而本次要验证的是"被认证保护的普通 API 响应"
 * 也带齐安全头。用一个只返回固定字符串的探针，可以把"响应头"与"业务逻辑"
 * 彻底分开——测试失败只可能是过滤链的问题。</p>
 *
 * <p>放在 test 源码树里，不会进入生产镜像。</p>
 */
@RestController
public class SecurityHeadersProbeController {

    @GetMapping("/security-probe")
    public String probe() {
        return "ok";
    }
}
