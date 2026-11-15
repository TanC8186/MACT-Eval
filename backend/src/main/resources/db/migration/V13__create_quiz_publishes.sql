CREATE TABLE quiz_publishes (
    id                VARCHAR(36) PRIMARY KEY,
    lesson_session_id VARCHAR(36) NOT NULL,
    question_id       VARCHAR(36) NOT NULL,
    status            VARCHAR(20) NOT NULL,
    published_at      TIMESTAMP   NOT NULL,
    collected_at      TIMESTAMP,
    created_at        TIMESTAMP   NOT NULL
);

CREATE INDEX idx_quiz_publishes_session ON quiz_publishes (lesson_session_id, status);
