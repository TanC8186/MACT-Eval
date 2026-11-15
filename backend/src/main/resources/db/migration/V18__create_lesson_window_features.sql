-- 统一时间窗对齐的窗口级特征（导师建议：统一时间窗粒度对齐各模态）
CREATE TABLE lesson_window_features (
    id VARCHAR(64) PRIMARY KEY,
    lesson_session_id VARCHAR(64) NOT NULL,
    window_index INT NOT NULL,
    window_start TIMESTAMP NOT NULL,
    window_end TIMESTAMP NOT NULL,
    video_score DOUBLE PRECISION,
    speech_score DOUBLE PRECISION,
    business_score DOUBLE PRECISION,
    fused_score DOUBLE PRECISION,
    video_features TEXT,
    speech_features TEXT,
    business_features TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    CONSTRAINT uq_lesson_window UNIQUE (lesson_session_id, window_index)
);
CREATE INDEX idx_lesson_window_session ON lesson_window_features (lesson_session_id);
