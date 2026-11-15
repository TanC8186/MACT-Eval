-- 收紧 V29/V31 历史 outbox payload 修复边界，并留下可查询的审计记录（R02 收尾）
-- 版本: V33
--
-- 背景（V31 的实际缺陷，已用真库复现）：
--   V31 用 jsonb_build_object(...) || COALESCE(NULLIF(btrim(params),'')::jsonb,'{}')
--   合并 payload，但没有约束 params 必须是 JSON **对象**：
--     '{"id":"j"}'::jsonb || '[1,2]'::jsonb  -> [{"id":"j"}, 1, 2]   (array)
--     '{"id":"j"}'::jsonb || '"abc"'::jsonb  -> [{"id":"j"}, "abc"]  (array)
--     '{"id":"j"}'::jsonb || '42'::jsonb     -> [{"id":"j"}, 42]     (array)
--     '{"id":"j"}'::jsonb || 'true'::jsonb   -> [{"id":"j"}, true]   (array)
--   这些都是合法 jsonb，jsonb_typeof 返回 'array' 而不是 'object'。
--   TaskOutboxPublisher.publishPending 用 readValue(payload, Map<String,Object>)
--   反序列化，非对象 payload 100% 抛错 → attempt 递增 → 指数退避（上限 60s）→ 永续重试。
--   V31 的 EXCEPTION 分支只 RAISE NOTICE，NOTICE 不是可查询的持久记录，
--   事后无法定位到底哪些事件被跳过。
--
-- 本迁移的四类结果（每一行都必须落一类，不允许静默）：
--   repaired：params 是 JSON 对象，或为 NULL/空白 → 重建
--             = params || {id,type,priority}（信封在右侧，params 无法覆盖它），
--             重置 attempt=0 / published_at=NULL / next_retry_at=NOW()，
--             last_error='repaired by V33'（可发送）。
--   parked  ：params 是合法 JSON 但不是对象（array/string/number/boolean）
--             → 不生成可发送 payload，把 **published_at 也置 NULL** 并把
--             next_retry_at=NOW()+100 years —— 两条运行期重发送入口
--             （findPending / findStalePublishedForQueuedJobs）都选不中；
--             last_error='repair_failed: params is <jsonb_typeof>'。
--   skipped_not_json：params 根本不是合法 JSON、或信封合并后仍不完整
--             （含嵌套过深/数值过长）→ 同样 park，
--             last_error='repair_failed: ...'。
--   error   ：处理该行时抛出非 JSON 类异常 → 同样 park，outcome 单独记为
--             'error'，不混进 skipped_not_json。
--
-- 必须在真实 PostgreSQL 上验证（见 scripts/ops/verify_outbox_repair.py）：
-- jsonb_typeof 的分支行为、CAST 失败时的 EXCEPTION 语义、非对象 payload 的真实形态，
-- 静态 SQL 断言都无法替代。
--
-- 不修改 V1–V32（Flyway 校验和不变）；只新增本文件。
-- 可重复执行：已 repaired 的行 last_error 变为 'repaired by V33'，不在范围内；
-- 已 parked 的行 last_error 变为 'repair_failed: ...'，也不在范围内
-- （范围谓词用 IS DISTINCT FROM，NULL 安全 —— last_error IS NULL 恰恰是
--  publisher 成功投递后的常见形态）；
-- 审计表对 (outbox_id, outcome) 建唯一索引并在插入前 NOT EXISTS 判重。

-- ============================================================
-- 1) 持久化审计表
--    取代 V31 的 RAISE NOTICE：NOTICE 只进日志、不可查询，事后再也找不到
--    被跳过的事件。人工处理 parked / skipped_not_json 行时从这里查。
-- ============================================================
CREATE TABLE IF NOT EXISTS outbox_repair_audit (
    id              BIGSERIAL PRIMARY KEY,
    outbox_id       TEXT        NOT NULL,
    aggregate_id    TEXT,
    outcome         TEXT        NOT NULL,
    detail          TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 幂等性的一部分：同一事件同一结论只允许一条审计记录。
CREATE UNIQUE INDEX IF NOT EXISTS uq_outbox_repair_audit_event
    ON outbox_repair_audit (outbox_id, outcome);

CREATE INDEX IF NOT EXISTS idx_outbox_repair_audit_outcome
    ON outbox_repair_audit (outcome, created_at DESC);

COMMENT ON TABLE outbox_repair_audit IS
    '历史 outbox payload 修复审计（V33 起）：每个在范围内的事件**每个结论**一行——'
    '同一事件在不同次执行中可能先后得到不同结论（例如先 parked，人工恢复后重跑变 '
    'repaired），这是有意的历史轨迹，不是重复记账；'
    'outcome=repaired 表示已生成可发送的 {id,type,priority}+params 对象消息；'
    'outcome=parked / skipped_not_json 表示 params 不是 JSON 对象或非法，'
    '事件已被推到 100 年后不可发送，必须由人工判断该任务是否重跑或作废；'
    '(outbox_id, outcome) 唯一，保证迁移重复执行不会重复记账';

COMMENT ON COLUMN outbox_repair_audit.outcome IS
    'repaired（已重建可发送对象 payload）| parked（params 是数组/字符串/数字/布尔，'
    '不生成 payload，等人工处理）| skipped_not_json（params 非法 JSON 或 analysis_jobs '
    '行缺失）| error（处理该行时抛出非 JSON 类异常，detail 以 unexpected_error: 开头）；'
    'error 与 skipped_not_json 分开记录，避免运维朝错误方向排查';

COMMENT ON COLUMN outbox_repair_audit.detail IS
    '判定依据：params 与既有 payload 的 jsonb_typeof、以及是否触发了非对象 payload 重建';

-- ============================================================
-- 2) payload 新契约（覆盖 V31 写的旧注释）
-- ============================================================
COMMENT ON COLUMN task_outbox.payload IS
    '入队消息 JSON 对象：恰好是 id/type/priority 三个封装键 + analysis_jobs.params 的'
    '顶层键展平（与 AnalysisJobService.createJob 的 message.put 顺序一致，params 覆盖同名键）。'
    'V33 起契约收紧：payload 必须是 jsonb object，发布器用 '
    'readValue(payload, Map<String,Object>) 反序列化，非对象 payload 会永久退避重试；'
    'params 不是 JSON 对象的历史事件一律 park（last_error=repair_failed:...，'
    'next_retry_at 推到 100 年后）并写入 outbox_repair_audit';

-- ============================================================
-- 2b) 嵌套深度辅助函数（仅在本次迁移期间存在）
-- ============================================================
-- 为什么需要它：TaskOutboxPublisher 用 Jackson readValue(..., Map.class)
-- 反序列化 payload，Jackson 默认的 StreamReadConstraints.maxNestingDepth = 1000。
-- jsonb 本身允许更深的嵌套，python json.loads 也不设这个上限，所以"payload 是
-- 合法对象、能被 python 解析"并不等于"publisher 能发出去"。超过 Jackson 上限的
-- 事件会在发布时抛 StreamConstraintsException 并永久退避重试 —— 必须确定性拒绝。
--
-- cap 参数让计数在超过上限后立刻返回，避免对恶意深层输入做无界递归。
-- 迁移末尾会 DROP，不把它留在业务 schema 里。
CREATE OR REPLACE FUNCTION v33_json_depth(v jsonb, cap integer DEFAULT 300)
RETURNS integer
LANGUAGE plpgsql IMMUTABLE AS $fn$
DECLARE
    t     text := jsonb_typeof(v);
    d     integer := 0;
    sub   integer;
    elem  jsonb;
BEGIN
    IF t IN ('object', 'array') THEN
        IF cap <= 0 THEN
            RETURN 1;
        END IF;
        IF t = 'object' THEN
            FOR elem IN SELECT value FROM jsonb_each(v) LOOP
                sub := v33_json_depth(elem, cap - 1);
                IF sub > d THEN d := sub; END IF;
            END LOOP;
        ELSE
            FOR elem IN SELECT value FROM jsonb_array_elements(v) LOOP
                sub := v33_json_depth(elem, cap - 1);
                IF sub > d THEN d := sub; END IF;
            END LOOP;
        END IF;
        RETURN d + 1;
    END IF;
    RETURN 0;
END
$fn$;

-- ============================================================
-- 2c) 数值长度辅助函数（同样只在本次迁移期间存在）
-- ============================================================
-- 为什么还需要它：Jackson 的 StreamReadConstraints 不止限制嵌套深度。
-- 本项目未自定义 StreamReadConstraints（JacksonConfig 里没有覆盖），因此四条
-- 默认上限全部生效。数值取自**本项目实际使用的 jackson-core 2.17.2**
-- （StreamReadConstraints.defaults()，不是凭文档记忆）：
--     maxNestingDepth  = 1000       -> v33_json_depth      > 200      拦截
--     maxNumberLength  = 1000       -> v33_max_number_len  > 700      拦截
--     maxStringLength  = 20000000   -> v33_max_string_len  > 1000000  拦截
--     maxNameLength    = 50000      -> v33_max_name_len    > 40000    拦截
-- 四条都必须在发送前判定：任何一条超标，publishPending 都会抛
-- StreamConstraintsException 并永久指数退避 —— 与嵌套过深完全同一种"永远重试"故障。
-- 四条互不覆盖：标量长度与嵌套深度无关；maxNameLength 只约束对象的**键**，
-- 而 maxStringLength 只约束字符串**值**，所以键超长不会被字符串守卫拦下。
-- 注意 maxNameLength(50000) 比 maxStringLength(20000000) 严 400 倍。
--
-- 计数口径：数字字面量里除符号/小数点/指数标记以外的字符数。这是一个保守上界
-- （Jackson 计的是整个 token 长度），足以把危险值拦在发送之前。
-- 递归受 depth 检查保护：本函数只在深度 <= 200 通过之后才被调用。
CREATE OR REPLACE FUNCTION v33_max_number_len(v jsonb, cap integer DEFAULT 300)
RETURNS integer
LANGUAGE plpgsql IMMUTABLE AS $fn$
DECLARE
    t     text := jsonb_typeof(v);
    m     integer := 0;
    sub   integer;
    elem  jsonb;
    digits integer;
BEGIN
    IF t = 'number' THEN
        RETURN length(regexp_replace(v::text, '[^0-9]', '', 'g'));
    END IF;
    IF t IN ('object', 'array') AND cap > 0 THEN
        IF t = 'object' THEN
            FOR elem IN SELECT value FROM jsonb_each(v) LOOP
                sub := v33_max_number_len(elem, cap - 1);
                IF sub > m THEN m := sub; END IF;
            END LOOP;
        ELSE
            FOR elem IN SELECT value FROM jsonb_array_elements(v) LOOP
                sub := v33_max_number_len(elem, cap - 1);
                IF sub > m THEN m := sub; END IF;
            END LOOP;
        END IF;
    END IF;
    RETURN m;
END
$fn$;

-- ============================================================
-- 2d) 字符串长度辅助函数（同前两个一样，只在本次迁移期间存在）
-- ============================================================
-- Jackson 的 maxStringLength = 20000000（20 MB）。一个 20 MB 的 params 字符串值
-- 深度为 2、数字长度守卫也不受影响，却同样会在发布时无条件抛错并永久重试。
-- 阈值取 1 000 000（字符）：真实业务 params 都是文件名/桶名/枚举，远小于 1 MB；
-- 任何接近 20 MB 的值都只可能是脏数据，没必要为它保留 20 倍的余量。
--
-- 计数口径：按 jsonb 标量取出的**字符数**（#>> '{}' 对 string 标量返回其文本）。
-- 这是保守的：UTF-8 多字节字符的实际字节数只会更大，而 Jackson 计的是字符数，
-- 所以按字符数比较不会把合法短字符串误杀。
CREATE OR REPLACE FUNCTION v33_max_string_len(v jsonb, cap integer DEFAULT 300)
RETURNS integer
LANGUAGE plpgsql IMMUTABLE AS $fn$
DECLARE
    t     text := jsonb_typeof(v);
    m     integer := 0;
    sub   integer;
    elem  jsonb;
    len   integer;
BEGIN
    IF t = 'string' THEN
        RETURN length(v #>> '{}');
    END IF;
    IF t IN ('object', 'array') AND cap > 0 THEN
        IF t = 'object' THEN
            FOR elem IN SELECT value FROM jsonb_each(v) LOOP
                sub := v33_max_string_len(elem, cap - 1);
                IF sub > m THEN m := sub; END IF;
            END LOOP;
        ELSE
            FOR elem IN SELECT value FROM jsonb_array_elements(v) LOOP
                sub := v33_max_string_len(elem, cap - 1);
                IF sub > m THEN m := sub; END IF;
            END LOOP;
        END IF;
    END IF;
    RETURN m;
END
$fn$;

-- ============================================================
-- 2e) 对象键长度辅助函数（第四个，同样只在本次迁移期间存在）
-- ============================================================
-- Jackson 的 maxNameLength = 50000，只约束 JSON **对象的键**；字符串值走
-- maxStringLength（20000000），两者是独立的约束。所以一个 60000 字符的键
-- 既不会被 v33_max_string_len 拦下（它只看值），也能通过深度与数值检查，
-- 却会在发布时抛 Name length (60000) exceeds the maximum allowed (50000)。
-- 阈值取 40000：真实业务 params 的键全是固定枚举（mediaType/objectName/…），
-- 连 100 字符都用不到。留 20% 余量而不是把阈值压到很低，是为了不误杀
-- "虽然奇怪但仍能正常发布"的键——守卫的职责是拦住会永久失败的，不是收紧风格。
CREATE OR REPLACE FUNCTION v33_max_name_len(v jsonb, cap integer DEFAULT 300)
RETURNS integer
LANGUAGE plpgsql IMMUTABLE AS $fn$
DECLARE
    t     text := jsonb_typeof(v);
    m     integer := 0;
    sub   integer;
    k     text;
    elem  jsonb;
BEGIN
    IF t = 'object' AND cap > 0 THEN
        -- 键长度用 jsonb_each_text 取（键就是文本）；递归必须用 jsonb_each，
        -- 因为 jsonb_each_text 的 value 是 text，传给 jsonb 参数会直接报
        -- "invalid input syntax for type json"（实测踩过）。
        FOR k IN SELECT key FROM jsonb_each_text(v) LOOP
            IF length(k) > m THEN m := length(k); END IF;
        END LOOP;
        FOR elem IN SELECT value FROM jsonb_each(v) LOOP
            sub := v33_max_name_len(elem, cap - 1);
            IF sub > m THEN m := sub; END IF;
        END LOOP;
    ELSIF t = 'array' AND cap > 0 THEN
        FOR elem IN SELECT value FROM jsonb_array_elements(v) LOOP
            sub := v33_max_name_len(elem, cap - 1);
            IF sub > m THEN m := sub; END IF;
        END LOOP;
    END IF;
    RETURN m;
END
$fn$;

-- ============================================================
-- 3) 逐行修复
-- ============================================================
DO $$
DECLARE
    r               RECORD;
    v_params        jsonb;
    v_param_type    TEXT;
    v_payload_type  TEXT;
    v_payload_trim  TEXT;
    v_payload       jsonb;
    v_params_trim   TEXT;
    v_new_error     TEXT;
    v_outcome       TEXT;
    v_detail        TEXT;
    v_rows          INTEGER;
    v_ws            TEXT;
    v_depth         INTEGER;
    v_num_len       INTEGER;
    v_str_len       INTEGER;
    v_name_len      INTEGER;
    v_repaired      INTEGER := 0;
    v_parked        INTEGER := 0;
    v_skipped       INTEGER := 0;
BEGIN
    -- 范围：只有 V29 插入的（'inserted by V29 backfill'）和 V31 尝试修复过的
    -- （'repaired by V31'）。其它 outbox 行一概不碰。
    -- last_error <> 'repaired by V33' 是对重复执行的显式防线（即使将来有人
    -- 往 IN 列表里加了值，已修复行也不会被二次改写）。
    -- FOR UPDATE OF o：只锁 outbox 行（LEFT JOIN 的右表只取 AccessShareLock），
    -- 可与并发迁移/SQL 手工执行互斥。
    --
    -- 【为什么还要加 V29 指纹这一支】
    -- TaskOutboxPublisher.publishPending 每次投递失败都会用异常文本**覆写**
    -- last_error，于是 'inserted by V29 backfill' / 'repaired by V31' 这两个标记
    -- 会在第一次失败后永久消失。若只按 last_error 圈范围，那批最坏的历史行
    -- （params 非法、payload 非对象）恰恰在第一次退避后掉出范围，继续无限重试 ——
    -- 正是本迁移要消灭的现象。
    -- V29 给这些行留下了不会漂移的结构指纹：id = md5(job_id || ':V29')
    -- （见 V29 的 INSERT ... SELECT md5(j.id || ':V29')）。正常入队事件的 id 是
    -- UUID.randomUUID()，不可能与指纹相撞。
    -- 指纹分支额外要求 event_type 匹配且**payload 信封不完整**，因此：
    --   · 已经被 V31/V33 修好并正常发布过的 V29 行（payload 含 id/type/priority 对象）
    --     不会被重新打开投递 —— 否则会重复发送；
    --   · 任何非 V29 行都不可能被这一支命中（指纹是 V29 独有的）。
    FOR r IN
        SELECT o.id            AS outbox_id,
               o.aggregate_id  AS aggregate_id,
               o.payload       AS payload,
               o.last_error    AS last_error,
               j.id            AS job_id,
               j.type          AS job_type,
               j.priority      AS job_priority,
               j.params        AS params,
               (o.last_error IN ('inserted by V29 backfill', 'repaired by V31'))
                               AS by_marker,
               (o.event_type = 'ANALYSIS_JOB_QUEUED'
                AND o.id = md5(o.aggregate_id || ':V29'))          AS by_fingerprint
          FROM task_outbox o
          LEFT JOIN analysis_jobs j ON j.id = o.aggregate_id
         WHERE (
                 o.last_error IN ('inserted by V29 backfill', 'repaired by V31')
                 OR (o.event_type = 'ANALYSIS_JOB_QUEUED'
                     AND o.id = md5(o.aggregate_id || ':V29'))
               )
           -- 重复执行的第二道防线：V33 自己写下的终态都要排除。
           --   'repaired by V33'          -> 已重建，不得再改（否则会重复投递）
           --   'repair_failed: ...'       -> 已 park，不得再改（否则 next_retry_at
           --                                 每次执行都被刷新，快照永远不相等）
           -- 这两类只有 V33 会写，因此按前缀排除不会误伤 V29/V31 的历史行。
           -- 【必须用 IS DISTINCT FROM，不能用 <>】
           -- SQL 三值逻辑下 `NULL <> 'x'` 求值为 NULL，WHERE 会把该行丢掉。
           -- 而 publishPending 投递成功后会把 last_error 清成 NULL——
           -- 'last_error IS NULL' 恰是 V29 历史行最常见的生产形态。
           -- 用 <> 会让这批行既不修复、也不 park、也不写审计，永久失修。
           AND o.last_error IS DISTINCT FROM 'repaired by V33'
           AND (o.last_error IS NULL OR o.last_error NOT LIKE 'repair\_failed:%')
         ORDER BY o.id
           FOR UPDATE OF o
    LOOP
        BEGIN
            -- ---- 3.1 既有 payload 的类型（生产隐患：V31 可能已经写出非对象 jsonb）----
            -- 注意 btrim(x) 只去掉**空格**，不去 tab/换行。V31 用的是 btrim(params)，
            -- 因此 params 为 "   \t" 时 NULLIF 判不出来，CAST 抛异常被判成"非法 JSON"。
            -- 这里显式给出空白字符集合，让"只有空白"真正归入 empty 分支。
            --
            -- 【不能用 E'...\v'】PostgreSQL 的 E'' 字符串**没有 \v 转义**：
            -- E' \t\r\n\f\v' 实际是 空格/TAB/CR/LF/FF + 字母 'v'（0x76）。
            -- 于是 btrim('{"a":1}v', E' \t\r\n\f\v') 会变成合法 JSON '{"a":1}'，
            -- 把**非法 JSON 洗成可发送 payload**；反过来真正的垂直制表符 chr(11)
            -- 反而trim不掉，被误判为非法 JSON。两处都必须用 chr(11) 拼出真正的 VT。
            v_ws := ' ' || chr(9) || chr(10) || chr(13) || chr(12) || chr(11);
            v_payload_trim := btrim(r.payload, v_ws);
            v_payload_type := NULL;
            IF v_payload_trim IS NOT NULL AND v_payload_trim <> '' THEN
                BEGIN
                    v_payload_type := jsonb_typeof(v_payload_trim::jsonb);
                EXCEPTION WHEN others THEN
                    v_payload_type := 'invalid';
                END;
            END IF;

            -- ---- 3.2 params 的分类：object / empty / array / string / number / boolean / invalid ----
            -- （depth 校验在下面 object 分支里做：Jackson 的默认嵌套深度上限是 1000，
            --  超过它的对象 jsonb 合法、也能被 python json.loads 解析，但
            --  TaskOutboxPublisher 的 readValue(Map) 会抛 StreamConstraintsException，
            --  于是事件永远重试。这类 payload 不能算"可发送"。）
            v_param_type := 'empty';
            v_params := NULL;
            v_params_trim := btrim(r.params, v_ws);
            -- task_outbox 上没有指向 analysis_jobs 的外键，历史行可能是孤儿
            -- （任务行已被清理）。这种情况无法重建任何可发送消息：
            -- aggregate_id 指向的任务不存在，worker claim 必然 404，
            -- 所以按不可发送处理，而不是拿 NULL 拼一个假消息。
            IF r.job_id IS NULL THEN
                v_param_type := 'orphan';
            ELSIF v_params_trim IS NOT NULL AND v_params_trim <> '' THEN
                BEGIN
                    v_params := v_params_trim::jsonb;
                    v_param_type := jsonb_typeof(v_params);
                EXCEPTION WHEN others THEN
                    v_param_type := 'invalid';
                    v_params := NULL;
                END;
            END IF;

            -- ---- 3.2b 只由指纹命中、且 payload 已经是完整信封的行：不得重发 ----
            -- 这些行是 V31 修好之后正常发布过的历史事件（last_error 已被 publisher
            -- 覆写或清空，指纹仍在）。若走 repaired 分支就会重新打开投递并重复发送。
            -- 判定标准与 createJob 的信封一致：payload 是对象且 id/type/priority 齐全。
            -- 判定必须用 ->> （取值）而不是键存在运算符：JSON 里 {"id":null}
            -- 的键存在判定为真，但那不是可发送信封 —— worker 取到空 id 后 claim 404。
            -- 【by_marker 必须写成 IS NOT TRUE】
            -- last_error 为 NULL 时 `NULL IN (...)` 求值为 NULL，`NOT NULL` 仍是 NULL，
            -- 于是直接写 NOT r.by_marker 会把该行整条排除在守卫之外 —— 而 last_error
            -- 被 publisher 清成 NULL 恰恰是这个指纹分支要处理的最常见形态。
            IF r.by_fingerprint
               AND r.by_marker IS NOT TRUE
               AND v_payload_type = 'object'
               AND (v_payload_trim::jsonb ->> 'id') IS NOT NULL
               AND (v_payload_trim::jsonb ->> 'type') IS NOT NULL
               AND (v_payload_trim::jsonb ->> 'priority') IS NOT NULL THEN
                RAISE NOTICE 'V33: outbox % already carries a full envelope (last_error=%), left as is',
                    r.outbox_id, r.last_error;
                CONTINUE;
            END IF;

            IF v_param_type IN ('object', 'empty') THEN
                -- ---- 3.3 可发送路径：payload 必须是对象 ----
                v_outcome := 'repaired';
                -- 【合并顺序必须是 params || envelope，不能反过来】
                -- `||` 的右侧键覆盖左侧。若写成 envelope || params，则 params 里
                -- 恰好含 'id'/'type'/'priority' 时（历史 params 可能带同名键，
                -- 也可能是显式 null）会把信封覆盖掉：{...} || {"id":null}
                -- 产出 {"id": null} —— 仍是合法 jsonb 对象、仍能反序列化成 Map，
                -- 因此能通过“可发送”检查，但 worker 取到 id='' 后 claim 404，
                -- 任务永远停在 QUEUED。把 envelope 放右侧即可保证它不可被覆盖。
                IF v_param_type = 'object' THEN
                    v_payload := v_params || jsonb_build_object(
                                     'id', r.job_id,
                                     'type', r.job_type,
                                     'priority', r.job_priority
                                 );
                ELSE
                    v_payload := jsonb_build_object(
                                     'id', r.job_id,
                                     'type', r.job_type,
                                     'priority', r.job_priority
                                 );
                END IF;

                -- 合并后再验一次：信封三键必须存在且非 null，否则宁可不发。
                IF v_payload->>'id' IS NULL
                   OR v_payload->>'type' IS NULL
                   OR v_payload->>'priority' IS NULL THEN
                    v_outcome   := 'skipped_not_json';
                    v_new_error := 'repair_failed: envelope incomplete after merge';
                    v_detail    := 'not sendable: merged payload lacks id/type/priority';
                    UPDATE task_outbox o
                       SET attempt       = 0,
                           published_at  = NULL,
                           next_retry_at = NOW() + INTERVAL '100 years',
                           last_error    = v_new_error
                     WHERE o.id = r.outbox_id
                       AND o.last_error IS NOT DISTINCT FROM r.last_error;
                    GET DIAGNOSTICS v_rows = ROW_COUNT;
                    IF v_rows > 0 THEN
                        v_skipped := v_skipped + 1;
                    END IF;
                    INSERT INTO outbox_repair_audit (outbox_id, aggregate_id, outcome, detail)
                    SELECT r.outbox_id, r.aggregate_id, v_outcome, v_detail
                     WHERE NOT EXISTS (
                         SELECT 1 FROM outbox_repair_audit a
                          WHERE a.outbox_id = r.outbox_id AND a.outcome = v_outcome);
                    CONTINUE;
                END IF;

                -- M1：Jackson 默认 max nesting depth = 1000。这里留足余量，
                -- 超过 200 层的对象按"不可发送"处理 —— 真实 params 是扁平对象，
                -- 合法数据不可能接近这个深度。
                IF v_param_type = 'object' THEN
                    v_depth := v33_json_depth(v_params);
                    IF v_depth > 200 THEN
                        v_outcome   := 'skipped_not_json';
                        v_new_error := 'repair_failed: params nesting depth '
                                       || v_depth || ' exceeds publisher limit';
                        v_detail    := format(
                            'not sendable: params nesting depth=%s (>200, Jackson max 1000)',
                            v_depth);
                        UPDATE task_outbox o
                           SET attempt       = 0,
                               published_at  = NULL,
                               next_retry_at = NOW() + INTERVAL '100 years',
                               last_error    = v_new_error
                         WHERE o.id = r.outbox_id
                           AND o.last_error IS NOT DISTINCT FROM r.last_error;
                        GET DIAGNOSTICS v_rows = ROW_COUNT;
                        IF v_rows > 0 THEN
                            v_skipped := v_skipped + 1;
                        END IF;
                        INSERT INTO outbox_repair_audit (outbox_id, aggregate_id, outcome, detail)
                        SELECT r.outbox_id, r.aggregate_id, v_outcome, v_detail
                         WHERE NOT EXISTS (
                             SELECT 1 FROM outbox_repair_audit a
                              WHERE a.outbox_id = r.outbox_id AND a.outcome = v_outcome);
                        CONTINUE;
                    END IF;
                    -- MAJOR-1：Jackson 的 maxNumberLength = 1000 与嵌套深度无关，
                    -- 标量超长数字同样会让发布无条件失败并永久重试。
                    v_num_len := v33_max_number_len(v_params);
                    IF v_num_len > 700 THEN
                        v_outcome   := 'skipped_not_json';
                        v_new_error := 'repair_failed: params has a numeric literal of '
                                       || v_num_len || ' digits (publisher max 1000)';
                        v_detail    := format(
                            'not sendable: longest numeric literal=%s digits '
                            '(Jackson maxNumberLength=1000)', v_num_len);
                        UPDATE task_outbox o
                           SET attempt       = 0,
                               published_at  = NULL,
                               next_retry_at = NOW() + INTERVAL '100 years',
                               last_error    = v_new_error
                         WHERE o.id = r.outbox_id
                           AND o.last_error IS NOT DISTINCT FROM r.last_error;
                        GET DIAGNOSTICS v_rows = ROW_COUNT;
                        IF v_rows > 0 THEN
                            v_skipped := v_skipped + 1;
                        END IF;
                        INSERT INTO outbox_repair_audit (outbox_id, aggregate_id, outcome, detail)
                        SELECT r.outbox_id, r.aggregate_id, v_outcome, v_detail
                         WHERE NOT EXISTS (
                             SELECT 1 FROM outbox_repair_audit a
                              WHERE a.outbox_id = r.outbox_id AND a.outcome = v_outcome);
                        CONTINUE;
                    END IF;
                    -- 第三条 Jackson 上限：maxStringLength = 20000000。
                    -- 深度 2、数字长度正常的长字符串同样会让发布无条件失败。
                    v_str_len := v33_max_string_len(v_params);
                    IF v_str_len > 1000000 THEN
                        v_outcome   := 'skipped_not_json';
                        v_new_error := 'repair_failed: params has a string of '
                                       || v_str_len || ' chars (publisher max 20000000)';
                        v_detail    := format(
                            'not sendable: longest string value=%s chars '
                            '(Jackson maxStringLength=20000000)', v_str_len);
                        UPDATE task_outbox o
                           SET attempt       = 0,
                               published_at  = NULL,
                               next_retry_at = NOW() + INTERVAL '100 years',
                               last_error    = v_new_error
                         WHERE o.id = r.outbox_id
                           AND o.last_error IS NOT DISTINCT FROM r.last_error;
                        GET DIAGNOSTICS v_rows = ROW_COUNT;
                        IF v_rows > 0 THEN
                            v_skipped := v_skipped + 1;
                        END IF;
                        INSERT INTO outbox_repair_audit (outbox_id, aggregate_id, outcome, detail)
                        SELECT r.outbox_id, r.aggregate_id, v_outcome, v_detail
                         WHERE NOT EXISTS (
                             SELECT 1 FROM outbox_repair_audit a
                              WHERE a.outbox_id = r.outbox_id AND a.outcome = v_outcome);
                        CONTINUE;
                    END IF;
                    -- 第四条 Jackson 上限：maxNameLength = 50000，只约束对象**键**。
                    -- 键超长不会被上面的字符串守卫拦下（那条只看字符串值）。
                    v_name_len := v33_max_name_len(v_params);
                    IF v_name_len > 40000 THEN
                        v_outcome   := 'skipped_not_json';
                        v_new_error := 'repair_failed: params has an object key of '
                                       || v_name_len || ' chars (publisher max 50000)';
                        v_detail    := format(
                            'not sendable: longest object key=%s chars '
                            '(Jackson maxNameLength=50000)', v_name_len);
                        UPDATE task_outbox o
                           SET attempt       = 0,
                               published_at  = NULL,
                               next_retry_at = NOW() + INTERVAL '100 years',
                               last_error    = v_new_error
                         WHERE o.id = r.outbox_id
                           AND o.last_error IS NOT DISTINCT FROM r.last_error;
                        GET DIAGNOSTICS v_rows = ROW_COUNT;
                        IF v_rows > 0 THEN
                            v_skipped := v_skipped + 1;
                        END IF;
                        INSERT INTO outbox_repair_audit (outbox_id, aggregate_id, outcome, detail)
                        SELECT r.outbox_id, r.aggregate_id, v_outcome, v_detail
                         WHERE NOT EXISTS (
                             SELECT 1 FROM outbox_repair_audit a
                              WHERE a.outbox_id = r.outbox_id AND a.outcome = v_outcome);
                        CONTINUE;
                    END IF;
                END IF;

                IF v_payload_type IS DISTINCT FROM 'object' THEN
                    v_detail := format(
                        'payload rebuilt: params jsonb_typeof=%s, previous payload jsonb_typeof=%s',
                        v_param_type, COALESCE(v_payload_type, 'null'));
                ELSE
                    v_detail := format(
                        'payload rebuilt: params jsonb_typeof=%s, previous payload was already an object',
                        v_param_type);
                END IF;

                -- 下面这条 UPDATE 是整条迁移里唯一重新打开投递的地方：
                -- published_at 必须先置回 NULL，publisher 的 findPending 才会重新选中它。
                -- [V33-REPAIR]
                UPDATE task_outbox o
                   SET payload       = v_payload::text,
                       attempt       = 0,
                       published_at  = NULL,
                       next_retry_at = NOW(),
                       last_error    = 'repaired by V33'
                 WHERE o.id = r.outbox_id
                   AND o.last_error IS NOT DISTINCT FROM r.last_error;
                GET DIAGNOSTICS v_rows = ROW_COUNT;
                -- [/V33-REPAIR]

                IF v_rows = 0 THEN
                    -- 并发执行者已经改过这一行：不改、不记账，交给对方负责。
                    RAISE NOTICE 'V33: outbox % changed concurrently, left to the other run', r.outbox_id;
                    CONTINUE;
                END IF;
                v_repaired := v_repaired + 1;

            ELSE
                -- ---- 3.4 不可发送路径：绝不生成伪 payload ----
                IF v_param_type = 'invalid' THEN
                    v_outcome   := 'skipped_not_json';
                    v_new_error := 'repair_failed: params is not valid json';
                ELSIF v_param_type = 'orphan' THEN
                    v_outcome   := 'skipped_not_json';
                    v_new_error := 'repair_failed: analysis_jobs row missing';
                ELSE
                    v_outcome   := 'parked';
                    v_new_error := 'repair_failed: params is ' || v_param_type;
                END IF;

                v_detail := format(
                    'not sendable: params jsonb_typeof=%s, previous payload jsonb_typeof=%s, '
                    'next_retry_at pushed 100 years out for manual triage',
                    v_param_type, COALESCE(v_payload_type, 'null'));

                -- 不可发送路径：必须同时对**两条**运行期查询都不可见。
                --
                -- 运行期有两个会自动重新发送的入口，判据各不相同：
                --   1) TaskOutboxPublisher.findPending
                --        where published_at IS NULL and next_retry_at <= now()
                --   2) TaskOutboxPublisher.findStalePublishedForQueuedJobs
                --        where published_at IS NOT NULL and published_at < cutoff
                --              and job.status = 'QUEUED'
                --        —— 它**完全不看** next_retry_at。
                --
                -- 因此“保留 published_at、只推远 next_retry_at”并不安全：只要该行
                -- 原本已发布过（published_at 非空），入口 2 仍会选中它，把
                -- published_at 清空、attempt 归零、last_error 改写成
                -- 'requeued: job still QUEUED after Ns'，于是被 park 的坏 payload
                -- 在 ~300 秒后重新变成“可发送”，无限退避循环原样复活。
                --
                -- 唯一同时躲开两个入口的稳定组合是：
                --   published_at = NULL  +  next_retry_at = 100 年后
                -- 这样入口 1 因 next_retry_at 远未到期而不选，入口 2 因
                -- published_at IS NULL 而不选。两处都成立，park 才是持久的。
                -- [V33-PARK]
                UPDATE task_outbox o
                   SET attempt       = 0,
                       published_at  = NULL,
                       next_retry_at = NOW() + INTERVAL '100 years',
                       last_error    = v_new_error
                 WHERE o.id = r.outbox_id
                   AND o.last_error IS NOT DISTINCT FROM r.last_error;
                GET DIAGNOSTICS v_rows = ROW_COUNT;
                -- [/V33-PARK]

                IF v_rows = 0 THEN
                    RAISE NOTICE 'V33: outbox % changed concurrently, left to the other run', r.outbox_id;
                    CONTINUE;
                END IF;

                IF v_outcome = 'skipped_not_json' THEN
                    v_skipped := v_skipped + 1;
                ELSE
                    v_parked := v_parked + 1;
                END IF;
            END IF;

            -- ---- 3.5 持久审计（NOT EXISTS + 唯一索引双保险，重复执行不重复记账）----
            IF NOT EXISTS (
                SELECT 1 FROM outbox_repair_audit a
                 WHERE a.outbox_id = r.outbox_id
                   AND a.outcome = v_outcome
            ) THEN
                INSERT INTO outbox_repair_audit (outbox_id, aggregate_id, outcome, detail)
                VALUES (r.outbox_id, r.aggregate_id, v_outcome, v_detail);
            END IF;

        EXCEPTION WHEN others THEN
            -- 单行异常不得让整条迁移失败（否则一处脏数据会阻塞后续所有迁移）。
            -- 但仍然要留下**可查询**的痕迹，这是 V31 只 RAISE NOTICE 的缺陷。
            RAISE WARNING 'V33: row failed for outbox % (job %) - %', r.outbox_id, r.job_id, SQLERRM;
            BEGIN
                -- 与 park 分支同一套稳定性条件：published_at 置 NULL 且
                -- next_retry_at 推到 100 年后，两条运行期重发送入口都选不中。
                UPDATE task_outbox o
                   SET attempt       = 0,
                       published_at  = NULL,
                       next_retry_at = NOW() + INTERVAL '100 years',
                       last_error    = 'repair_failed: unexpected error'
                 WHERE o.id = r.outbox_id
                   AND o.last_error IS NOT DISTINCT FROM r.last_error;
                -- outcome 用独立的 'error'，不能复用 'skipped_not_json'：
                -- 后者的语义是"params 不是合法 JSON"，而这里可能是任何异常
                -- （约束冲突、权限、类型错误），混用会让运维朝错误方向排查。
                INSERT INTO outbox_repair_audit (outbox_id, aggregate_id, outcome, detail)
                SELECT r.outbox_id, r.aggregate_id, 'error',
                       'unexpected_error: ' || SQLERRM
                 WHERE NOT EXISTS (
                     SELECT 1 FROM outbox_repair_audit a
                      WHERE a.outbox_id = r.outbox_id
                        AND a.outcome = 'error'
                 );
            EXCEPTION WHEN others THEN
                -- MAJOR-2：连"记录失败"都失败了（典型：outbox_repair_audit 不可写）。
                -- 此时若只 WARNING，该行会保持原来的可发送状态且没有审计记录 ——
                -- 正好是本迁移要消灭的"静默失修"。因此这里 fail closed：
                -- 让整条迁移失败，由 Flyway 回滚，操作者修好审计表后重跑。
                RAISE EXCEPTION 'V33: cannot record repair outcome for outbox % (%) and '
                    'audit storage is unavailable (%); aborting so the migration can be '
                    'retried instead of leaving a silently sendable row',
                    r.outbox_id, r.aggregate_id, SQLERRM;
            END;
        END;
    END LOOP;

    RAISE NOTICE 'V33: repaired % event(s), parked % event(s), skipped % event(s)',
        v_repaired, v_parked, v_skipped;
END $$;

-- 辅助函数只服务于本次迁移，不留在业务 schema 里（避免污染 public 命名空间）。
-- DROP IF EXISTS 保证迁移可重复执行。
DROP FUNCTION IF EXISTS v33_json_depth(jsonb, integer);
DROP FUNCTION IF EXISTS v33_max_number_len(jsonb, integer);
DROP FUNCTION IF EXISTS v33_max_string_len(jsonb, integer);
DROP FUNCTION IF EXISTS v33_max_name_len(jsonb, integer);
