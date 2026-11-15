package com.macteval.repository;

import com.macteval.model.entity.LessonWindowFeatureEntity;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.List;

public interface LessonWindowFeatureRepository extends JpaRepository<LessonWindowFeatureEntity, String> {
    List<LessonWindowFeatureEntity> findByLessonSessionIdOrderByWindowIndexAsc(String lessonSessionId);
    boolean existsByLessonSessionId(String lessonSessionId);

    /**
     * 批量删除某课堂的<b>全部版本</b>窗口特征。
     * <p>对齐路径已改用 {@link #deleteByLessonSessionIdAndJobIds}（按版本删除）；
     * 本方法只留给“整课堂派生数据删除”这类显式维护操作，不要在任务补偿里使用。</p>
     */
    @Modifying(flushAutomatically = true, clearAutomatically = true)
    @Query("delete from LessonWindowFeatureEntity e where e.lessonSessionId = :lessonSessionId")
    void deleteByLessonSessionId(@Param("lessonSessionId") String lessonSessionId);

    /**
     * 按分析版本读取窗口（R04）：同一课堂下按 (video_job_id, audio_job_id) 组合精确选取。
     * <p>空串与 NULL 视为同一含义（该模态本版本无任务）：新行写空串，历史行为 NULL，
     * 因此用 {@code coalesce(...,'')} 归一后再比较。</p>
     */
    @Query("select e from LessonWindowFeatureEntity e where e.lessonSessionId = :lessonSessionId "
            + "and coalesce(e.videoJobId, '') = :videoJobId "
            + "and coalesce(e.audioJobId, '') = :audioJobId "
            + "order by e.windowIndex asc")
    List<LessonWindowFeatureEntity> findByLessonSessionIdAndJobIdsOrderByWindowIndexAsc(
            @Param("lessonSessionId") String lessonSessionId,
            @Param("videoJobId") String videoJobId,
            @Param("audioJobId") String audioJobId);

    /**
     * 只删除同一版本组合的窗口（对齐重建前的幂等清空）。
     * <p>不能再用 {@link #deleteByLessonSessionId}：旧任务的补偿对齐会整课堂删除，
     * 把新版本已经发布的窗口覆盖掉（R04）。</p>
     */
    @Modifying(flushAutomatically = true, clearAutomatically = true)
    @Query("delete from LessonWindowFeatureEntity e where e.lessonSessionId = :lessonSessionId "
            + "and coalesce(e.videoJobId, '') = :videoJobId "
            + "and coalesce(e.audioJobId, '') = :audioJobId")
    void deleteByLessonSessionIdAndJobIds(@Param("lessonSessionId") String lessonSessionId,
                                          @Param("videoJobId") String videoJobId,
                                          @Param("audioJobId") String audioJobId);

    /** 该课堂是否存在任一窗口行（任意版本）。 */
    @Query("select case when count(e) > 0 then true else false end "
            + "from LessonWindowFeatureEntity e where e.lessonSessionId = :lessonSessionId")
    boolean existsAnyVersion(@Param("lessonSessionId") String lessonSessionId);
}
