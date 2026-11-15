package com.macteval.repository;

import com.macteval.model.entity.SuggestionEntity;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.stereotype.Repository;
import java.util.Collection;
import java.util.List;

@Repository
public interface SuggestionRepository extends JpaRepository<SuggestionEntity, String> {
    List<SuggestionEntity> findByLessonSessionId(String lessonSessionId);
    boolean existsByLessonSessionId(String lessonSessionId);
    List<SuggestionEntity> findByStatus(String status);
    List<SuggestionEntity> findByStatusIn(Collection<String> statuses);
    List<SuggestionEntity> findByLessonSessionIdAndStatus(String lessonSessionId, String status);
    List<SuggestionEntity> findByStatusInAndLessonSessionIdIn(
            Collection<String> statuses, Collection<String> lessonSessionIds);
}
