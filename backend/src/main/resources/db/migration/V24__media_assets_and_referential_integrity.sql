-- MACT-Eval 媒体资产、报告追溯与外键完整性
-- 版本: V24
--
-- 上线前只读审计（2026-10-25 已执行，dev 与隔离库均为 0 孤儿）：
--   lesson/course/class/teacher、attendance、behavior、transcript、window、
--   report、suggestion、student-class、course-teacher 关联均无孤儿行。
-- 其他环境若存在孤儿，本迁移会在 ADD CONSTRAINT 处失败；必须先按
-- docs/reviews/2026-10-25-remediation-plan.md 的审计/清理步骤处理。

-- ============================================================
-- 1. 报告可追溯信息（媒体摘要、模型摘要、运行配置）
-- ============================================================
ALTER TABLE evaluation_reports
    ADD COLUMN IF NOT EXISTS traceability_json TEXT;

-- ============================================================
-- 2. 媒体资产：上传归属、配额与生命周期
-- ============================================================
CREATE TABLE IF NOT EXISTS media_assets (
    id                  VARCHAR(36) PRIMARY KEY,
    owner_id            VARCHAR(36) NOT NULL,
    lesson_session_id   VARCHAR(36) NOT NULL,
    bucket              VARCHAR(64) NOT NULL,
    object_key          VARCHAR(255) NOT NULL,
    original_file_name  VARCHAR(255),
    media_type          VARCHAR(20) NOT NULL,
    content_type        VARCHAR(100),
    size_bytes          BIGINT,
    etag                VARCHAR(100),
    sha256              VARCHAR(64),
    status              VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    expires_at          TIMESTAMP,
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_media_object UNIQUE (bucket, object_key)
);

CREATE INDEX IF NOT EXISTS idx_media_owner ON media_assets(owner_id);
CREATE INDEX IF NOT EXISTS idx_media_lesson ON media_assets(lesson_session_id);
CREATE INDEX IF NOT EXISTS idx_media_expiry ON media_assets(status, expires_at);

-- ============================================================
-- 3. 外键：业务记录不得指向已不存在的课程/班级/课堂/用户
-- ============================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_lesson_course') THEN
        ALTER TABLE lesson_sessions
            ADD CONSTRAINT fk_lesson_course FOREIGN KEY (course_id)
            REFERENCES courses(id) ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_lesson_class') THEN
        ALTER TABLE lesson_sessions
            ADD CONSTRAINT fk_lesson_class FOREIGN KEY (class_id)
            REFERENCES class_groups(id) ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_lesson_teacher') THEN
        ALTER TABLE lesson_sessions
            ADD CONSTRAINT fk_lesson_teacher FOREIGN KEY (teacher_id)
            REFERENCES users(id) ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_attendance_lesson') THEN
        ALTER TABLE attendance_records
            ADD CONSTRAINT fk_attendance_lesson FOREIGN KEY (lesson_session_id)
            REFERENCES lesson_sessions(id) ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_attendance_student') THEN
        ALTER TABLE attendance_records
            ADD CONSTRAINT fk_attendance_student FOREIGN KEY (student_id)
            REFERENCES users(id) ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_behavior_lesson') THEN
        ALTER TABLE behavior_observations
            ADD CONSTRAINT fk_behavior_lesson FOREIGN KEY (lesson_session_id)
            REFERENCES lesson_sessions(id) ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_transcript_lesson') THEN
        ALTER TABLE lesson_transcripts
            ADD CONSTRAINT fk_transcript_lesson FOREIGN KEY (lesson_session_id)
            REFERENCES lesson_sessions(id) ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_window_lesson') THEN
        ALTER TABLE lesson_window_features
            ADD CONSTRAINT fk_window_lesson FOREIGN KEY (lesson_session_id)
            REFERENCES lesson_sessions(id) ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_report_lesson') THEN
        ALTER TABLE evaluation_reports
            ADD CONSTRAINT fk_report_lesson FOREIGN KEY (lesson_session_id)
            REFERENCES lesson_sessions(id) ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_suggestion_lesson') THEN
        ALTER TABLE suggestions
            ADD CONSTRAINT fk_suggestion_lesson FOREIGN KEY (lesson_session_id)
            REFERENCES lesson_sessions(id) ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_media_owner') THEN
        ALTER TABLE media_assets
            ADD CONSTRAINT fk_media_owner FOREIGN KEY (owner_id)
            REFERENCES users(id) ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_media_lesson') THEN
        ALTER TABLE media_assets
            ADD CONSTRAINT fk_media_lesson FOREIGN KEY (lesson_session_id)
            REFERENCES lesson_sessions(id) ON DELETE RESTRICT;
    END IF;
END $$;

-- ============================================================
-- 4. CHECK 约束：时间窗与枚举值
-- ============================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_behavior_window_order') THEN
        ALTER TABLE behavior_observations
            ADD CONSTRAINT ck_behavior_window_order CHECK (window_end > window_start);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_window_order') THEN
        ALTER TABLE lesson_window_features
            ADD CONSTRAINT ck_window_order CHECK (window_end > window_start);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_lesson_status') THEN
        ALTER TABLE lesson_sessions
            ADD CONSTRAINT ck_lesson_status CHECK (status IN
                ('SCHEDULED', 'IN_PROGRESS', 'COMPLETED', 'CANCELLED'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_attendance_status') THEN
        ALTER TABLE attendance_records
            ADD CONSTRAINT ck_attendance_status CHECK (status IN
                ('PENDING', 'PRESENT', 'LATE', 'ABSENT', 'EXCUSED'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_attendance_source') THEN
        ALTER TABLE attendance_records
            ADD CONSTRAINT ck_attendance_source CHECK (source IN
                ('STUDENT_CHECKIN', 'CAMERA_EVIDENCE', 'TEACHER_CORRECTION'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_report_status') THEN
        ALTER TABLE evaluation_reports
            ADD CONSTRAINT ck_report_status CHECK (status IN
                ('draft', 'published', 'superseded', 'archived'));
    END IF;
END $$;

-- ============================================================
-- 5. 查询索引（按实际列表/授权过滤路径）
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_report_teacher_status
    ON evaluation_reports(teacher_id, status);
CREATE INDEX IF NOT EXISTS idx_suggestion_lesson_status
    ON suggestions(lesson_session_id, status);
CREATE INDEX IF NOT EXISTS idx_job_owner_status
    ON analysis_jobs(owner_id, status);
