package com.macteval.repository;

import com.macteval.model.entity.LessonTranscriptEntity;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.stereotype.Repository;

import java.util.Optional;

@Repository
public interface LessonTranscriptRepository extends JpaRepository<LessonTranscriptEntity, String> {
    Optional<LessonTranscriptEntity> findFirstByLessonSessionIdOrderByCreatedAtDescIdDesc(String lessonSessionId);
    Optional<LessonTranscriptEntity> findByJobId(String jobId);
    boolean existsByLessonSessionId(String lessonSessionId);
}
