package com.macteval.service;

import com.macteval.model.entity.LessonWindowFeatureEntity;
import com.macteval.repository.LessonWindowFeatureRepository;

import java.util.List;

/**
 * 按分析版本选取窗口特征的统一规则（R04）。
 *
 * <p>窗口行按 {@code (lesson_session_id, video_job_id, audio_job_id, window_index)} 唯一，
 * 因此“读到哪一版”必须由报告的选定任务组合决定，不能读课堂全局行——否则旧任务
 * 的补偿对齐会让截图来自 J2、分数来自 J1。</p>
 *
 * <p>历史行回退规则（R11）：<b>只有纯 legacy 报告</b>才允许读取“两列皆空”的历史行。
 * 判定条件是报告没有选定任何成功的分析任务（视频与音频任务 id 都为空）。只要选定了
 * 任一成功任务，精确版本没有窗口就必须返回空——否则“J2 已成功但窗口尚未生成/对齐
 * 仍 PENDING/结果为空”的报告会用 J1 时代的历史行出分，出现任务号来自 J2、分数来自
 * 历史行的混用。此时报告必须显示数据不足（窗口未完成），而不是旧窗口分数。</p>
 */
final class WindowVersionSelector {

    /** 空串表示“该模态本版本没有任务”；与历史行的 NULL 视为同一含义。 */
    static final String NO_JOB = "";

    /**
     * 报告是否处于“纯历史/legacy”状态：没有任何成功分析任务被选定。
     *
     * <p>只有这种报告允许回退读取两列皆空的历史行。选定了任一任务（视频或音频）
     * 时，精确版本必须自己给出窗口；否则意味着该版本的窗口尚未生成、对齐仍在
     * PENDING 或结果为空，属于“数据不足”，不得用旧行补分。</p>
     */
    static boolean isLegacyOnly(String videoJobId, String audioJobId) {
        return NO_JOB.equals(normalize(videoJobId)) && NO_JOB.equals(normalize(audioJobId));
    }

    private WindowVersionSelector() {
    }

    static String normalize(String jobId) {
        return jobId == null || jobId.isBlank() ? NO_JOB : jobId;
    }

    /**
     * 读取指定版本组合的窗口特征；仅当组合为空且报告属于纯 legacy 时回退历史行。
     *
     * <p>回退许可由 {@link #isLegacyOnly} 推导，调用方无需额外判断。</p>
     */
    static List<LessonWindowFeatureEntity> read(LessonWindowFeatureRepository repository,
                                                String lessonSessionId,
                                                String videoJobId,
                                                String audioJobId) {
        return read(repository, lessonSessionId, videoJobId, audioJobId,
                isLegacyOnly(videoJobId, audioJobId));
    }

    /**
     * 读取指定版本组合的窗口特征，并显式指定是否允许历史行回退。
     *
     * <p>{@code allowHistoricalFallback} 只应为纯 legacy 报告传 {@code true}。
     * 允许回退时：精确组合无行 → 读取两列皆空的历史行；否则返回空列表
     * （调用方据此生成“数据不足/窗口未完成”状态）。</p>
     */
    static List<LessonWindowFeatureEntity> read(LessonWindowFeatureRepository repository,
                                                String lessonSessionId,
                                                String videoJobId,
                                                String audioJobId,
                                                boolean allowHistoricalFallback) {
        List<LessonWindowFeatureEntity> versioned =
                repository.findByLessonSessionIdAndJobIdsOrderByWindowIndexAsc(
                        lessonSessionId, normalize(videoJobId), normalize(audioJobId));
        if (versioned != null && !versioned.isEmpty()) {
            return versioned;
        }
        if (!allowHistoricalFallback) {
            // 已选定分析任务但精确版本没有窗口：数据不足，禁止用旧历史行补分。
            return List.of();
        }
        List<LessonWindowFeatureEntity> all =
                repository.findByLessonSessionIdOrderByWindowIndexAsc(lessonSessionId);
        if (all == null) {
            return List.of();
        }
        return all.stream()
                .filter(w -> NO_JOB.equals(normalize(w.getVideoJobId()))
                        && NO_JOB.equals(normalize(w.getAudioJobId())))
                .toList();
    }
}
