package com.macteval.config;

import com.macteval.websocket.ClassroomWebSocketHandler;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.socket.config.annotation.EnableWebSocket;
import org.springframework.web.socket.config.annotation.WebSocketConfigurer;
import org.springframework.web.socket.config.annotation.WebSocketHandlerRegistry;
import org.springframework.web.socket.server.standard.ServletServerContainerFactoryBean;

import java.util.List;

@Configuration
@EnableWebSocket
public class WebSocketConfig implements WebSocketConfigurer {

    private final ClassroomWebSocketHandler classroomHandler;
    private final JwtHandshakeInterceptor jwtHandshakeInterceptor;
    private final List<String> allowedOrigins;

    public WebSocketConfig(
            ClassroomWebSocketHandler classroomHandler,
            JwtHandshakeInterceptor jwtHandshakeInterceptor,
            @Value("${mact.cors.allowed-origins}") String allowedOriginsCsv) {
        this.classroomHandler = classroomHandler;
        this.jwtHandshakeInterceptor = jwtHandshakeInterceptor;
        this.allowedOrigins = List.of(allowedOriginsCsv.split(","));
    }

    @Override
    public void registerWebSocketHandlers(WebSocketHandlerRegistry registry) {
        registry.addHandler(classroomHandler, "/ws/classroom")
                .addInterceptors(jwtHandshakeInterceptor)
                // 与 HTTP CORS 对齐，禁止匿名跨站 WebSocket 窃听
                .setAllowedOrigins(allowedOrigins.toArray(new String[0]));
    }

    @Bean
    public ServletServerContainerFactoryBean createWebSocketContainer() {
        ServletServerContainerFactoryBean container = new ServletServerContainerFactoryBean();
        container.setMaxTextMessageBufferSize(65536);
        container.setMaxBinaryMessageBufferSize(65536);
        container.setMaxSessionIdleTimeout(600000L);
        return container;
    }
}
