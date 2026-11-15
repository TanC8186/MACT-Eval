-- MACT-Eval 数据库演进：教学建议采纳状态
-- 版本: V8
--
-- 背景:
--   教学效果追踪闭环需要在建议被教师采纳时记录采纳时间，
--   用于计算"干预前基线 vs 干预后"的维度分数对比。
--   status 取值约定: pending(未处理) / adopted(已采纳) / dismissed(已忽略)。
--   保留既有 accepted_at / ignored_at 列以兼容原 accept/ignore 端点。

ALTER TABLE suggestions ADD COLUMN IF NOT EXISTS adopted_at TIMESTAMP;

CREATE INDEX IF NOT EXISTS idx_suggestions_status_adopted
    ON suggestions(status, adopted_at);
