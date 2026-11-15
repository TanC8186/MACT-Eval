-- 任务结果交付与恢复协议（见 docs/operations/result-delivery-and-recovery-protocol.md）
-- 版本: V28
--
-- 只做增量：不修改 V1–V27 已执行的迁移。
--
-- 1) recovery_count：RUNNING 过期租约被回收的次数；达到上限后必须进入 FAILED，
--    不能无限重排或永久 RUNNING。
-- 2) recovery_checked_at：最近一次恢复探测时间（含存储不可达导致的延期）。
-- 3) next_recovery_at：存储不可达时的延期检查时间；为 NULL 表示立即可查。
-- 4) last_result_ref：最近一次已持久化结果的引用（bucket/key/sha256/bytes），
--    仅审计用；不存签名 URL、不存媒体内容。
-- 5) 部分索引：只覆盖 RUNNING 且带租约的行，避免全表扫描。

ALTER TABLE analysis_jobs
    ADD COLUMN IF NOT EXISTS recovery_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE analysis_jobs
    ADD COLUMN IF NOT EXISTS recovery_checked_at TIMESTAMP;
ALTER TABLE analysis_jobs
    ADD COLUMN IF NOT EXISTS next_recovery_at TIMESTAMP;
ALTER TABLE analysis_jobs
    ADD COLUMN IF NOT EXISTS last_result_ref TEXT;

CREATE INDEX IF NOT EXISTS idx_job_running_lease
    ON analysis_jobs (lease_until)
    WHERE status = 'RUNNING';

COMMENT ON COLUMN analysis_jobs.recovery_count IS
    'RUNNING 过期租约被数据库回收器重排的次数；达到 mact.recovery.max-attempts 后 FAILED';
COMMENT ON COLUMN analysis_jobs.recovery_checked_at IS
    '最近一次恢复探测时间（含结果存储不可达的延期）';
COMMENT ON COLUMN analysis_jobs.next_recovery_at IS
    'NULL=立即可扫描；非 NULL=在此之前跳过（存储不可达退避）';
COMMENT ON COLUMN analysis_jobs.last_result_ref IS
    '已持久化结果引用 JSON（bucket/key/sha256/bytes），不含签名 URL';
