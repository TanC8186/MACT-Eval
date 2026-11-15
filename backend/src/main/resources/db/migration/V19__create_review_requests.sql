-- 复核请求表（替代内存存储，支持持久化查询与审计）
CREATE TABLE review_requests (
    id               VARCHAR(36) PRIMARY KEY,
    submission_id    VARCHAR(36) NOT NULL,
    student_id       VARCHAR(36) NOT NULL,
    reason           TEXT NOT NULL,
    status           VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    created_at       TIMESTAMP NOT NULL,
    resolved_at      TIMESTAMP,
    teacher_response TEXT
);

CREATE INDEX idx_review_submission ON review_requests (submission_id);
CREATE INDEX idx_review_status ON review_requests (status);