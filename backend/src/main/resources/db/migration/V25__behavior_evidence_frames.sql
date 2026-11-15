-- MACT-Eval 行为识别证据帧
-- 版本: V25
--
-- AI worker 为每个分析窗口保留最多 2 张可人工复核的画面（课堂全景 + 异常状态）。
-- 对象存放在 MinIO analysis-artifacts 私有桶，后端按属主签发短期下载 URL。
-- 唯一键保证同一任务/窗口/标签的视频重放不会重复建行。

CREATE TABLE IF NOT EXISTS behavior_evidence_frames (
    id                  VARCHAR(36) PRIMARY KEY,
    lesson_session_id   VARCHAR(36) NOT NULL,
    job_id              VARCHAR(36) NOT NULL,
    window_index        INTEGER NOT NULL,
    timestamp_ms        BIGINT,
    bucket              VARCHAR(64) NOT NULL,
    object_key          VARCHAR(255) NOT NULL,
    code                VARCHAR(30) NOT NULL,
    label               VARCHAR(50),
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_evidence_frame UNIQUE (job_id, window_index, code)
);

CREATE INDEX IF NOT EXISTS idx_evidence_lesson_window
    ON behavior_evidence_frames(lesson_session_id, window_index);
CREATE INDEX IF NOT EXISTS idx_evidence_job
    ON behavior_evidence_frames(job_id);
CREATE INDEX IF NOT EXISTS idx_evidence_created
    ON behavior_evidence_frames(created_at);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_evidence_lesson') THEN
        ALTER TABLE behavior_evidence_frames
            ADD CONSTRAINT fk_evidence_lesson FOREIGN KEY (lesson_session_id)
            REFERENCES lesson_sessions(id) ON DELETE RESTRICT;
    END IF;
END $$;
