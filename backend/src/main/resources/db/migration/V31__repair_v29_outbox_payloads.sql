-- 修复 V29 历史回填写入的坏 outbox 事件（R02）
-- 版本: V31
--
-- 背景：V29 为「RUNNING 且 attempt=0」的回收行补建 outbox 事件时，直接把
-- analysis_jobs.params 当作 payload 写入。params 只含业务参数，缺少
-- id/type/priority 的封装（见 AnalysisJobService.createJob 的入队契约），
-- worker 因此可能把 Redis message ID 当作 jobId，claim 得到 404；
-- QUEUED 补偿又发送同一条坏消息，任务永远无法完成。
--
-- 处理范围：只修 last_error = 'inserted by V29 backfill' 的行，即
-- **确定由 V29 插入**的那批历史事件；不动其它 QUEUED 任务、不动已正确的事件。
--
-- 逐行 EXCEPTION 保护：单行 params 不是合法 JSON 对象时跳过该行并 RAISE NOTICE，
-- 整条迁移仍然成功（否则一处脏数据会让全部后续迁移无法执行）。
--
-- 不修改 V29 的内容（Flyway 校验和不变）。

DO $$
DECLARE
    r RECORD;
    repaired INTEGER := 0;
    skipped  INTEGER := 0;
BEGIN
    FOR r IN
        SELECT o.id AS outbox_id, j.id AS job_id, j.type, j.priority, j.params
          FROM task_outbox o
          JOIN analysis_jobs j ON j.id = o.aggregate_id
         WHERE o.last_error = 'inserted by V29 backfill'
    LOOP
        BEGIN
            UPDATE task_outbox o
               SET payload = (
                       jsonb_build_object(
                           'id', r.job_id,
                           'type', r.type,
                           'priority', r.priority
                       ) || COALESCE(NULLIF(btrim(r.params), '')::jsonb, '{}'::jsonb)
                   )::text,
                   attempt = 0,
                   published_at = NULL,
                   next_retry_at = NOW(),
                   last_error = 'repaired by V31'
             WHERE o.id = r.outbox_id
               AND o.last_error = 'inserted by V29 backfill';
            repaired := repaired + 1;
        EXCEPTION WHEN others THEN
            skipped := skipped + 1;
            RAISE NOTICE 'V31: skipped outbox event % (job %) - %', r.outbox_id, r.job_id, SQLERRM;
        END;
    END LOOP;
    RAISE NOTICE 'V31: repaired % event(s), skipped % event(s)', repaired, skipped;
END $$;

COMMENT ON COLUMN task_outbox.payload IS
    '入队消息 JSON：id/type/priority + 展平的 params；V31 起修复 V29 补建的坏事件';
