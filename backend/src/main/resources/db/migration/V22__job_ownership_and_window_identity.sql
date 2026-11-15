-- MACT-Eval 任务归属与窗口身份
-- 版本: V22
--
-- 目标：
--   1. 分析任务记录发起教师与课堂，任务列表/详情可按属主过滤（BE01）。
--   2. 行为观测记录 job_id + window_index，作为重复/并发回调的幂等键（AI02）。
--   3. 转写记录按 job_id 唯一，避免同一任务回调重放写入多行。
--
-- 兼容性：历史任务的 owner_id/lesson_session_id、历史观测的 job_id 允许为 NULL；
-- 唯一索引只作用于非 NULL 的新数据，因此无需清理历史行即可上线。

ALTER TABLE analysis_jobs
    ADD COLUMN IF NOT EXISTS owner_id VARCHAR(36),
    ADD COLUMN IF NOT EXISTS lesson_session_id VARCHAR(36);

CREATE INDEX IF NOT EXISTS idx_job_owner_created ON analysis_jobs(owner_id, created_at);
CREATE INDEX IF NOT EXISTS idx_job_lesson ON analysis_jobs(lesson_session_id);

ALTER TABLE behavior_observations
    ADD COLUMN IF NOT EXISTS job_id VARCHAR(36),
    ADD COLUMN IF NOT EXISTS window_index INTEGER,
    ADD COLUMN IF NOT EXISTS window_start_offset_ms BIGINT,
    ADD COLUMN IF NOT EXISTS window_end_offset_ms BIGINT,
    ADD COLUMN IF NOT EXISTS media_anchor TIMESTAMP;

-- 同一分析任务同一窗口只允许一条观测。partial index 排除历史 NULL 行。
CREATE UNIQUE INDEX IF NOT EXISTS uq_behavior_job_window
    ON behavior_observations(job_id, window_index)
    WHERE job_id IS NOT NULL AND window_index IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_behavior_job ON behavior_observations(job_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_transcript_job
    ON lesson_transcripts(job_id)
    WHERE job_id IS NOT NULL;
