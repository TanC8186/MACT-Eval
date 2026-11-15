CREATE TABLE lesson_transcripts (
    id                   VARCHAR(36) PRIMARY KEY,
    lesson_session_id    VARCHAR(36) NOT NULL,
    job_id               VARCHAR(36),
    text                 TEXT        NOT NULL,
    language             VARCHAR(20),
    language_probability DOUBLE PRECISION,
    segments             TEXT,
    model_version        VARCHAR(100),
    created_at           TIMESTAMP   NOT NULL
);

CREATE INDEX idx_transcript_lesson ON lesson_transcripts (lesson_session_id);
