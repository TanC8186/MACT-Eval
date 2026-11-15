-- 初始化课堂评价系统数据库表

CREATE TABLE IF NOT EXISTS evaluation_reports (
    id              VARCHAR(36) PRIMARY KEY,
    lesson_id       VARCHAR(100) NOT NULL UNIQUE,
    course_name     VARCHAR(200),
    teacher_name    VARCHAR(100),
    engagement_score DOUBLE PRECISION,
    teaching_score  DOUBLE PRECISION,
    climate_score   DOUBLE PRECISION,
    timeline_json   TEXT,
    suggestions_json TEXT,
    raw_report      TEXT,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_evaluation_reports_created_at ON evaluation_reports(created_at DESC);

CREATE TABLE IF NOT EXISTS classroom_configs (
    classroom_id        VARCHAR(100) PRIMARY KEY,
    name                VARCHAR(200) NOT NULL,
    rows_count          INTEGER DEFAULT 8,
    cols_count          INTEGER DEFAULT 6,
    camera_config_json  TEXT,
    seat_config_json    TEXT,
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS students (
    student_id      VARCHAR(50) PRIMARY KEY,
    name            VARCHAR(100) NOT NULL,
    seat_row        INTEGER,
    seat_col        INTEGER,
    classroom_id    VARCHAR(100),
    engagement_level VARCHAR(20) DEFAULT 'unknown'
);

CREATE INDEX idx_students_classroom ON students(classroom_id);
