-- 收敛历史异常行，避免再依赖运行时兜底（独立审查 A 的 F3 与覆盖缺口 1）。
-- 版本: V29
--
-- 只做**能明确判定归属**的收敛，不猜测、不删除业务数据：
--   1) RUNNING 且 lease_until IS NULL 且 attempt >= 1：
--      claim 对这类行返回 recovery_pending（协议禁止在那里推进 attempt），
--      而旧 findRecoverable 又带 lease_until IS NOT NULL，导致两端都不处理。
--      回收器已改为能选中 NULL 租约行；这里把「最后一次已知活动」回填到
--      heartbeat_at，使回收器有稳定锚点，不再依赖 coalesce 兜底。
--      不改状态、不改 attempt。
--   2) RUNNING 且 attempt = 0：从未真正开始执行（claim 一定先写 attempt+1），
--      只可能是历史脏数据或手工重置残留 → 回收到 QUEUED 并重置 outbox，
--      让正常队列重新投递。用 CTE 精确捕获受影响的行，避免用字符串匹配。
--   3) started_at 为 NULL 的 RUNNING 行用 created_at 回填，供告警阈值使用。
--
-- 不修改 V1–V28。本迁移可重复执行：所有 UPDATE 都带可重入条件，
-- 第二次执行时 affected 行数为 0。

-- 1) NULL 租约的锚点回填
UPDATE analysis_jobs
   SET heartbeat_at = COALESCE(heartbeat_at, started_at, created_at)
 WHERE status = 'RUNNING'
   AND lease_until IS NULL
   AND attempt >= 1;

-- 3) 告警阈值的开始时间回填（先做，使 2) 的 next_recovery_at 语义一致）
UPDATE analysis_jobs
   SET started_at = COALESCE(started_at, created_at)
 WHERE status = 'RUNNING'
   AND started_at IS NULL;

-- 2) attempt=0 的 RUNNING 行回收到 QUEUED，并在同一语句里记录被回收的 id
WITH requeued AS (
    UPDATE analysis_jobs
       SET status = 'QUEUED',
           lease_until = NULL,
           heartbeat_at = NULL,
           next_recovery_at = NULL,
           recovery_checked_at = NOW(),
           recovery_count = COALESCE(recovery_count, 0) + 1,
           error_message = 'requeued: RUNNING with attempt=0 (V29 backfill)'
     WHERE status = 'RUNNING'
       AND attempt = 0
     RETURNING id, heartbeat_at AS ignored
)
UPDATE task_outbox o
   SET published_at = NULL,
       attempt = 0,
       next_retry_at = NOW(),
       last_error = 'requeued by V29 backfill'
  FROM requeued r
 WHERE o.aggregate_id = r.id;

-- 上面那一步只重置了**已存在**的 outbox 事件。对没有事件的回收行补一条，
-- 否则任务会停在 QUEUED 无人投递。
INSERT INTO task_outbox (id, aggregate_id, event_type, payload, attempt,
                         next_retry_at, published_at, last_error, created_at)
SELECT md5(j.id || ':V29'), j.id, 'ANALYSIS_JOB_QUEUED', j.params, 0,
       NOW(), NULL, 'inserted by V29 backfill', NOW()
  FROM analysis_jobs j
 WHERE j.status = 'QUEUED'
   AND j.recovery_checked_at IS NOT NULL
   AND j.error_message = 'requeued: RUNNING with attempt=0 (V29 backfill)'
   AND NOT EXISTS (SELECT 1 FROM task_outbox o WHERE o.aggregate_id = j.id);

COMMENT ON COLUMN analysis_jobs.heartbeat_at IS
    '最近一次心跳；V29 起对历史 NULL 租约行用 started_at/created_at 回填，供回收器比较';
