package com.macteval.config;

import io.minio.BucketExistsArgs;
import io.minio.MakeBucketArgs;
import io.minio.MinioClient;
import lombok.extern.slf4j.Slf4j;
import org.springframework.boot.ApplicationRunner;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Primary;
import org.springframework.data.redis.connection.RedisConnectionFactory;
import org.springframework.data.redis.core.RedisTemplate;
import org.springframework.data.redis.serializer.StringRedisSerializer;
import org.springframework.web.servlet.config.annotation.CorsRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

import java.util.Arrays;
import java.util.List;

@Slf4j
@Configuration
public class AppConfig implements WebMvcConfigurer {

    @Value("${mact.minio.endpoint}")
    private String minioEndpoint;

    @Value("${mact.minio.public-endpoint:http://localhost:9000}")
    private String minioPublicEndpoint;

    @Value("${mact.minio.access-key}")
    private String minioAccessKey;

    @Value("${mact.minio.secret-key}")
    private String minioSecretKey;

    @Value("${mact.cors.allowed-origins}")
    private String allowedOrigins;

    @Override
    public void addCorsMappings(CorsRegistry registry) {
        registry.addMapping("/**")
                .allowedOrigins(parseOrigins(allowedOrigins))
                .allowedMethods("GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH")
                .allowedHeaders("*")
                .allowCredentials(true);
    }

    /**
     * 把 CORS 来源 CSV 解析为来源数组：逐项去掉首尾空白并丢弃空项。
     *
     * <p>裸 {@code split(",")} 会把 {@code "http://a:3000, http://b:3000"} 的第二项
     * 解析成带前导空格的 {@code " http://b:3000"}，该字符串永远不等于浏览器发送的
     * Origin，请求会被静默拒绝。ai-service 侧已做同样的去空白处理，这里保持一致。</p>
     *
     * <p>全部为空时返回空数组（不放行任何来源），保持 fail-closed，绝不回退成 {@code *}。</p>
     */
    static String[] parseOrigins(String csv) {
        if (csv == null) return new String[0];
        return Arrays.stream(csv.split(","))
                .map(String::trim)
                .filter(origin -> !origin.isEmpty())
                .toArray(String[]::new);
    }

    /**
     * 后端内部使用的 MinIO 客户端（容器内网端点 <code>minio:9000</code>），
     * 用于 bucket 初始化、对象存在性检查、AI worker 下载等后端内部 I/O。
     */
    @Bean
    @Primary
    public MinioClient minioClient() {
        return MinioClient.builder()
                .endpoint(minioEndpoint)
                .credentials(minioAccessKey, minioSecretKey)
                .build();
    }

    /**
     * 生成预签名 URL 专用客户端：使用浏览器可达的 public endpoint。
     * <p>SigV4 签名绑定 host，若用内网端点生成 URL 再重写主机名会导致签名失效（403）。
     * 因此必须让签名直接按浏览器可达地址计算。
     * 显式指定 region（us-east-1，MinIO 默认）以跳过 SDK 的 region 探测网络请求——
     * 否则它会尝试连接该 endpoint，而 public endpoint 在容器内不可达。</p>
     */
    @Bean
    public MinioClient presignMinioClient() {
        return MinioClient.builder()
                .endpoint(minioPublicEndpoint)
                .region("us-east-1")
                .credentials(minioAccessKey, minioSecretKey)
                .build();
    }

    @Bean
    public RedisTemplate<String, Object> redisTemplate(RedisConnectionFactory factory) {
        RedisTemplate<String, Object> template = new RedisTemplate<>();
        template.setConnectionFactory(factory);
        template.setKeySerializer(new StringRedisSerializer());
        template.setValueSerializer(new StringRedisSerializer());
        template.setHashKeySerializer(new StringRedisSerializer());
        template.setHashValueSerializer(new StringRedisSerializer());
        return template;
    }

    @Bean
    public ApplicationRunner minioBucketInitializer(
            MinioClient client,
            @Value("${mact.minio.enabled:true}") boolean enabled) {
        return args -> {
            if (!enabled) {
                log.info("MinIO bucket initialization is disabled");
                return;
            }

            try {
                List<String> buckets = List.of(
                        "teaching-resources", "student-submissions", "classroom-raw-media",
                        "analysis-artifacts", "report-attachments", "temporary-uploads"
                );
                for (String bucket : buckets) {
                    if (!client.bucketExists(BucketExistsArgs.builder().bucket(bucket).build())) {
                        client.makeBucket(MakeBucketArgs.builder().bucket(bucket).build());
                        log.info("Created MinIO bucket: {}", bucket);
                    }
                }
            } catch (Exception e) {
                log.warn("Failed to initialize MinIO buckets: {}", e.getMessage());
            }
        };
    }
}
