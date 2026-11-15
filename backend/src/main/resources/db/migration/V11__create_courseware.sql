CREATE TABLE courseware (
    id              VARCHAR(36)  PRIMARY KEY,
    course_id       VARCHAR(36)  NOT NULL,
    job_id          VARCHAR(36),
    file_name       VARCHAR(500) NOT NULL,
    object_name     VARCHAR(500) NOT NULL,
    bucket          VARCHAR(100) NOT NULL DEFAULT 'teaching-resources',
    content_type    VARCHAR(50),
    status          VARCHAR(20)  NOT NULL,
    objectives      TEXT,
    key_points      TEXT,
    full_text       TEXT,
    word_count      INTEGER,
    paragraph_count INTEGER,
    model_version   VARCHAR(100),
    created_at      TIMESTAMP    NOT NULL,
    updated_at      TIMESTAMP    NOT NULL
);

CREATE UNIQUE INDEX uk_courseware_job_id ON courseware (job_id);
CREATE INDEX idx_courseware_course_id ON courseware (course_id);
