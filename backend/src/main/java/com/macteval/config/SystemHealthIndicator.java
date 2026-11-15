package com.macteval.config;

import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.actuate.health.Health;
import org.springframework.boot.actuate.health.HealthIndicator;
import org.springframework.data.redis.connection.RedisConnectionFactory;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestTemplate;

import javax.sql.DataSource;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 系统健康指示器 — 探测 PostgreSQL / Redis / ai-service。
 * <p>
 * 全部组件正常 → UP；任一组件异常 → DOWN 并在 details 中列出异常组件。
 */
@Slf4j
@Component
public class SystemHealthIndicator implements HealthIndicator {

    private static final int TIMEOUT_MS = 2000;

    private final DataSource dataSource;
    private final RedisConnectionFactory redisConnectionFactory;
    private final RestTemplate restTemplate;

    @Value("${mact.ai-service.url:http://localhost:8001}")
    private String aiServiceUrl;

    public SystemHealthIndicator(DataSource dataSource,
                                 RedisConnectionFactory redisConnectionFactory) {
        this.dataSource = dataSource;
        this.redisConnectionFactory = redisConnectionFactory;
        SimpleClientHttpRequestFactory factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(TIMEOUT_MS);
        factory.setReadTimeout(TIMEOUT_MS);
        this.restTemplate = new RestTemplate(factory);
    }

    @Override
    public Health health() {
        Map<String, Object> database = probeDatabase();
        Map<String, Object> redis = probeRedis();
        Map<String, Object> aiService = probeAiService();

        List<String> failed = new ArrayList<>();
        if (!"UP".equals(database.get("status"))) failed.add("postgresql");
        if (!"UP".equals(redis.get("status"))) failed.add("redis");
        if (!"UP".equals(aiService.get("status"))) failed.add("ai-service");

        Map<String, Object> details = new LinkedHashMap<>();
        details.put("postgresql", database);
        details.put("redis", redis);
        details.put("ai-service", aiService);

        if (failed.isEmpty()) {
            return Health.up().withDetails(details).build();
        }
        details.put("unavailable-components", failed);
        log.warn("System health DOWN, unavailable components: {}", failed);
        return Health.down().withDetails(details).build();
    }

    private Map<String, Object> probeDatabase() {
        Map<String, Object> info = new LinkedHashMap<>();
        try (Connection conn = dataSource.getConnection();
             var stmt = conn.createStatement();
             var rs = stmt.executeQuery("SELECT 1")) {
            if (rs.next() && rs.getInt(1) == 1) {
                info.put("status", "UP");
                info.put("product", conn.getMetaData().getDatabaseProductName());
            } else {
                info.put("status", "DOWN");
                info.put("error", "SELECT 1 returned an unexpected result");
            }
        } catch (Exception e) {
            info.put("status", "DOWN");
            info.put("error", e.getMessage());
        }
        return info;
    }

    private Map<String, Object> probeRedis() {
        Map<String, Object> info = new LinkedHashMap<>();
        try (var conn = redisConnectionFactory.getConnection()) {
            String pong = conn.ping();
            info.put("status", "PONG".equalsIgnoreCase(pong) ? "UP" : "DOWN");
            info.put("pong", pong);
        } catch (Exception e) {
            info.put("status", "DOWN");
            info.put("error", e.getMessage());
        }
        return info;
    }

    private Map<String, Object> probeAiService() {
        Map<String, Object> info = new LinkedHashMap<>();
        info.put("url", aiServiceUrl);
        try {
            var resp = restTemplate.getForEntity(aiServiceUrl + "/health", Map.class);
            boolean up = resp.getStatusCode().is2xxSuccessful() && resp.getBody() != null;
            info.put("status", up ? "UP" : "DOWN");
            info.put("http", resp.getStatusCode().value());
        } catch (Exception e) {
            info.put("status", "DOWN");
            info.put("error", e.getMessage());
        }
        return info;
    }
}
