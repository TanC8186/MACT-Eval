CREATE TABLE courseware_analysis (
    id            VARCHAR(36)  PRIMARY KEY,
    courseware_id VARCHAR(36)  NOT NULL,
    status        VARCHAR(20)  NOT NULL,
    dimensions    TEXT,
    suggestions   TEXT,
    strengths     TEXT,
    raw_llm_text  TEXT,
    error_message TEXT,
    created_at    TIMESTAMP    NOT NULL,
    updated_at    TIMESTAMP    NOT NULL
);

CREATE INDEX idx_courseware_analysis_courseware ON courseware_analysis (courseware_id);
