package com.macteval.repository;

import com.macteval.model.entity.MediaAssetEntity;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.stereotype.Repository;

import java.time.LocalDateTime;
import java.util.Collection;
import java.util.List;
import java.util.Optional;

@Repository
public interface MediaAssetRepository extends JpaRepository<MediaAssetEntity, String> {
    Optional<MediaAssetEntity> findByIdAndOwnerId(String id, String ownerId);
    long countByLessonSessionIdAndStatusIn(String lessonSessionId, Collection<String> statuses);
    List<MediaAssetEntity> findByLessonSessionIdAndStatusIn(String lessonSessionId, Collection<String> statuses);
    List<MediaAssetEntity> findByStatusInAndExpiresAtBefore(
            Collection<String> statuses, LocalDateTime cutoff);
    List<MediaAssetEntity> findByStatusAndCreatedAtBefore(String status, LocalDateTime cutoff);
}
