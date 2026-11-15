package com.macteval.repository;

import com.macteval.model.entity.EvaluationReportEntity;
import jakarta.persistence.LockModeType;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import org.springframework.stereotype.Repository;

import java.time.LocalDateTime;
import java.util.Collection;
import java.util.List;
import java.util.Optional;

@Repository
public interface EvaluationReportRepository extends JpaRepository<EvaluationReportEntity, String> {

    Optional<EvaluationReportEntity> findByLessonSessionId(String lessonSessionId);
    boolean existsByLessonSessionId(String lessonSessionId);

    List<EvaluationReportEntity> findByLessonSessionIdIn(Collection<String> lessonSessionIds);

    List<EvaluationReportEntity> findByCreatedAtBetweenOrderByCreatedAtDesc(LocalDateTime start, LocalDateTime end);

    long count();

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select r from EvaluationReportEntity r where r.lessonSessionId = :lessonSessionId")
    Optional<EvaluationReportEntity> findByLessonSessionIdForUpdate(
            @Param("lessonSessionId") String lessonSessionId);
}
