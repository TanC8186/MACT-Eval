-- MACT-Eval 业务不变量、报告版本与可靠任务
-- 版本: V23
--
-- 上线前只读审计（2026-10-25 已执行，结果均为 0 行/0 条）：
--   SELECT lesson_session_id, student_id, count(*) FROM attendance_records
--     GROUP BY 1,2 HAVING count(*) > 1;
--   SELECT count(*) FROM evaluation_reports r
--     LEFT JOIN lesson_sessions s ON s.id = r.lesson_session_id
--     WHERE r.lesson_session_id IS NOT NULL AND s.id IS NULL;
-- 若其他环境审计发现重复/孤儿，本迁移会在建唯一索引处失败，必须先按
-- docs/reviews/2026-10-25-remediation-plan.md 的审计步骤合并后再上线。

-- ============================================================
-- 1. 强制首次改密
-- ============================================================
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS password_changed_at TIMESTAMP;

-- ============================================================
-- 2. 课堂实际时间与并发版本
-- ============================================================
ALTER TABLE lesson_sessions
    ADD COLUMN IF NOT EXISTS actual_start_time TIMESTAMP,
    ADD COLUMN IF NOT EXISTS actual_end_time TIMESTAMP,
    ADD COLUMN IF NOT EXISTS version BIGINT NOT NULL DEFAULT 0;

-- ============================================================
-- 3. 考勤业务唯一约束（并发/重复初始化的最终防线）
-- ============================================================
CREATE UNIQUE INDEX IF NOT EXISTS uq_attendance_lesson_student
    ON attendance_records(lesson_session_id, student_id);

-- ============================================================
-- 4. 报告版本与可追溯信息
-- ============================================================
ALTER TABLE evaluation_reports
    ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS published_by VARCHAR(36),
    ADD COLUMN IF NOT EXISTS analysis_video_job_id VARCHAR(36),
    ADD COLUMN IF NOT EXISTS analysis_audio_job_id VARCHAR(36),
    ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64);

-- 已发布版本的内容快照：重新生成只改 working row，历史发布内容从快照读取。
CREATE TABLE IF NOT EXISTS evaluation_report_versions (
    id                  VARCHAR(36) PRIMARY KEY,
    report_id           VARCHAR(36) NOT NULL,
    lesson_session_id   VARCHAR(36) NOT NULL,
    version             INTEGER NOT NULL,
    status              VARCHAR(20) NOT NULL,
    snapshot_json       TEXT NOT NULL,
    content_hash        VARCHAR(64),
    analysis_video_job_id VARCHAR(36),
    analysis_audio_job_id VARCHAR(36),
    published_by        VARCHAR(36),
    published_at        TIMESTAMP,
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_report_version UNIQUE (lesson_session_id, version)
);

CREATE INDEX IF NOT EXISTS idx_report_version_lesson
    ON evaluation_report_versions(lesson_session_id, version);

-- ============================================================
-- 5. 教学建议证据与规则版本
-- ============================================================
ALTER TABLE suggestions
    ADD COLUMN IF NOT EXISTS rule_id VARCHAR(80),
    ADD COLUMN IF NOT EXISTS rule_version VARCHAR(20),
    ADD COLUMN IF NOT EXISTS evidence_refs TEXT,
    ADD COLUMN IF NOT EXISTS report_version INTEGER,
    ADD COLUMN IF NOT EXISTS actor_id VARCHAR(36),
    ADD COLUMN IF NOT EXISTS status_changed_at TIMESTAMP;

-- ============================================================
-- 5b. 转写媒体覆盖与模型追溯（AI05/AI06）
-- ============================================================
ALTER TABLE lesson_transcripts
    ADD COLUMN IF NOT EXISTS media_duration_ms BIGINT,
    ADD COLUMN IF NOT EXISTS coverage_start_ms BIGINT,
    ADD COLUMN IF NOT EXISTS coverage_end_ms BIGINT,
    ADD COLUMN IF NOT EXISTS model_digest VARCHAR(64),
    ADD COLUMN IF NOT EXISTS config_json TEXT;

-- ============================================================
-- 6. 任务租约、心跳与可恢复投递（AI03/AI04）
-- ============================================================
ALTER TABLE analysis_jobs
    ADD COLUMN IF NOT EXISTS attempt INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS lease_until TIMESTAMP,
    ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(255),
    ADD COLUMN IF NOT EXISTS model_digest VARCHAR(64),
    ADD COLUMN IF NOT EXISTS config_json TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_job_idempotency
    ON analysis_jobs(idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS task_outbox (
    id              VARCHAR(36) PRIMARY KEY,
    aggregate_id    VARCHAR(36) NOT NULL,
    event_type      VARCHAR(40) NOT NULL,
    payload         TEXT NOT NULL,
    attempt         INTEGER NOT NULL DEFAULT 0,
    next_retry_at   TIMESTAMP NOT NULL DEFAULT NOW(),
    published_at    TIMESTAMP,
    last_error      TEXT,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON task_outbox(published_at, next_retry_at);
