package com.macteval.websocket;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.macteval.model.entity.LessonSessionEntity;
import com.macteval.model.entity.StudentEntity;
import com.macteval.repository.LessonSessionRepository;
import com.macteval.repository.StudentRepository;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Component;
import org.springframework.web.socket.CloseStatus;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketSession;
import org.springframework.web.socket.handler.TextWebSocketHandler;

import java.io.IOException;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArraySet;

/**
 * WebSocket handler for real-time classroom data.
 * Handshake JWT is required (JwtHandshakeInterceptor); room join is further
 * scoped to the authenticated user (teacher owns lesson / student in class).
 */
@Slf4j
@Component
public class ClassroomWebSocketHandler extends TextWebSocketHandler {

    private final ObjectMapper objectMapper;
    private final LessonSessionRepository lessonSessionRepository;
    private final StudentRepository studentRepository;

    private final Map<String, Set<WebSocketSession>> lessonSessions = new ConcurrentHashMap<>();
    private final Map<String, String> sessionLessons = new ConcurrentHashMap<>();
    /** per-session send lock — Spring requires single-threaded sendMessage */
    private final Map<String, Object> sessionLocks = new ConcurrentHashMap<>();

    public ClassroomWebSocketHandler(
            ObjectMapper objectMapper,
            LessonSessionRepository lessonSessionRepository,
            StudentRepository studentRepository) {
        this.objectMapper = objectMapper;
        this.lessonSessionRepository = lessonSessionRepository;
        this.studentRepository = studentRepository;
    }

    @Override
    public void afterConnectionEstablished(WebSocketSession session) {
        String lessonId = extractLessonId(session);
        if (lessonId == null) {
            log.info("WebSocket connected: session={} (no lesson)", session.getId());
            return;
        }
        if (!canAccessLesson(session, lessonId)) {
            log.warn("WebSocket room denied: session={}, user={}, lesson={}",
                    session.getId(), principal(session), lessonId);
            try {
                session.close(CloseStatus.POLICY_VIOLATION);
            } catch (IOException ignored) {
                // already closing
            }
            return;
        }
        lessonSessions.computeIfAbsent(lessonId, k -> new CopyOnWriteArraySet<>()).add(session);
        sessionLessons.put(session.getId(), lessonId);
        log.info("WebSocket connected: session={}, lesson={}", session.getId(), lessonId);
    }

    @Override
    public void afterConnectionClosed(WebSocketSession session, CloseStatus status) {
        String lessonId = sessionLessons.remove(session.getId());
        if (lessonId != null) {
            Set<WebSocketSession> sessions = lessonSessions.get(lessonId);
            if (sessions != null) {
                sessions.remove(session);
                if (sessions.isEmpty()) {
                    lessonSessions.remove(lessonId);
                }
            }
        }
        sessionLocks.remove(session.getId());
        log.info("WebSocket disconnected: session={}", session.getId());
    }

    @Override
    protected void handleTextMessage(WebSocketSession session, TextMessage message) {
        // Client can send subscribe messages to change room — currently ack only.
    }

    public void sendToLesson(String lessonSessionId, Object payload) {
        Set<WebSocketSession> sessions = lessonSessions.get(lessonSessionId);
        if (sessions == null || sessions.isEmpty()) return;

        try {
            String json = objectMapper.writeValueAsString(payload);
            TextMessage message = new TextMessage(json);
            for (WebSocketSession session : sessions) {
                if (!session.isOpen()) continue;
                Object lock = sessionLocks.computeIfAbsent(session.getId(), k -> new Object());
                synchronized (lock) {
                    try {
                        session.sendMessage(message);
                    } catch (IOException e) {
                        log.warn("Failed to send message to session {}: {}", session.getId(), e.getMessage());
                    }
                }
            }
        } catch (Exception e) {
            log.error("Failed to serialize message for lesson {}", lessonSessionId, e);
        }
    }

    public void pushAttendanceUpdate(String lessonSessionId, Map<String, Object> summary) {
        sendToLesson(lessonSessionId, Map.of(
                "type", "attendance:update",
                "data", summary,
                "timestamp", System.currentTimeMillis()
        ));
    }

    public void pushBehaviorUpdate(String lessonSessionId, Map<String, Object> behavior) {
        sendToLesson(lessonSessionId, Map.of(
                "type", "behavior:update",
                "data", behavior,
                "timestamp", System.currentTimeMillis()
        ));
    }

    /** teacher owns lesson, admin, or student belongs to the lesson's class. */
    @SuppressWarnings("unchecked")
    private boolean canAccessLesson(WebSocketSession session, String lessonId) {
        String userId = attr(session, "userId");
        String role = attr(session, "role");
        if (userId == null) {
            return false;
        }
        if ("admin".equalsIgnoreCase(role)) {
            return true;
        }
        LessonSessionEntity lesson = lessonSessionRepository.findById(lessonId).orElse(null);
        if (lesson == null) {
            return false;
        }
        if ("teacher".equalsIgnoreCase(role)) {
            return Objects.equals(userId, lesson.getTeacherId());
        }
        if ("student".equalsIgnoreCase(role)) {
            StudentEntity student = studentRepository.findById(userId).orElse(null);
            return student != null
                    && lesson.getClassId() != null
                    && lesson.getClassId().equals(student.getClassId());
        }
        return false;
    }

    private String attr(WebSocketSession session, String key) {
        Object v = session.getAttributes() != null ? session.getAttributes().get(key) : null;
        return v != null ? String.valueOf(v) : null;
    }

    private String principal(WebSocketSession session) {
        return attr(session, "userId");
    }

    private String extractLessonId(WebSocketSession session) {
        String query = session.getUri() != null ? session.getUri().getQuery() : null;
        if (query != null && query.contains("lessonId=")) {
            String[] params = query.split("&");
            for (String param : params) {
                if (param.startsWith("lessonId=")) {
                    return param.substring("lessonId=".length());
                }
            }
        }
        return null;
    }
}
