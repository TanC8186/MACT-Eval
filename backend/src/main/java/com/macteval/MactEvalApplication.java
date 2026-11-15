package com.macteval;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.autoconfigure.security.servlet.UserDetailsServiceAutoConfiguration;
import org.springframework.scheduling.annotation.EnableScheduling;

/**
 * 应用入口。
 *
 * <p><b>为什么显式排除 {@link UserDetailsServiceAutoConfiguration}</b>（2026-10-28）：
 * 本项目只用 JWT，从不使用表单登录，也没有任何 {@code UserDetailsService} bean。
 * 于是 Spring Boot 会自动配一个 {@code InMemoryUserDetailsManager}，随机生成一个
 * 密码并以 WARN 级别打印：</p>
 *
 * <pre>
 * Using generated security password: 95790ecc-0a1d-4efa-8c04-9a046ac4d8b1
 * </pre>
 *
 * <p>这个密码**不可用于任何接口**（JWT 过滤器不读它，formLogin 未启用），所以
 * 它不是可利用的凭据。但它确实是一条"把随机口令写进日志"的记录——日志脱敏扫描
 * 明确把它标了出来，而"这条 password 反正没用"是靠**语境**成立的结论，不是靠
 * 日志本身能看出来的。把它关掉，日志里就不存在需要解释的口令行。</p>
 */
@SpringBootApplication(exclude = UserDetailsServiceAutoConfiguration.class)
@EnableScheduling
public class MactEvalApplication {

    public static void main(String[] args) {
        SpringApplication.run(MactEvalApplication.class, args);
    }
}
