-- MACT-Eval 数据库修复：补充 evaluation_reports 缺失列
-- 版本: V4

ALTER TABLE evaluation_reports
    ADD COLUMN IF NOT EXISTS published_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS engagement_score DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS teaching_score DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS climate_score DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS data_coverage TEXT,
    ADD COLUMN IF NOT EXISTS attendance_summary TEXT,
    ADD COLUMN IF NOT EXISTS behavior_summary TEXT,
    ADD COLUMN IF NOT EXISTS quiz_statistics TEXT,
    ADD COLUMN IF NOT EXISTS assignment_summary TEXT,
    ADD COLUMN IF NOT EXISTS feedback_summary TEXT;
