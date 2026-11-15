package com.macteval.repository;

import com.macteval.model.entity.BehaviorObservationEntity;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.stereotype.Repository;

import java.util.List;
import java.util.Optional;

@Repository
public interface BehaviorObservationRepository extends JpaRepository<BehaviorObservationEntity, String> {
    List<BehaviorObservationEntity> findByLessonSessionIdOrderByWindowStartAsc(String lessonSessionId);
    List<BehaviorObservationEntity> findByLessonSessionIdAndActivityId(String lessonSessionId, String activityId);
    List<BehaviorObservationEntity> findByLessonSessionIdAndJobIdOrderByWindowIndexAsc(
            String lessonSessionId, String jobId);
    Optional<BehaviorObservationEntity> findByJobIdAndWindowIndex(String jobId, Integer windowIndex);
    boolean existsByLessonSessionId(String lessonSessionId);
    long countByJobId(String jobId);
}
