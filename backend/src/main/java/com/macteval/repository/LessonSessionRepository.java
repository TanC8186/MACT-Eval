package com.macteval.repository;

import com.macteval.model.entity.LessonSessionEntity;
import jakarta.persistence.LockModeType;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import org.springframework.stereotype.Repository;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Optional;

@Repository
public interface LessonSessionRepository extends JpaRepository<LessonSessionEntity, String> {
    List<LessonSessionEntity> findByCourseId(String courseId);
    List<LessonSessionEntity> findByTeacherId(String teacherId);
    List<LessonSessionEntity> findByClassId(String classId);
    Page<LessonSessionEntity> findByCourseId(String courseId, Pageable pageable);
    Page<LessonSessionEntity> findByTeacherId(String teacherId, Pageable pageable);
    Page<LessonSessionEntity> findByClassId(String classId, Pageable pageable);
    boolean existsByClassId(String classId);
    List<LessonSessionEntity> findByTeacherIdAndDateBetween(String teacherId, LocalDateTime start, LocalDateTime end);
    List<LessonSessionEntity> findByStatus(LessonSessionEntity.SessionStatus status);

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select s from LessonSessionEntity s where s.id = :id")
    Optional<LessonSessionEntity> findByIdForUpdate(@Param("id") String id);
}
