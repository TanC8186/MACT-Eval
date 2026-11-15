package com.macteval.config;

import org.junit.jupiter.api.Test;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.web.cors.CorsConfiguration;
import org.springframework.web.servlet.config.annotation.CorsRegistry;
import org.yaml.snakeyaml.Yaml;

import java.io.InputStream;
import java.util.Arrays;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * CORS 来源 CSV 的解析契约。
 *
 * <p>裸 {@code split(",")} 会把 {@code "http://a:3000, http://b:3000"} 的第二项解析成
 * 带前导空格的值，该值永远无法匹配浏览器发送的 Origin，跨域请求会被静默拒绝。
 * 这里锁定三项行为：去空白、丢弃空项、以及生产默认值本身的最小集合。</p>
 */
class CorsOriginConfigTest {

    private static final List<String> PRODUCTION_ORIGINS = List.of(
            "http://localhost:3000", "http://localhost:5173",
            "http://127.0.0.1:3000", "http://127.0.0.1:5173");

    @Test
    void trimsWhitespaceAroundEachOrigin() {
        assertThat(AppConfig.parseOrigins("http://a:3000, http://b:3000"))
                .containsExactly("http://a:3000", "http://b:3000");

        assertThat(AppConfig.parseOrigins("  http://a:3000  ,\thttp://b:3000\n"))
                .containsExactly("http://a:3000", "http://b:3000");
    }

    @Test
    void dropsBlankEntriesInsteadOfRegisteringUnmatchedOrigins() {
        assertThat(AppConfig.parseOrigins("http://a:3000, ,http://b:3000,"))
                .containsExactly("http://a:3000", "http://b:3000");

        // 全空不得退化成放开所有来源，而是不放行任何来源（fail-closed）。
        assertThat(AppConfig.parseOrigins("  ,  ,")).isEmpty();
        assertThat(AppConfig.parseOrigins("")).isEmpty();
        assertThat(AppConfig.parseOrigins(null)).isEmpty();
    }

    @Test
    void corsRegistrationUsesTheTrimmedOrigins() {
        CorsConfiguration configuration = registeredConfiguration("http://a:3000, http://b:3000 ,");

        assertThat(configuration.getAllowedOrigins())
                .containsExactly("http://a:3000", "http://b:3000");
        assertThat(configuration.getAllowCredentials()).isTrue();
    }

    /**
     * 生产默认值来自 {@code application.yml}（单端交付：仅教师/管理员端的两个来源，
     * 含 localhost 与 127.0.0.1 两种写法）。3001 是已下线学生端，5174/5175 是双端时代的
     * Vite 端口，都不得再被放行。
     */
    @Test
    void productionDefaultContainsExactlyTheTwoDeliveredFrontendOrigins() {
        String csv = yamlValue("mact", "cors", "allowed-origins");
        assertThat(csv).isNotNull();

        List<String> parsed = Arrays.asList(AppConfig.parseOrigins(csv));
        assertThat(parsed).containsExactlyElementsOf(PRODUCTION_ORIGINS);

        assertThat(parsed).noneMatch(origin ->
                origin.endsWith(":3001") || origin.endsWith(":5174") || origin.endsWith(":5175"));

        // 同一份默认值经过 addCorsMappings 后仍是这四个来源。
        assertThat(registeredConfiguration(csv).getAllowedOrigins())
                .containsExactlyElementsOf(PRODUCTION_ORIGINS);
    }

    /** {@code CorsRegistry#getCorsConfigurations()} 是 protected，测试通过反射读取注册结果。 */
    @SuppressWarnings("unchecked")
    private CorsConfiguration registeredConfiguration(String csv) {
        AppConfig config = new AppConfig();
        ReflectionTestUtils.setField(config, "allowedOrigins", csv);
        CorsRegistry registry = new CorsRegistry();
        config.addCorsMappings(registry);
        Map<String, CorsConfiguration> configurations =
                ReflectionTestUtils.invokeMethod(registry, "getCorsConfigurations");
        assertThat(configurations).isNotNull();
        return configurations.get("/**");
    }

    @SuppressWarnings("unchecked")
    private String yamlValue(String... path) {
        try (InputStream in = getClass().getResourceAsStream("/application.yml")) {
            assertThat(in).as("application.yml must be on the test classpath").isNotNull();
            Object node = new Yaml().load(in);
            for (String key : path) {
                assertThat(node).isInstanceOf(Map.class);
                node = ((Map<String, Object>) node).get(key);
            }
            return node == null ? null : String.valueOf(node);
        } catch (Exception e) {
            throw new IllegalStateException("Could not read application.yml", e);
        }
    }
}
