-- MACT-Eval 数据库扩展：新增领域模型表
-- 版本: V2
-- 描述: 新增用户体系、课程、课堂、题目、作业、考勤、行为、反馈、任务等核心表

-- ============================================================
-- 1. 用户体系
-- ============================================================

CREATE TABLE IF NOT EXISTS users (
    id              VARCHAR(36) PRIMARY KEY,
    name            VARCHAR(100) NOT NULL,
    password        VARCHAR(255),
    phone           VARCHAR(50) UNIQUE,
    avatar          VARCHAR(255),
    wechat_open_id  VARCHAR(100),
    wechat_bound    BOOLEAN NOT NULL DEFAULT FALSE,
    role            VARCHAR(20) NOT NULL,
    teacher_id      VARCHAR(50) UNIQUE,
    title           VARCHAR(100),
    department      VARCHAR(200),
    student_id      VARCHAR(50) UNIQUE,
    class_id        VARCHAR(36),
    seat_row        INTEGER,
    seat_col        INTEGER,
    face_consent_granted    BOOLEAN DEFAULT FALSE,
    face_consent_granted_at TIMESTAMP,
    admin_id        VARCHAR(50) UNIQUE,
    mfa_enabled     BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);
CREATE INDEX IF NOT EXISTS idx_users_wechat ON users(wechat_open_id);
CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone);

-- ============================================================
-- 2. 课程与班级
-- ============================================================

CREATE TABLE IF NOT EXISTS courses (
    id              VARCHAR(36) PRIMARY KEY,
    name            VARCHAR(200) NOT NULL,
    code            VARCHAR(50),
    description     TEXT,
    teacher_id      VARCHAR(36) NOT NULL,
    department      VARCHAR(200),
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_courses_teacher ON courses(teacher_id);

CREATE TABLE IF NOT EXISTS class_groups (
    id              VARCHAR(36) PRIMARY KEY,
    name            VARCHAR(200) NOT NULL,
    grade           VARCHAR(50),
    department      VARCHAR(200),
    student_count   INTEGER DEFAULT 0,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

-- ============================================================
-- 3. 课堂会话
-- ============================================================

CREATE TABLE IF NOT EXISTS lesson_sessions (
    id                  VARCHAR(36) PRIMARY KEY,
    course_id           VARCHAR(36) NOT NULL,
    class_id            VARCHAR(36) NOT NULL,
    teacher_id          VARCHAR(36) NOT NULL,
    title               VARCHAR(200) NOT NULL,
    date                TIMESTAMP NOT NULL,
    start_time          TIMESTAMP NOT NULL,
    end_time            TIMESTAMP,
    status              VARCHAR(20) NOT NULL DEFAULT 'SCHEDULED',
    knowledge_point_ids TEXT,
    teaching_objectives TEXT,
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_lesson_course ON lesson_sessions(course_id);
CREATE INDEX IF NOT EXISTS idx_lesson_class ON lesson_sessions(class_id);
CREATE INDEX IF NOT EXISTS idx_lesson_teacher ON lesson_sessions(teacher_id);
CREATE INDEX IF NOT EXISTS idx_lesson_date ON lesson_sessions(date);
CREATE INDEX IF NOT EXISTS idx_lesson_status ON lesson_sessions(status);

CREATE TABLE IF NOT EXISTS activities (
    id                  VARCHAR(36) PRIMARY KEY,
    lesson_session_id   VARCHAR(36) NOT NULL,
    type                VARCHAR(30) NOT NULL,
    label               VARCHAR(200),
    start_time          TIMESTAMP NOT NULL,
    end_time            TIMESTAMP,
    duration            INTEGER,
    created_at          TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_activity_lesson ON activities(lesson_session_id);

-- ============================================================
-- 4. 知识点
-- ============================================================

CREATE TABLE IF NOT EXISTS knowledge_points (
    id              VARCHAR(36) PRIMARY KEY,
    course_id       VARCHAR(36) NOT NULL,
    name            VARCHAR(200) NOT NULL,
    description     TEXT,
    difficulty      VARCHAR(10),
    parent_id       VARCHAR(36),
    "order"         INTEGER NOT NULL,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_kp_course ON knowledge_points(course_id);

-- ============================================================
-- 5. 题目与课堂作答
-- ============================================================

CREATE TABLE IF NOT EXISTS questions (
    id                  VARCHAR(36) PRIMARY KEY,
    course_id           VARCHAR(36) NOT NULL,
    knowledge_point_ids TEXT,
    type                VARCHAR(30) NOT NULL,
    content             TEXT NOT NULL,
    options             TEXT,
    correct_answer      TEXT,
    answer_explanation  TEXT,
    score               DOUBLE PRECISION NOT NULL,
    difficulty          VARCHAR(10),
    created_by          VARCHAR(36) NOT NULL,
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_questions_course ON questions(course_id);
CREATE INDEX IF NOT EXISTS idx_questions_type ON questions(type);

CREATE TABLE IF NOT EXISTS in_class_answers (
    id                  VARCHAR(36) PRIMARY KEY,
    lesson_session_id   VARCHAR(36) NOT NULL,
    question_id         VARCHAR(36) NOT NULL,
    student_id          VARCHAR(36) NOT NULL,
    answer              TEXT NOT NULL,
    is_correct          BOOLEAN,
    score               DOUBLE PRECISION,
    submitted_at        TIMESTAMP NOT NULL DEFAULT NOW(),
    grading_status      VARCHAR(20) DEFAULT 'AUTO_GRADED',
    teacher_score       DOUBLE PRECISION,
    teacher_comment     TEXT
);

CREATE INDEX IF NOT EXISTS idx_answer_lesson_question ON in_class_answers(lesson_session_id, question_id);
CREATE INDEX IF NOT EXISTS idx_answer_student ON in_class_answers(student_id);

-- ============================================================
-- 6. 课后作业与提交
-- ============================================================

CREATE TABLE IF NOT EXISTS assignments (
    id                  VARCHAR(36) PRIMARY KEY,
    lesson_session_id   VARCHAR(36) NOT NULL,
    title               VARCHAR(200) NOT NULL,
    description         TEXT,
    question_ids        TEXT,
    attachments         TEXT,
    due_date            TIMESTAMP NOT NULL,
    status              VARCHAR(20) NOT NULL DEFAULT 'DRAFT',
    total_score         DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_assignments_lesson ON assignments(lesson_session_id);

CREATE TABLE IF NOT EXISTS submissions (
    id                  VARCHAR(36) PRIMARY KEY,
    assignment_id       VARCHAR(36) NOT NULL,
    student_id          VARCHAR(36) NOT NULL,
    answers             TEXT,
    attachments         TEXT,
    submitted_at        TIMESTAMP,
    status              VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    total_score         DOUBLE PRECISION,
    teacher_comment     TEXT,
    ai_grading_draft    TEXT,
    teacher_confirmed   BOOLEAN NOT NULL DEFAULT FALSE,
    teacher_confirmed_at TIMESTAMP,
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_submission_assignment ON submissions(assignment_id);
CREATE INDEX IF NOT EXISTS idx_submission_student ON submissions(student_id);

-- ============================================================
-- 7. 考勤记录
-- ============================================================

CREATE TABLE IF NOT EXISTS attendance_records (
    id                  VARCHAR(36) PRIMARY KEY,
    lesson_session_id   VARCHAR(36) NOT NULL,
    student_id          VARCHAR(36) NOT NULL,
    status              VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    source              VARCHAR(30) NOT NULL,
    camera_confidence   DOUBLE PRECISION,
    camera_visible      BOOLEAN,
    checkin_time        TIMESTAMP,
    teacher_confirmed   BOOLEAN NOT NULL DEFAULT FALSE,
    teacher_confirmed_at TIMESTAMP,
    teacher_comment     TEXT,
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_attendance_lesson ON attendance_records(lesson_session_id);
CREATE INDEX IF NOT EXISTS idx_attendance_student ON attendance_records(student_id);

-- ============================================================
-- 8. 行为观察
-- ============================================================

CREATE TABLE IF NOT EXISTS behavior_observations (
    id                      VARCHAR(36) PRIMARY KEY,
    lesson_session_id       VARCHAR(36) NOT NULL,
    activity_id             VARCHAR(36),
    window_start            TIMESTAMP NOT NULL,
    window_end              TIMESTAMP NOT NULL,
    visible_seat_count      INTEGER,
    total_configured_seats  INTEGER,
    coverage                DOUBLE PRECISION,
    data_quality            VARCHAR(20),
    in_seat_count           INTEGER,
    leave_seat_count        INTEGER,
    hand_raised_count       INTEGER,
    standing_count          INTEGER,
    upright_posture_count   INTEGER,
    aggregated_upright_proportion VARCHAR(20),
    created_at              TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_behavior_lesson ON behavior_observations(lesson_session_id);

-- ============================================================
-- 9. 学生反馈
-- ============================================================

CREATE TABLE IF NOT EXISTS feedback_entries (
    id                  VARCHAR(36) PRIMARY KEY,
    lesson_session_id   VARCHAR(36) NOT NULL,
    type                VARCHAR(20) NOT NULL,
    student_id          VARCHAR(36),
    content             TEXT NOT NULL,
    theme               VARCHAR(100),
    emotion_tag         VARCHAR(50),
    status              VARCHAR(20) NOT NULL DEFAULT 'SUBMITTED',
    teacher_reply       TEXT,
    teacher_replied_at  TIMESTAMP,
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_feedback_lesson ON feedback_entries(lesson_session_id);
CREATE INDEX IF NOT EXISTS idx_feedback_type ON feedback_entries(type);

-- ============================================================
-- 10. 分析任务
-- ============================================================

CREATE TABLE IF NOT EXISTS analysis_jobs (
    id              VARCHAR(36) PRIMARY KEY,
    type            VARCHAR(30) NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'QUEUED',
    priority        VARCHAR(10) NOT NULL DEFAULT 'NORMAL',
    progress        INTEGER DEFAULT 0,
    source_hash     VARCHAR(64),
    params          TEXT,
    result_metadata TEXT,
    error_message   TEXT,
    retry_count     INTEGER DEFAULT 0,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    started_at      TIMESTAMP,
    completed_at    TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_job_status ON analysis_jobs(status);
CREATE INDEX IF NOT EXISTS idx_job_type_status ON analysis_jobs(type, status);

-- ============================================================
-- 11. 教学建议
-- ============================================================

CREATE TABLE IF NOT EXISTS suggestions (
    id              VARCHAR(36) PRIMARY KEY,
    lesson_session_id VARCHAR(36),
    report_id       VARCHAR(36),
    category        VARCHAR(100),
    priority        VARCHAR(10) NOT NULL DEFAULT 'MEDIUM',
    content         TEXT NOT NULL,
    evidence        TEXT,
    status          VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    accepted_at     TIMESTAMP,
    ignored_at      TIMESTAMP,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_suggestions_lesson ON suggestions(lesson_session_id);
CREATE INDEX IF NOT EXISTS idx_suggestions_status ON suggestions(status);

-- ============================================================
-- 12. 更新 evaluation_reports 表添加新字段
-- ============================================================

ALTER TABLE evaluation_reports
    ADD COLUMN IF NOT EXISTS lesson_session_id VARCHAR(36),
    ADD COLUMN IF NOT EXISTS course_id VARCHAR(36),
    ADD COLUMN IF NOT EXISTS class_id VARCHAR(36),
    ADD COLUMN IF NOT EXISTS teacher_id VARCHAR(36),
    ADD COLUMN IF NOT EXISTS summary TEXT,
    ADD COLUMN IF NOT EXISTS data_coverage TEXT,
    ADD COLUMN IF NOT EXISTS attendance_summary TEXT,
    ADD COLUMN IF NOT EXISTS behavior_summary TEXT,
    ADD COLUMN IF NOT EXISTS quiz_statistics TEXT,
    ADD COLUMN IF NOT EXISTS assignment_summary TEXT,
    ADD COLUMN IF NOT EXISTS feedback_summary TEXT;

CREATE INDEX IF NOT EXISTS idx_report_lesson ON evaluation_reports(lesson_session_id);
