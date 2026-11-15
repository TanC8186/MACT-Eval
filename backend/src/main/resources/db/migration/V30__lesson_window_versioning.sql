-- 窗口特征按分析版本保存（R04）
-- 版本: V30
--
-- 背景：alignLesson 原先按课堂整表删除重建（deleteByLessonSessionId），
-- 于是「J1 早先 PARTIAL、T2 才补偿」的对齐会覆盖「J2 在 T1 已写入」的窗口；
-- 报告按“最新成功任务”读取，结果截图/任务号来自 J2、分数来自 J1。
-- 修复：窗口行记录产生它的视频/音频任务 id，行的身份变为
-- (lesson_session_id, video_job_id, audio_job_id, window_index)，
-- 对齐只重建自己版本组合的行，报告按选定版本精确读取。
--
-- 兼容：历史行两列为 NULL；唯一索引用 COALESCE(..., '') 归一为空串，
-- 与旧 uq_lesson_window（每课堂每窗口一行）语义一致，无需回填历史数据。
--
-- 只做增量，不修改 V1–V29。

ALTER TABLE lesson_window_features
    ADD COLUMN IF NOT EXISTS video_job_id VARCHAR(36),
    ADD COLUMN IF NOT EXISTS audio_job_id VARCHAR(36);

-- 旧的全课堂唯一约束让位于“按版本”唯一性。
ALTER TABLE lesson_window_features DROP CONSTRAINT IF EXISTS uq_lesson_window;

CREATE UNIQUE INDEX IF NOT EXISTS uq_lesson_window_version
    ON lesson_window_features (lesson_session_id, COALESCE(video_job_id, ''), COALESCE(audio_job_id, ''), window_index);

-- 按课堂读取（报告/窗口列表）仍需要索引；V18 已建过，这里显式保证存在。
CREATE INDEX IF NOT EXISTS idx_lesson_window_session
    ON lesson_window_features (lesson_session_id);

COMMENT ON COLUMN lesson_window_features.video_job_id IS
    '产生该窗口的视频分析任务；空串/ NULL 表示该版本视频模态无任务（历史行）';
COMMENT ON COLUMN lesson_window_features.audio_job_id IS
    '产生该窗口的音频分析任务；空串/ NULL 表示该版本语音模态无任务（历史行）';
