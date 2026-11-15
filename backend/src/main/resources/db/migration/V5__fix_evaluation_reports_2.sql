-- MACT-Eval 数据库修复：补充 evaluation_reports 所有缺失列
-- 版本: V5

ALTER TABLE evaluation_reports
    ADD COLUMN IF NOT EXISTS title VARCHAR(200) NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'draft',
    ADD COLUMN IF NOT EXISTS suggestions TEXT;
