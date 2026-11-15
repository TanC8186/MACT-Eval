package com.macteval.repository;

import com.macteval.model.entity.BehaviorEvidenceFrameEntity;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import org.springframework.stereotype.Repository;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Optional;

@Repository
public interface BehaviorEvidenceFrameRepository extends JpaRepository<BehaviorEvidenceFrameEntity, String> {
    List<BehaviorEvidenceFrameEntity> findByLessonSessionIdOrderByWindowIndexAsc(String lessonSessionId);
    List<BehaviorEvidenceFrameEntity> findByLessonSessionIdAndJobIdOrderByWindowIndexAsc(
            String lessonSessionId, String jobId);
    Optional<BehaviorEvidenceFrameEntity> findByJobIdAndWindowIndexAndCode(
            String jobId, Integer windowIndex, String code);
    List<BehaviorEvidenceFrameEntity> findByCreatedAtBefore(LocalDateTime cutoff);

    @Modifying(flushAutomatically = true, clearAutomatically = true)
    @Query("delete from BehaviorEvidenceFrameEntity f where f.jobId = :jobId")
    void deleteByJobId(@Param("jobId") String jobId);
}
