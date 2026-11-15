-- 任务业务落库 / 窗口对齐阶段（R01 / R03）
-- 版本: V32
--
-- R01：PARTIAL 既可能是“落库失败可补偿”，也可能是“AI 合法返回 partial/NO_WINDOWS”。
--      两者共用一个状态位就会被补偿器误升成 SUCCEEDED，因此单独记录落库阶段。
-- R03：观测已落库但对齐失败时，任务仍需保持 SUCCEEDED（报告要引用它的
--      analysisVideoJobId），只有独立的对齐阶段能让恢复器发现并只重建派生窗口。
--
-- ingest_state:    NULL=历史行/不适用；PENDING=待补偿；INGESTED=已完成；EXHAUSTED=耗尽
-- alignment_state: NULL=不适用；PENDING/ DONE / FAILED
--
-- 只做增量，不修改 V1–V31。

ALTER TABLE analysis_jobs
    ADD COLUMN IF NOT EXISTS ingest_state VARCHAR(20);
ALTER TABLE analysis_jobs
    ADD COLUMN IF NOT EXISTS alignment_state VARCHAR(20);
ALTER TABLE analysis_jobs
    ADD COLUMN IF NOT EXISTS alignment_attempts INTEGER NOT NULL DEFAULT 0;

-- 补偿扫描按 ingest_state 过滤，对齐扫描按 alignment_state 过滤。
CREATE INDEX IF NOT EXISTS idx_job_ingest_pending
    ON analysis_jobs (next_recovery_at)
    WHERE ingest_state = 'PENDING';

CREATE INDEX IF NOT EXISTS idx_job_alignment_pending
    ON analysis_jobs (next_recovery_at)
    WHERE alignment_state = 'PENDING';

-- 历史 PARTIAL 行不回填：无法静态判定它是落库失败还是 AI 合法的 partial，
-- 宁可让它们保持“不适用”（不会被补偿器升级），也不能凭猜测改写语义。

COMMENT ON COLUMN analysis_jobs.ingest_state IS
    '业务落库阶段：NULL=不适用；PENDING=待补偿；INGESTED=已完成；EXHAUSTED=补偿耗尽';
COMMENT ON COLUMN analysis_jobs.alignment_state IS
    '窗口对齐阶段：NULL=不适用；PENDING=观测已落库待对齐；DONE；FAILED';
COMMENT ON COLUMN analysis_jobs.alignment_attempts IS
    '窗口对齐补偿已尝试次数；达到上限后 alignment_state=FAILED';
