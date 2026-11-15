package com.macteval.repository;

import com.macteval.model.entity.AttendanceRecordEntity;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import org.springframework.stereotype.Repository;

import java.util.List;

@Repository
public interface AttendanceRecordRepository extends JpaRepository<AttendanceRecordEntity, String> {
    List<AttendanceRecordEntity> findByLessonSessionId(String lessonSessionId);
    boolean existsByLessonSessionId(String lessonSessionId);
    List<AttendanceRecordEntity> findByLessonSessionIdAndStudentId(String lessonSessionId, String studentId);
    long countByLessonSessionIdAndStatus(String lessonSessionId, AttendanceRecordEntity.AttendanceStatus status);

    /**
     * 按名册补齐缺失考勤项；已有人工结果保持不变。
     * ON CONFLICT 依赖 V23 的 uq_attendance_lesson_student。
     */
    @Modifying
    @Query(value = """
            INSERT INTO attendance_records
                (id, lesson_session_id, student_id, status, source, camera_visible,
                 teacher_confirmed, created_at, updated_at)
            VALUES (:id, :lessonId, :studentId, 'PENDING', 'CAMERA_EVIDENCE', FALSE,
                    FALSE, NOW(), NOW())
            ON CONFLICT (lesson_session_id, student_id) DO NOTHING
            """, nativeQuery = true)
    int insertPendingIfAbsent(@Param("id") String id,
                              @Param("lessonId") String lessonSessionId,
                              @Param("studentId") String studentId);
}
