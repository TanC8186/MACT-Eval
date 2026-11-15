package com.macteval.service;

import com.macteval.model.entity.LessonSessionEntity;
import com.macteval.model.entity.SuggestionEntity;
import com.macteval.repository.LessonSessionRepository;
import com.macteval.repository.SuggestionRepository;
import lombok.RequiredArgsConstructor;
import org.springframework.security.access.AccessDeniedException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;
import java.util.NoSuchElementException;

/**
 * 教学建议状态流转与属主校验。
 *
 * <p>建议必须属于某个课堂；教师只能操作自己课堂的建议。没有课堂关联的
 * 历史建议只允许管理员处理，避免教师通过猜测 ID 修改他人建议。</p>
 */
@Service
@RequiredArgsConstructor
public class SuggestionService {

    private final SuggestionRepository suggestionRepository;
    private final LessonSessionRepository lessonSessionRepository;

    public enum Action { ADOPT, ACCEPT, IGNORE }

    @Transactional
    public SuggestionEntity changeStatus(String suggestionId, Action action, String userId, boolean isAdmin) {
        SuggestionEntity entity = suggestionRepository.findById(suggestionId)
                .orElseThrow(() -> new NoSuchElementException("建议不存在"));
        assertCanManage(entity, userId, isAdmin);

        LocalDateTime now = LocalDateTime.now();
        switch (action) {
            case ADOPT -> {
                entity.setStatus("adopted");
                entity.setAdoptedAt(now);
            }
            case ACCEPT -> {
                entity.setStatus("accepted");
                entity.setAcceptedAt(now);
                if (entity.getAdoptedAt() == null) {
                    // /accept 是旧端点；效果追踪以 adoptedAt 为准，兼容记录采纳时间。
                    entity.setAdoptedAt(now);
                }
            }
            case IGNORE -> {
                entity.setStatus("ignored");
                entity.setIgnoredAt(now);
            }
        }
        return suggestionRepository.save(entity);
    }

    private void assertCanManage(SuggestionEntity suggestion, String userId, boolean isAdmin) {
        if (isAdmin) {
            return;
        }
        if (suggestion.getLessonSessionId() == null || userId == null) {
            throw new AccessDeniedException("无权操作该教学建议");
        }
        String owner = lessonSessionRepository.findById(suggestion.getLessonSessionId())
                .map(LessonSessionEntity::getTeacherId)
                .orElse(null);
        if (owner == null || !owner.equals(userId)) {
            throw new AccessDeniedException("无权操作其他教师课堂的教学建议");
        }
    }
}
