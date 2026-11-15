package com.macteval.repository;

import com.macteval.model.entity.EvaluationReportVersionEntity;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.stereotype.Repository;

import java.util.Collection;
import java.util.List;
import java.util.Optional;

@Repository
public interface EvaluationReportVersionRepository extends JpaRepository<EvaluationReportVersionEntity, String> {
    List<EvaluationReportVersionEntity> findByLessonSessionIdOrderByVersionDesc(String lessonSessionId);
    Optional<EvaluationReportVersionEntity> findByLessonSessionIdAndVersion(String lessonSessionId, Integer version);
    Optional<EvaluationReportVersionEntity> findFirstByLessonSessionIdOrderByVersionDesc(String lessonSessionId);
    Optional<EvaluationReportVersionEntity> findFirstByLessonSessionIdAndStatusOrderByVersionDesc(
            String lessonSessionId, String status);
    List<EvaluationReportVersionEntity> findByLessonSessionIdInAndStatus(
            Collection<String> lessonSessionIds, String status);
}
