package com.macteval.service;

import com.macteval.exception.BusinessException;
import com.macteval.model.entity.LessonSessionEntity;
import com.macteval.model.entity.StudentEntity;
import com.macteval.repository.LessonSessionRepository;
import com.macteval.repository.StudentRepository;
import com.macteval.security.WsTicketStore;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.util.Objects;

/**
 * 签发 WebSocket 一次性票据（SEC-02，2026-10-28）。
 *
 * <p>为什么在签发阶段就做课堂级授权，而不是留给握手后的
 * {@code ClassroomWebSocketHandler.canAccessLesson}：票据一旦泄漏，它本身就是
 * 一张 60 秒的"可连任意课堂"通行证。若不在签发时校验属主，攻击者用自己账号
 * 就能为**别人的**课堂拿到票据；虽然 handler 事后会关连接，但连接建立本身
 * 已经产生会话、写入日志，并被前端当成"连上了"。</p>
 *
 * <p>两处校验都保留（签发 + 连接），不是重复劳动：签发证明"你有权为这个课堂
 * 申请凭证"，连接证明"此刻你仍然有权"——课堂可能在这 60 秒内被转交或结束。</p>
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class WsTicketService {

    private final WsTicketStore ticketStore;
    private final LessonSessionRepository lessonSessionRepository;
    private final StudentRepository studentRepository;

    /**
     * @return 一次性票据字符串（32 位十六进制）
     * @throws BusinessException 课堂不存在（404）或无权访问（403）
     */
    public String issue(String lessonSessionId, String userId, String role) {
        LessonSessionEntity lesson = lessonSessionRepository.findById(lessonSessionId)
                .orElseThrow(() -> new BusinessException(404, "课堂不存在"));
        if (!canAccess(lesson, userId, role)) {
            // 不区分"课堂不存在"与"无权访问"之外的细节，避免枚举他人课堂 ID。
            log.warn("WS ticket denied: user={} role={} lesson={}", userId, role, lessonSessionId);
            throw new BusinessException(403, "无权访问该课堂的实时数据");
        }
        return ticketStore.issue(userId, role, lessonSessionId);
    }

    private boolean canAccess(LessonSessionEntity lesson, String userId, String role) {
        if (userId == null || role == null) {
            return false;
        }
        if ("admin".equalsIgnoreCase(role)) {
            return true;
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
}
