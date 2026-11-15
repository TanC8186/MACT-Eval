#!/usr/bin/env python3
"""V33 历史 outbox payload 修复的真实 PostgreSQL 验证（R02 收尾）。

静态 SQL 断言（OutboxPayloadRepairMigrationTest）只能证明迁移文件"长成什么样"，
证明不了它在真库里到底把哪一行改成了什么。本脚本在隔离容器里真正执行迁移并
**查询结果 payload**，这是计划 §4 验收要求的证据。

做法的精确说明（不要含糊）：

1. 目标容器白名单：只允许 ``mact-ui-postgres``；库名必须以 ``macteval_`` 开头，
   且不得是用户自己的 ``macteval`` / ``postgres``。用户原有容器一律拒绝。
2. 在容器内 DROP/CREATE 一个 scratch 库 ``macteval_outbox_repair_proof``。
3. 用 psql 按**数字顺序**重放真实的 V1..V32 迁移文件（不是手搓最小 schema），
   因此 V33 是在真实 V29 / V31 语句之后执行的，前置 schema 完全等价。
4. 插入 11 个夹具事件，覆盖 object / NULL / 空串 / array / string / number /
   boolean / 非法 JSON / V31 已经写坏的既有 payload / 非 V29 控制行 / 已修复行。
5. 执行**真实的 V33 文件**，然后用 SQL 查询 payload / last_error / published_at /
   next_retry_at / 审计表逐条断言。
6. 用 python ``json.loads`` 复验可发送 payload 能解析成 dict —— 这正是
   ``TaskOutboxPublisher.publishPending`` 的 ``readValue(..., Map.class)`` 与
   ai-service 消费者要求的形态。
7. 再执行一次 V33，断言行数据 / 审计行完全不变（幂等）。
8. 打印 JSON 摘要，写 docs/reviews/evidence/2026-10-27-outbox-repair-proof.json，
   任何断言失败以非零码退出。

只用标准库。在 Windows Git Bash 下可直接运行。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

CONTAINER = "mact-ui-postgres"
DB_USER = "macteval"
ADMIN_DB = "postgres"
PROOF_DB = "macteval_outbox_repair_proof"

#: 用户自己的库，绝不能作为 scratch 目标。
FORBIDDEN_DATABASES = {"macteval", "postgres", "template0", "template1"}

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "backend" / "src" / "main" / "resources" / "db" / "migration"
EVIDENCE = REPO_ROOT / "docs" / "reviews" / "evidence" / "2026-10-27-outbox-repair-proof.json"

V33 = MIGRATIONS / "V33__harden_v29_outbox_payload_repair.sql"

failures: list[str] = []
cases_executed: list[str] = []
observed: list[str] = []


def fail(msg: str) -> None:
    failures.append(msg)
    print(f"FAIL  {msg}", file=sys.stderr)


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"ok    {msg}")
    else:
        fail(msg)


def run(cmd: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )


def psql(sql: str, db: str = PROOF_DB, *, rows: bool = False,
         with_notices: bool = False) -> str:
    """跑 psql；rows=True 时用 -A -t 输出便于解析。

    ``with_notices=True`` 时把 stderr 一起返回：PL/pgSQL 的 ``RAISE NOTICE``
    走 stderr，只收 stdout 就会看到"空输出"，误判迁移没产生任何结论。
    """
    cmd = [
        "docker", "exec", "-i", CONTAINER, "psql",
        "-U", DB_USER, "-d", db, "-X", "-q", "-v", "ON_ERROR_STOP=1",
    ]
    if rows:
        cmd += ["-A", "-t"]
    proc = run(cmd, stdin=sql)
    if proc.returncode != 0:
        raise RuntimeError(f"psql 失败 (db={db}, rc={proc.returncode}):\n{proc.stderr}\n--- SQL ---\n{sql[:4000]}")
    return proc.stdout + (proc.stderr if with_notices else "")


def psql_json_rows(sql: str) -> list[dict]:
    """每行一个 row_to_json 对象。"""
    out = psql(sql, rows=True)
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def guard() -> None:
    """容器 + 库名白名单。不在白名单里就直接拒绝，不做任何写操作。"""
    if CONTAINER != "mact-ui-postgres":
        raise SystemExit(f"refusing: container must be mact-ui-postgres, got {CONTAINER}")
    if CONTAINER.startswith("personal_design-"):
        raise SystemExit("refusing: that is the user's own container")

    running = run(["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER])
    if running.returncode != 0 or running.stdout.strip() != "true":
        raise SystemExit(f"refusing: container {CONTAINER} is not running")

    if not PROOF_DB.startswith("macteval_"):
        raise SystemExit(f"refusing: database must start with macteval_, got {PROOF_DB}")
    if PROOF_DB in FORBIDDEN_DATABASES:
        raise SystemExit(f"refusing: {PROOF_DB} is a user database")

    # 本脚本随后会 `DROP DATABASE IF EXISTS` scratch 库，所以白名单必须覆盖
    # 每一个会被重建的库名（而不只是 PROOF_DB 这个变量本身）。
    scratch_databases = {PROOF_DB}
    existing = set(psql("SELECT datname FROM pg_database;", ADMIN_DB, rows=True).split())
    for db in scratch_databases:
        if not db.startswith("macteval_"):
            raise SystemExit(f"refusing: {db} must start with macteval_")
        if db in FORBIDDEN_DATABASES:
            raise SystemExit(f"refusing: {db} is a user database")
        if db in {"macteval_ui", "macteval_capacity", "macteval_round2"}:
            raise SystemExit(f"refusing: {db} belongs to the acceptance rig / drills; "
                             f"this script would DROP it")
    print(f"guard ok: container={CONTAINER} scratch_db={PROOF_DB} "
          f"(will be dropped+recreated; existing={sorted(existing)})")


def migration_files_upto(limit: int) -> list[Path]:
    files = sorted(
        (p for p in MIGRATIONS.glob("V*.sql")),
        key=lambda p: int(p.name.split("__")[0][1:]),
    )
    return [p for p in files if int(p.name.split("__")[0][1:]) <= limit]


FIXTURES = r"""
-- 夹具：analysis_jobs + task_outbox。params 覆盖 object / NULL / 空串 /
-- array / string / number / boolean / 非法 JSON。
INSERT INTO analysis_jobs (id, type, status, priority, progress, params, attempt)
VALUES
  ('job-01', 'VIDEO_ANALYSIS', 'QUEUED', 'NORMAL', 0, '{"mediaType":"video","objectName":"a.mp4"}', 0),
  ('job-02', 'VIDEO_ANALYSIS', 'QUEUED', 'NORMAL', 0, NULL, 0),
  ('job-03', 'AUDIO_ANALYSIS', 'QUEUED', 'NORMAL', 0, '', 0),
  ('job-04', 'VIDEO_ANALYSIS', 'QUEUED', 'NORMAL', 0, '[1,2]', 0),
  ('job-05', 'VIDEO_ANALYSIS', 'QUEUED', 'NORMAL', 0, '"abc"', 0),
  ('job-06', 'VIDEO_ANALYSIS', 'QUEUED', 'NORMAL', 0, '42', 0),
  ('job-07', 'VIDEO_ANALYSIS', 'QUEUED', 'NORMAL', 0, 'not-json{', 0),
  ('job-08', 'VIDEO_ANALYSIS', 'QUEUED', 'NORMAL', 0, '{"mediaType":"video","windowDuration":60}', 0),
  ('job-09', 'VIDEO_ANALYSIS', 'QUEUED', 'NORMAL', 0, '{"mediaType":"video"}', 0),
  ('job-10', 'VIDEO_ANALYSIS', 'QUEUED', 'NORMAL', 0, '{"mediaType":"video"}', 0),
  ('job-11', 'AUDIO_ANALYSIS', 'QUEUED', 'NORMAL', 0, 'true', 0);

-- ob-01: V29 原样把 params 当 payload（object）
INSERT INTO task_outbox (id, aggregate_id, event_type, payload, attempt, next_retry_at, published_at, last_error)
VALUES ('ob-01','job-01','ANALYSIS_JOB_QUEUED','{"mediaType":"video","objectName":"a.mp4"}',0,NOW(),NULL,'inserted by V29 backfill');
-- ob-02: params IS NULL（V29 写的是 j.params，此处 payload 为空串）
INSERT INTO task_outbox (id, aggregate_id, event_type, payload, attempt, next_retry_at, published_at, last_error)
VALUES ('ob-02','job-02','ANALYSIS_JOB_QUEUED','',0,NOW(),NULL,'inserted by V29 backfill');
-- ob-03: params 是空串
INSERT INTO task_outbox (id, aggregate_id, event_type, payload, attempt, next_retry_at, published_at, last_error)
VALUES ('ob-03','job-03','ANALYSIS_JOB_QUEUED','',0,NOW(),NULL,'inserted by V29 backfill');
-- ob-04: params 是 JSON array（已发布过，published_at 非空）
INSERT INTO task_outbox (id, aggregate_id, event_type, payload, attempt, next_retry_at, published_at, last_error)
VALUES ('ob-04','job-04','ANALYSIS_JOB_QUEUED','[1,2]',3,NOW(),'2026-01-01 00:00:00','inserted by V29 backfill');
-- ob-05: params 是 JSON string
INSERT INTO task_outbox (id, aggregate_id, event_type, payload, attempt, next_retry_at, published_at, last_error)
VALUES ('ob-05','job-05','ANALYSIS_JOB_QUEUED','"abc"',0,NOW(),NULL,'inserted by V29 backfill');
-- ob-06: params 是 JSON number
INSERT INTO task_outbox (id, aggregate_id, event_type, payload, attempt, next_retry_at, published_at, last_error)
VALUES ('ob-06','job-06','ANALYSIS_JOB_QUEUED','42',0,NOW(),NULL,'inserted by V29 backfill');
-- ob-07: params 不是合法 JSON
INSERT INTO task_outbox (id, aggregate_id, event_type, payload, attempt, next_retry_at, published_at, last_error)
VALUES ('ob-07','job-07','ANALYSIS_JOB_QUEUED','not-json{',0,NOW(),NULL,'inserted by V29 backfill');
-- ob-08: 生产隐患。V31 已经跑过并把 payload 写成了非对象 jsonb（array）。
--        {id,type,priority} || '[1,2]' 在 jsonb 里是 array，不是 object。
INSERT INTO task_outbox (id, aggregate_id, event_type, payload, attempt, next_retry_at, published_at, last_error)
VALUES ('ob-08','job-08','ANALYSIS_JOB_QUEUED','[{"id":"job-08"},1,2]',0,NOW(),NULL,'repaired by V31');
-- ob-09: 控制行，last_error 与 V29/V31 无关，必须原封不动
INSERT INTO task_outbox (id, aggregate_id, event_type, payload, attempt, next_retry_at, published_at, last_error)
VALUES ('ob-09','job-09','ANALYSIS_JOB_QUEUED','{"id":"job-09"}',5,NOW(),NULL,'requeued by recovery');
-- ob-10: 已经是 V33 修复过的行（重跑幂等夹具）
INSERT INTO task_outbox (id, aggregate_id, event_type, payload, attempt, next_retry_at, published_at, last_error)
VALUES ('ob-10','job-10','ANALYSIS_JOB_QUEUED',
        '{"id":"job-10","type":"VIDEO_ANALYSIS","priority":"NORMAL","mediaType":"video"}',
        0,NOW(),NULL,'repaired by V33');
-- ob-11: params 是 JSON boolean（额外覆盖，超出十项最低要求）
INSERT INTO task_outbox (id, aggregate_id, event_type, payload, attempt, next_retry_at, published_at, last_error)
VALUES ('ob-11','job-11','ANALYSIS_JOB_QUEUED','true',0,NOW(),NULL,'inserted by V29 backfill');

-- ob-12: 孤儿事件。task_outbox 没有指向 analysis_jobs 的外键，V29 插入后
--        任务行可能已被清理。JOIN 会静默跳过它 —— 必须 park + 记账，不能假装没看见。
INSERT INTO task_outbox (id, aggregate_id, event_type, payload, attempt, next_retry_at, published_at, last_error)
VALUES ('ob-12','job-does-not-exist','ANALYSIS_JOB_QUEUED','{"mediaType":"video"}',0,NOW(),NULL,'inserted by V29 backfill');

-- ob-13: 最坏组合 —— 既有 payload 是非对象 jsonb *且* params 也是数组。
--        两个判型都要挡住，不能因为"其中一个看起来还行"就混过去。
INSERT INTO analysis_jobs (id, type, status, priority, progress, params, attempt)
VALUES ('job-13', 'VIDEO_ANALYSIS', 'QUEUED', 'NORMAL', 0, '["x"]', 0);
INSERT INTO task_outbox (id, aggregate_id, event_type, payload, attempt, next_retry_at, published_at, last_error)
VALUES ('ob-13','job-13','ANALYSIS_JOB_QUEUED','[{"id":"job-13"},1]',0,NOW(),NULL,'repaired by V31');

-- ob-14: params 只有空白字符。btrim 后为空 -> 走 empty 路径，只补信封键。
INSERT INTO analysis_jobs (id, type, status, priority, progress, params, attempt)
VALUES ('job-14', 'AUDIO_ANALYSIS', 'QUEUED', 'HIGH', 0, '   ' || chr(9) || chr(10), 0);
INSERT INTO task_outbox (id, aggregate_id, event_type, payload, attempt, next_retry_at, published_at, last_error)
VALUES ('ob-14','job-14','ANALYSIS_JOB_QUEUED','',0,NOW(),NULL,'repaired by V31');

-- ob-15: params 是空对象 '{}' -> object 路径，payload 恰好只有信封键。
INSERT INTO analysis_jobs (id, type, status, priority, progress, params, attempt)
VALUES ('job-15', 'VIDEO_ANALYSIS', 'QUEUED', 'LOW', 0, '{}', 0);
INSERT INTO task_outbox (id, aggregate_id, event_type, payload, attempt, next_retry_at, published_at, last_error)
VALUES ('ob-15','job-15','ANALYSIS_JOB_QUEUED','{}',0,NOW(),NULL,'inserted by V29 backfill');

-- ob-16: 【真实生产时序】V29 插入的坏行 + publisher 已经把 last_error 覆写成
--        反序列化异常文本 + attempt 已经涨到 9。last_error 标记已消失，
--        只能靠 V29 的结构指纹 id=md5(job_id||':V29') 才能找到它。
--        这是"最坏历史行掉出范围后无限重试"的核心复现。
INSERT INTO analysis_jobs (id, type, status, priority, progress, params, attempt)
VALUES ('job-16', 'VIDEO_ANALYSIS', 'QUEUED', 'NORMAL', 0, '[1,2,3]', 0);
INSERT INTO task_outbox (id, aggregate_id, event_type, payload, attempt, next_retry_at, published_at, last_error)
VALUES (md5('job-16' || ':V29'),'job-16','ANALYSIS_JOB_QUEUED','[1,2,3]',9,NOW(),NULL,
        'Cannot deserialize value of type java.util.LinkedHashMap from Array value');

-- ob-17: V29 指纹命中，但 payload 已经是**完整信封**（V31 修好且已正常发布）。
--        必须原封不动，绝不能被重新打开投递（否则重复发送）。
INSERT INTO analysis_jobs (id, type, status, priority, progress, params, attempt)
VALUES ('job-17', 'VIDEO_ANALYSIS', 'QUEUED', 'NORMAL', 0, '{"mediaType":"video"}', 0);
INSERT INTO task_outbox (id, aggregate_id, event_type, payload, attempt, next_retry_at, published_at, last_error)
VALUES (md5('job-17' || ':V29'),'job-17','ANALYSIS_JOB_QUEUED',
        '{"id":"job-17","type":"VIDEO_ANALYSIS","priority":"NORMAL","mediaType":"video"}',
        0,'2026-01-01 00:00:00','2026-01-01 00:00:00',NULL);

-- ob-18: [R11-1] V29 fingerprint row, payload=params (an OBJECT, so it IS
--        Map-deserializable). publishPending therefore publishes it
--        SUCCESSFULLY and nulls last_error. But the payload has no
--        id/type/priority, so the worker claim 404s and the job stays QUEUED
--        forever. This is the most common production shape of a V29 row.
--        The old predicate `last_error <> 'repaired by V33'` is NULL-unsafe
--        (NULL <> 'x' => NULL), so these rows were never repaired, never
--        parked and never audited -> permanently broken.
INSERT INTO analysis_jobs (id, type, status, priority, progress, params, attempt)
VALUES ('job-18', 'VIDEO_ANALYSIS', 'QUEUED', 'NORMAL', 0,
        '{"mediaType":"video","objectName":"x.mp4"}', 0);
INSERT INTO task_outbox (id, aggregate_id, event_type, payload, attempt, next_retry_at, published_at, last_error)
VALUES (md5('job-18' || ':V29'),'job-18','ANALYSIS_JOB_QUEUED',
        '{"mediaType":"video","objectName":"x.mp4"}',0,NOW(),NOW()-INTERVAL '1 hour',NULL);

-- ob-19: [R11-7] params itself carries id/type/priority as explicit nulls.
--        `envelope || params` lets params win -> {"id":null,...}, which is
--        still a legal object and still Map-deserializable, but the worker
--        reads an empty id. The envelope must win the merge instead.
INSERT INTO analysis_jobs (id, type, status, priority, progress, params, attempt)
VALUES ('job-19', 'VIDEO_ANALYSIS', 'QUEUED', 'NORMAL', 0,
        '{"id":null,"type":null,"priority":null,"mediaType":"video"}', 0);
INSERT INTO task_outbox (id, aggregate_id, event_type, payload, attempt, next_retry_at, published_at, last_error)
VALUES ('ob-19','job-19','ANALYSIS_JOB_QUEUED','{"id":null,"type":null,"priority":null}',0,NOW(),NULL,
        'inserted by V29 backfill');

-- ob-20: [R11-2] An already-published broken row (published_at NOT NULL).
--        A durable park must be invisible to BOTH re-send paths:
--          findPending                     -> published_at IS NULL
--          findStalePublishedForQueuedJobs -> published_at IS NOT NULL
--                                             (it never looks at next_retry_at)
--        Pushing next_retry_at alone is NOT enough: the reconciler would
--        re-arm this row after ~300s and resurrect the retry loop.
INSERT INTO analysis_jobs (id, type, status, priority, progress, params, attempt)
VALUES ('job-20', 'AUDIO_ANALYSIS', 'QUEUED', 'NORMAL', 0, '[7,8]', 0);
INSERT INTO task_outbox (id, aggregate_id, event_type, payload, attempt, next_retry_at, published_at, last_error)
VALUES ('ob-20','job-20','ANALYSIS_JOB_QUEUED','[7,8]',3,NOW(),NOW()-INTERVAL '1 hour',
        'inserted by V29 backfill');

-- ob-21: [B2 behaviour] params ends with the letter 'v'. PostgreSQL has no \v
--        escape in E'' strings, so the old btrim(x, E' \t\r\n\f\v') stripped
--        real 'v' characters and turned this INVALID json into valid
--        '{"a":1}' -- then published it. Must stay invalid -> skipped_not_json.
INSERT INTO analysis_jobs (id, type, status, priority, progress, params, attempt)
VALUES ('job-21', 'VIDEO_ANALYSIS', 'QUEUED', 'NORMAL', 0, '{"a":1}v', 0);
INSERT INTO task_outbox (id, aggregate_id, event_type, payload, attempt, next_retry_at, published_at, last_error)
VALUES ('ob-21','job-21','ANALYSIS_JOB_QUEUED','{"a":1}v',0,NOW(),NULL,'inserted by V29 backfill');

-- ob-22: [B2 behaviour, reverse] params is a real vertical tab (chr(11)) plus
--        whitespace -> conceptually empty -> repaired with envelope only.
INSERT INTO analysis_jobs (id, type, status, priority, progress, params, attempt)
VALUES ('job-22', 'VIDEO_ANALYSIS', 'QUEUED', 'NORMAL', 0, chr(11) || chr(9) || '  ', 0);
INSERT INTO task_outbox (id, aggregate_id, event_type, payload, attempt, next_retry_at, published_at, last_error)
VALUES ('ob-22','job-22','ANALYSIS_JOB_QUEUED','',0,NOW(),NULL,'inserted by V29 backfill');

-- ob-23: [M1 behaviour] params nested 250 levels deep. Legal jsonb, legal for
--        python json.loads, but Jackson's maxNestingDepth/our 200 guard must
--        reject it -- otherwise the publisher fails forever.
INSERT INTO analysis_jobs (id, type, status, priority, progress, params, attempt)
VALUES ('job-23', 'VIDEO_ANALYSIS', 'QUEUED', 'NORMAL', 0,
        (WITH RECURSIVE nest(i, v) AS (
             SELECT 1, '1'::jsonb
           UNION ALL
             SELECT i + 1, jsonb_build_object('k', v) FROM nest WHERE i < 250
         ) SELECT v::text FROM nest WHERE i = 250), 0);
INSERT INTO task_outbox (id, aggregate_id, event_type, payload, attempt, next_retry_at, published_at, last_error)
VALUES ('ob-23','job-23','ANALYSIS_JOB_QUEUED','"dense"',0,NOW(),NULL,'inserted by V29 backfill');

-- ob-24: [MAJOR-1 behaviour] params carries a 2000-digit numeric literal.
--        Depth is 2 (passes the depth guard) but Jackson's maxNumberLength is
--        1000 -> the publisher would fail forever.
INSERT INTO analysis_jobs (id, type, status, priority, progress, params, attempt)
VALUES ('job-24', 'VIDEO_ANALYSIS', 'QUEUED', 'NORMAL', 0,
        '{"n":' || repeat('9', 2000) || '}', 0);
INSERT INTO task_outbox (id, aggregate_id, event_type, payload, attempt, next_retry_at, published_at, last_error)
VALUES ('ob-24','job-24','ANALYSIS_JOB_QUEUED','"bignum"',0,NOW(),NULL,'inserted by V29 backfill');

-- ob-26: [envelope guard] fingerprint row (so NOT by_marker) whose EXISTING
--        payload is an object but carries null envelope values. A key-presence
--        test (? 'id') would treat it as "already has a full envelope" and skip
--        it, leaving a payload the worker can never claim. Value tests (- >>)
--        must rebuild it instead.
INSERT INTO analysis_jobs (id, type, status, priority, progress, params, attempt)
VALUES ('job-26', 'VIDEO_ANALYSIS', 'QUEUED', 'NORMAL', 0, '{"mediaType":"video"}', 0);
INSERT INTO task_outbox (id, aggregate_id, event_type, payload, attempt, next_retry_at, published_at, last_error)
VALUES (md5('job-26' || ':V29'),'job-26','ANALYSIS_JOB_QUEUED',
        '{"id":null,"type":null,"priority":null}',0,NOW(),NULL,NULL);

-- ob-27: [maxStringLength] a 2 MB string value. Depth is 2 and the number
--        guard is unaffected, but Jackson's maxStringLength (20 MB) is the
--        third StreamReadConstraints bound -- a value this size must be
--        rejected before sending, not retried forever.
INSERT INTO analysis_jobs (id, type, status, priority, progress, params, attempt)
VALUES ('job-27', 'VIDEO_ANALYSIS', 'QUEUED', 'NORMAL', 0,
        jsonb_build_object('s', repeat('x', 2000000))::text, 0);
INSERT INTO task_outbox (id, aggregate_id, event_type, payload, attempt, next_retry_at, published_at, last_error)
VALUES ('ob-27','job-27','ANALYSIS_JOB_QUEUED','"long"',0,NOW(),NULL,'inserted by V29 backfill');

-- ob-28: [control] a short string must NOT be rejected by that guard.
INSERT INTO analysis_jobs (id, type, status, priority, progress, params, attempt)
VALUES ('job-28', 'VIDEO_ANALYSIS', 'QUEUED', 'NORMAL', 0, '{"s":"short"}', 0);
INSERT INTO task_outbox (id, aggregate_id, event_type, payload, attempt, next_retry_at, published_at, last_error)
VALUES ('ob-28','job-28','ANALYSIS_JOB_QUEUED','"short"',0,NOW(),NULL,'inserted by V29 backfill');

-- ob-30: [maxNameLength] a 60000-char object KEY. Values are all short, so the
--        string guard does not fire; depth is 2 and the number guard is
--        unaffected. Jackson's maxNameLength (50000) is the fourth bound and
--        must reject this before sending.
INSERT INTO analysis_jobs (id, type, status, priority, progress, params, attempt)
VALUES ('job-30', 'VIDEO_ANALYSIS', 'QUEUED', 'NORMAL', 0,
        jsonb_build_object(repeat('k', 60000), 'v')::text, 0);
INSERT INTO task_outbox (id, aggregate_id, event_type, payload, attempt, next_retry_at, published_at, last_error)
VALUES ('ob-30','job-30','ANALYSIS_JOB_QUEUED','"longkey"',0,NOW(),NULL,'inserted by V29 backfill');

-- ob-31: [control] a 40000-char key, under the limit -> must be repaired.
INSERT INTO analysis_jobs (id, type, status, priority, progress, params, attempt)
VALUES ('job-31', 'VIDEO_ANALYSIS', 'QUEUED', 'NORMAL', 0,
        jsonb_build_object(repeat('k', 40000), 'v')::text, 0);
INSERT INTO task_outbox (id, aggregate_id, event_type, payload, attempt, next_retry_at, published_at, last_error)
VALUES ('ob-31','job-31','ANALYSIS_JOB_QUEUED','"longkey"',0,NOW(),NULL,'inserted by V29 backfill');

-- ob-25: [MAJOR-1 control] a short number must NOT be rejected.
INSERT INTO analysis_jobs (id, type, status, priority, progress, params, attempt)
VALUES ('job-25', 'VIDEO_ANALYSIS', 'QUEUED', 'NORMAL', 0, '{"n":123}', 0);
INSERT INTO task_outbox (id, aggregate_id, event_type, payload, attempt, next_retry_at, published_at, last_error)
VALUES ('ob-25','job-25','ANALYSIS_JOB_QUEUED','{}',0,NOW(),NULL,'inserted by V29 backfill');
"""

SNAPSHOT = r"""
SELECT row_to_json(t)::text FROM (
  SELECT o.id                                AS outbox_id,
         o.aggregate_id                      AS aggregate_id,
         o.last_error                        AS last_error,
         o.payload                           AS payload,
         o.attempt                           AS attempt,
         o.published_at                      AS published_at,
         o.next_retry_at                     AS next_retry_at,
         (o.published_at IS NULL AND o.next_retry_at <= NOW()) AS publisher_would_send,
         (o.next_retry_at > NOW() + INTERVAL '50 years')        AS parked_far_future,
         -- [R11-2] runtime's second auto-resend entry point:
         -- findStalePublishedForQueuedJobs looks at published_at ONLY and
         -- never consults next_retry_at. A durable park must be invisible
         -- to it as well, or the bad payload revives every ~300s.
         (o.published_at IS NOT NULL
          AND o.published_at < NOW() - INTERVAL '5 minutes')    AS selected_by_reconciler,
         -- Deliberately NO jsonb casts here: some fixtures carry payloads that are
         -- not valid JSON at all ('not-json{'), and a cast would abort the whole
         -- snapshot. Every JSON-shape assertion is computed in python instead.
         j.params                                              AS job_params
    FROM task_outbox o
    -- LEFT JOIN keeps orphan events (no analysis_jobs row) in the snapshot
    -- instead of dropping them.
    LEFT JOIN analysis_jobs j ON j.id = o.aggregate_id
   ORDER BY o.id
) t;
"""

AUDIT_SNAPSHOT = r"""
SELECT row_to_json(t)::text FROM (
  SELECT a.outbox_id, a.aggregate_id, a.outcome, a.detail
    FROM outbox_repair_audit a
   ORDER BY a.outbox_id, a.outcome
) t;
"""


def audit_table_exists() -> bool:
    out = psql(
        "SELECT to_regclass('public.outbox_repair_audit') IS NOT NULL;",
        rows=True,
    )
    return out.strip() == "t"


def snapshot() -> tuple[list[dict], list[dict]]:
    """返回 (outbox 快照, 审计快照)。修复前审计表还不存在，按空表处理。"""
    rows = psql_json_rows(SNAPSHOT)
    audit = psql_json_rows(AUDIT_SNAPSHOT) if audit_table_exists() else []
    return rows, audit


def main() -> int:
    guard()

    print("\n== 1. 重建 scratch 库")
    psql(f"DROP DATABASE IF EXISTS {PROOF_DB};", ADMIN_DB)
    psql(f"CREATE DATABASE {PROOF_DB};", ADMIN_DB)

    print("== 2. 按数字顺序重放真实 V1..V32 迁移")
    migrations = migration_files_upto(32)
    if not migrations:
        fail("没有找到任何迁移文件")
        return 1
    for path in migrations:
        psql(path.read_text(encoding="utf-8"))
    applied = [p.name for p in migrations]
    check(applied[-1].startswith("V32__"), f"前置迁移重放至 {applied[-1]}")
    check(any(n.startswith("V29__") for n in applied), "V29 真实语句已执行")
    check(any(n.startswith("V31__") for n in applied), "V31 真实语句已执行")

    print("== 3. 插入夹具")
    psql(FIXTURES)

    pre_rows, pre_audit = snapshot()
    # 30 = job-01..11（11）+ 孤儿 job-does-not-exist（1）+ job-13..28（16）+ job-30/31（2）。
    # job-29 属于 fail-closed 段落，在快照之后才创建、用完即删，不计入。
    check(len(pre_rows) == 30, f"夹具事件数 = {len(pre_rows)}（期望 30）")
    check(len(pre_audit) == 0, "修复前审计表为空")

    print("== 4. 执行真实 V33 文件")
    v33_sql = V33.read_text(encoding="utf-8")
    v33_out = psql(v33_sql, with_notices=True)
    for line in v33_out.splitlines():
        if line.strip():
            print("      " + line.strip())
    # 首次执行必须真的处理了行：
    #   repaired(13): ob-01/02/03/08/14/15 + ob-18（NULL last_error）+ ob-19（信封覆盖）
    #                 + ob-22（VT/空白） + ob-25（短数字对照） + ob-28（短字符串对照）
    #                 + ob-31（40k 键，maxNameLength 对照）
    #   parked(7):    ob-04/05/06/11/13/16 + ob-20（已发布过的坏行）
    #   skipped(7):   ob-07（非法 JSON）、ob-12（孤儿）、ob-21（字母 v 结尾的非法 JSON）、
    #                 ob-23（嵌套 250 层）、ob-24（2000 位数字）、ob-27（2 MB 字符串）、
    #                 ob-30（60k 键，maxNameLength 超限）
    #   ob-09/10/17 不在范围内（控制行 / 已修复 / 已是完整信封）。
    check("repaired 13 event(s), parked 7 event(s), skipped 7 event(s)" in v33_out,
          f"首次执行的 NOTICE 计数符合预期（13 repaired / 7 parked / 7 skipped；输出：{v33_out.strip()!r}）")

    rows, audit = snapshot()
    by_id = {r["outbox_id"]: r for r in rows}
    # ob-16 / ob-17 的 outbox id 是 md5 指纹而不是字面量，按 aggregate_id 再建一层索引。
    by_job = {r["aggregate_id"]: r for r in rows if r.get("aggregate_id")}
    audit_by_id: dict[str, list[dict]] = {}
    for a in audit:
        audit_by_id.setdefault(a["outbox_id"], []).append(a)

    # payload 可能本身就不是合法 JSON，因此在 python 侧判断类型，
    # 避免 SQL 里 CAST 失败把整条查询打断。
    def safe_payload_type(raw: str) -> str:
        if raw is None or raw.strip() == "":
            return "null"
        try:
            return json_typeof_str(json.loads(raw))
        except Exception:
            return "invalid-json"

    def json_typeof_str(value) -> str:
        if isinstance(value, dict):
            return "object"
        if isinstance(value, list):
            return "array"
        if isinstance(value, str):
            return "string"
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, (int, float)):
            return "number"
        return "null"

    def payload_keys(raw: str) -> set[str]:
        try:
            parsed = json.loads(raw)
        except Exception:
            return set()
        return set(parsed.keys()) if isinstance(parsed, dict) else set()

    # ---------- 可发送路径 ----------
    EXPECTED_JOB_ID = {
        "ob-01": "job-01",
        "ob-02": "job-02",
        "ob-03": "job-03",
        "ob-08": "job-08",
        "ob-10": "job-10",
    }
    sendable = {
        "ob-01": ({"mediaType", "objectName"}, "params 是 JSON 对象"),
        "ob-02": (set(), "params IS NULL"),
        "ob-03": (set(), "params 是空串"),
        "ob-08": ({"mediaType", "windowDuration"}, "V31 已写坏的既有非对象 payload"),
        "ob-14": (set(), "params 只有空白字符（btrim 后为空）"),
        "ob-15": (set(), "params 是空对象 {}"),
        "ob-22": (set(), "[B2] params 是 chr(11)+空白 -> 视为空，必须修复"),
        "ob-25": ({"n"}, "[MAJOR-1 对照] 短数字不得被数值长度守卫误杀"),
        "ob-26": ({"mediaType"}, "[信封守卫] 既有 payload 信封键为 null，必须重建而不是跳过"),
        "ob-28": ({"s"}, "[maxStringLength 对照] 短字符串不得被长度守卫误杀"),
        "ob-31": ({"k" * 40000}, "[maxNameLength 对照] 40k 键不得被键长度守卫误杀"),
    }
    EXPECTED_JOB_ID["ob-14"] = "job-14"
    EXPECTED_JOB_ID["ob-15"] = "job-15"
    EXPECTED_JOB_ID["ob-18"] = "job-18"
    EXPECTED_JOB_ID["ob-19"] = "job-19"
    EXPECTED_JOB_ID["ob-22"] = "job-22"
    EXPECTED_JOB_ID["ob-25"] = "job-25"
    EXPECTED_JOB_ID["ob-26"] = "job-26"
    EXPECTED_JOB_ID["ob-28"] = "job-28"
    EXPECTED_JOB_ID["ob-31"] = "job-31"
    for oid, (extra_keys, label) in sendable.items():
        cases_executed.append(f"{oid}: {label}")
        # ob-26 的 outbox id 是 md5 指纹，只能按 aggregate_id 定位。
        row = by_job[EXPECTED_JOB_ID[oid]] if oid == "ob-26" else by_id[oid]
        check(row["last_error"] == "repaired by V33", f"{oid} last_error='repaired by V33'（实际 {row['last_error']!r}）")
        check(safe_payload_type(row["payload"]) == "object",
              f"{oid} payload jsonb_typeof=object（实际 {safe_payload_type(row['payload'])}）")
        keys = payload_keys(row["payload"])
        check({"id", "type", "priority"} <= keys, f"{oid} payload 含 id/type/priority（实际 {sorted(keys)}）")
        check(keys == {"id", "type", "priority"} | extra_keys,
              f"{oid} payload 键集合精确等于 params 顶层键 + id/type/priority（实际 {sorted(keys)}）")
        check(row["attempt"] == 0, f"{oid} attempt 重置为 0")
        check(row["published_at"] is None, f"{oid} published_at IS NULL（可重新投递）")
        check(row["publisher_would_send"] is True, f"{oid} publisher 谓词 (published_at IS NULL AND next_retry_at <= now) 成立")
        # 真正复验 worker 契约：json.loads -> dict
        try:
            parsed = json.loads(row["payload"])
        except Exception as exc:  # noqa: BLE001
            parsed = None
            fail(f"{oid} payload 不是合法 JSON: {exc}")
        expected_job = EXPECTED_JOB_ID[oid]
        if parsed is not None:
            check(isinstance(parsed, dict), f"{oid} payload 反序列化为 dict（TaskOutboxPublisher/ai-service 要求）")
            if isinstance(parsed, dict):
                check(parsed.get("id") == expected_job,
                      f"{oid} payload.id == {expected_job}（实际 {parsed.get('id')!r}）")
                check(isinstance(parsed.get("type"), str) and parsed["type"] != "",
                      f"{oid} payload.type 是非空字符串（实际 {parsed.get('type')!r}）")
                check(isinstance(parsed.get("priority"), str) and parsed["priority"] != "",
                      f"{oid} payload.priority 是非空字符串（实际 {parsed.get('priority')!r}）")
        _entries = [a for a in audit if a["outbox_id"] == row["outbox_id"]]
        check(len(_entries) == 1 and _entries[0]["outcome"] == "repaired",
              f"{oid} 审计表有且仅有一条 repaired 记录")
        observed.append(f"{oid} payload={row['payload']}  last_error={row['last_error']}")

    # ---------- park / skip 路径 ----------
    parked = {
        "ob-04": ("array", "parked", "params 是 JSON array，且已发布过"),
        "ob-05": ("string", "parked", "params 是 JSON string"),
        "ob-06": ("number", "parked", "params 是 JSON number"),
        "ob-11": ("boolean", "parked", "params 是 JSON boolean"),
        "ob-07": (None, "skipped_not_json", "params 不是合法 JSON"),
        "ob-12": (None, "skipped_not_json", "孤儿事件：analysis_jobs 行不存在"),
        "ob-13": ("array", "parked", "既有非对象 payload + params 数组（双判型）"),
        "ob-16": ("array", "parked",
                  "生产时序：last_error 已被 publisher 覆写，靠 V29 md5 指纹才找到"),
        "ob-20": ("array", "parked",
                  "[R11-2] 已发布过的坏行：park 必须同时躲开 reconciler（否则 ~300s 后复活）"),
        "ob-21": (None, "skipped_not_json",
                  "[B2] params 以字母 v 结尾的非法 JSON（旧 btrim 会把它洗成合法 JSON）"),
        "ob-23": (None, "skipped_not_json",
                  "[M1] params 嵌套 250 层，超过 publisher/守卫上限"),
        "ob-24": (None, "skipped_not_json",
                  "[MAJOR-1] params 含 2000 位数字，超过 Jackson maxNumberLength"),
        "ob-27": (None, "skipped_not_json",
                  "[maxStringLength] params 含 2 MB 字符串，超过 Jackson maxStringLength"),
        "ob-30": (None, "skipped_not_json",
                  "[maxNameLength] params 含 60000 字符的对象键，超过 Jackson maxNameLength"),
    }
    # 夹具的字面 outbox id 是 ob-NN；md5 指纹夹具只能按 aggregate_id 定位。
    PARK_LOOKUP = {
        "ob-04": ("id", "ob-04"),
        "ob-05": ("id", "ob-05"),
        "ob-06": ("id", "ob-06"),
        "ob-11": ("id", "ob-11"),
        "ob-07": ("id", "ob-07"),
        "ob-12": ("id", "ob-12"),
        "ob-13": ("id", "ob-13"),
        "ob-16": ("job", "job-16"),
        "ob-20": ("id", "ob-20"),
        "ob-21": ("id", "ob-21"),
        "ob-23": ("id", "ob-23"),
        "ob-24": ("id", "ob-24"),
        "ob-27": ("id", "ob-27"),
        "ob-30": ("id", "ob-30"),
    }
    for oid, (jtype, outcome, label) in parked.items():
        cases_executed.append(f"{oid}: {label}")
        kind, key = PARK_LOOKUP[oid]
        row = by_id[key] if kind == "id" else by_job[key]
        # 两类"对象但不可发送"的特例：断言要精确到守卫给出的原因，
        # 而不是笼统的 "params is not valid json"（那会掩盖守卫是否真的生效）。
        if oid == "ob-23":
            check((row["last_error"] or "").startswith(
                      "repair_failed: params nesting depth "),
                  f"{oid} last_error 指出嵌套深度（实际 {row['last_error']!r}）")
            check("exceeds publisher limit" in (row["last_error"] or ""),
                  f"{oid} last_error 说明超出发布器上限")
        elif oid == "ob-24":
            check((row["last_error"] or "").startswith(
                      "repair_failed: params has a numeric literal of "),
                  f"{oid} last_error 指出数值字面量过长（实际 {row['last_error']!r}）")
            check("2000 digits" in (row["last_error"] or ""),
                  f"{oid} last_error 给出实际位数")
        elif oid == "ob-30":
            check((row["last_error"] or "").startswith(
                      "repair_failed: params has an object key of "),
                  f"{oid} last_error 指出对象键过长（实际 {row['last_error']!r}）")
            check("60000 chars" in (row["last_error"] or ""),
                  f"{oid} last_error 给出实际键长")
        elif oid == "ob-27":
            check((row["last_error"] or "").startswith(
                      "repair_failed: params has a string of "),
                  f"{oid} last_error 指出字符串过长（实际 {row['last_error']!r}）")
            check("chars" in (row["last_error"] or ""),
                  f"{oid} last_error 给出实际字符数")
        elif outcome == "parked":
            check(row["last_error"] == f"repair_failed: params is {jtype}",
                  f"{oid} last_error='repair_failed: params is {jtype}'（实际 {row['last_error']!r}）")
        elif oid == "ob-12":
            check(row["last_error"] == "repair_failed: analysis_jobs row missing",
                  f"{oid} last_error='repair_failed: analysis_jobs row missing'（实际 {row['last_error']!r}）")
        else:
            check(row["last_error"] == "repair_failed: params is not valid json",
                  f"{oid} last_error='repair_failed: params is not valid json'（实际 {row['last_error']!r}）")
        if oid == "ob-12":
            # 孤儿事件的 V29 原始 payload 本身是合法 JSON 对象（{"mediaType":"video"}），
            # 但它缺少 id/type/priority 封装 —— 正是最初那个"能被 ObjectMapper 解析、
            # 却让 worker claim 404 后无限重试"的形态。V33 的职责是让它不再被发送，
            # 而不是伪造一个聚合 id。所以这里断言"没有信封键 + publisher 选不中"。
            check(safe_payload_type(row["payload"]) == "object",
                  f"{oid} 保留原有 payload 形态（jsonb_typeof={safe_payload_type(row['payload'])}）")
        else:
            check(safe_payload_type(row["payload"]) != "object",
                  f"{oid} 没有生成对象 payload（实际 jsonb_typeof={safe_payload_type(row['payload'])}，payload={row['payload']!r}）")
        check(not ({"id", "type", "priority"} <= payload_keys(row["payload"])),
              f"{oid} payload 不含 id/type/priority，不是可发送消息")
        check(row["publisher_would_send"] is False,
              f"{oid} publisher 谓词不成立（不会被投递）")
        check(row["parked_far_future"] is True, f"{oid} next_retry_at 在 50 年以上（实际 {row['next_retry_at']}）")
        # [R11-2] park 必须同时躲开两条再发送路径：
        # findPending 需要 published_at IS NULL，
        # findStalePublishedForQueuedJobs 需要 published_at IS NOT NULL。
        # 只推远 next_retry_at、保留 published_at 会让 reconciler
        # 在 ~300s 后把坏 payload 重新武装。
        # 因此 park 后 published_at 必须为 NULL。
        check(row["published_at"] is None,
              f"{oid} park 后 published_at 已置 NULL（躲开 reconciler）")
        check(row["selected_by_reconciler"] is False,
              f"{oid} 不会被 findStalePublishedForQueuedJobs 选中（幽死循环不复活）")
        entries = [a for a in audit if a["outbox_id"] == row["outbox_id"]]
        check(len(entries) == 1 and entries[0]["outcome"] == outcome,
              f"{oid} 审计表有且仅有一条 {outcome} 记录")
        observed.append(
            f"{oid} payload={row['payload']!r}  last_error={row['last_error']}  "
            f"next_retry_at={row['next_retry_at']}")

    # ---------- 控制行 & 已修复行 ----------
    cases_executed.append("ob-09: 非 V29/V31 的控制行必须原封不动")
    ctl = by_id["ob-09"]
    check(ctl["last_error"] == "requeued by recovery", f"ob-09 last_error 未被改写（实际 {ctl['last_error']!r}）")
    check(ctl["payload"] == '{"id":"job-09"}', f"ob-09 payload 未被改写（实际 {ctl['payload']!r}）")
    check(ctl["attempt"] == 5, f"ob-09 attempt 未被改写（实际 {ctl['attempt']}）")
    check("ob-09" not in audit_by_id, "ob-09 没有审计记录")

    cases_executed.append("ob-10: 已 repaired by V33 的行必须原封不动（幂等夹具）")
    pre_by_id = {r["outbox_id"]: r for r in pre_rows}
    done = by_id["ob-10"]
    check(done["last_error"] == "repaired by V33", "ob-10 last_error 保持 repaired by V33")
    check(done["payload"] == pre_by_id["ob-10"]["payload"],
          f"ob-10 payload 未被改写（前 {pre_by_id['ob-10']['payload']!r} / 后 {done['payload']!r}）")
    check(done["next_retry_at"] == pre_by_id["ob-10"]["next_retry_at"], "ob-10 next_retry_at 未被改写")
    check("ob-10" not in audit_by_id, "ob-10 没有新增审计记录")

    cases_executed.append(
        "ob-17: V29 指纹命中但已是完整信封的行必须原封不动（禁止重复发送）")
    full = by_job["job-17"]
    pre_by_job = {r["aggregate_id"]: r for r in pre_rows if r.get("aggregate_id")}
    check(full["last_error"] is None, f"ob-17 last_error 保持 NULL（实际 {full['last_error']!r}）")
    pre_full = pre_by_job["job-17"]
    check(full["payload"] == pre_full["payload"],
          f"ob-17 payload 未被改写（前 {pre_full['payload']!r} / 后 {full['payload']!r}）")
    check(full["published_at"] is not None,
          "ob-17 published_at 保持非 NULL —— 没有被重新打开投递")
    check(full["attempt"] == pre_full["attempt"], "ob-17 attempt 未被重置")
    check(full["publisher_would_send"] is False, "ob-17 publisher 不会再次投递它")
    check(not [a for a in audit if a["outbox_id"] == full["outbox_id"]],
          "ob-17 没有审计记录（没有改动就不该记账）")

    # ---------- [MAJOR-2] 审计不可写时必须 fail closed ----------
    # 之前这一条只靠手工实验验证：把 RAISE EXCEPTION 改回 RAISE WARNING，
    # 整个 harness 仍然全绿。这里补行为夹具——让审计表 INSERT 必然失败，
    # 断言迁移**中止**且那一行保持原状（可发送 + 无审计 = 静默失修）。
    print("\n== 5a. [MAJOR-2] 审计不可写：迁移必须中止，行保持原状")
    cases_executed.append(
        "ob-29: [MAJOR-2] 审计 INSERT 失败 -> 迁移中止，行不被改动")
    psql("""
        INSERT INTO analysis_jobs (id, type, status, priority, progress, params, attempt)
        VALUES ('job-29', 'VIDEO_ANALYSIS', 'QUEUED', 'NORMAL', 0, '{"a":1}', 0);
        INSERT INTO task_outbox (id, aggregate_id, event_type, payload, attempt,
                                 next_retry_at, published_at, last_error)
        VALUES ('ob-29','job-29','ANALYSIS_JOB_QUEUED','{"a":1}',9,
                '2020-01-01 00:00:00', NULL, 'inserted by V29 backfill');
    """)
    psql("""
        CREATE OR REPLACE FUNCTION v33_block_audit() RETURNS trigger
        LANGUAGE plpgsql AS $f$
        BEGIN RAISE EXCEPTION 'audit storage unavailable (harness)'; END $f$;
        DROP TRIGGER IF EXISTS v33_block_audit_trg ON outbox_repair_audit;
        CREATE TRIGGER v33_block_audit_trg BEFORE INSERT ON outbox_repair_audit
            FOR EACH ROW EXECUTE FUNCTION v33_block_audit();
    """)
    pre_29 = {r["outbox_id"]: r for r in snapshot()[0]}.get("ob-29")
    aborted = False
    abort_output = ""
    try:
        abort_output = psql(v33_sql, with_notices=True)
    except RuntimeError as exc:
        aborted = True
        abort_output = str(exc)
    check(aborted, "审计不可写时迁移必须失败（fail closed），不能只 WARNING 后提交")
    check("cannot record repair outcome" in abort_output,
          f"失败信息必须点明原因（输出片段：{abort_output[-260:]!r}）")
    post_29 = {r["outbox_id"]: r for r in snapshot()[0]}.get("ob-29")
    check(post_29 is not None and pre_29 is not None, "ob-29 行仍存在")
    if post_29 and pre_29:
        check(post_29["last_error"] == "inserted by V29 backfill",
              f"ob-29 last_error 未被改写（实际 {post_29['last_error']!r}）")
        check(post_29["attempt"] == 9, f"ob-29 attempt 保持 9（实际 {post_29['attempt']}）")
        check(post_29["payload"] == pre_29["payload"], "ob-29 payload 未被改写")
    check(not [a for a in snapshot()[1] if a["outbox_id"] == "ob-29"],
          "ob-29 没有审计记录（迁移整体回滚，不留半成品）")
    # 清理触发器与夹具，后续段落还要能写审计表、还要做快照比较。
    psql("DROP TRIGGER IF EXISTS v33_block_audit_trg ON outbox_repair_audit;"
         "DROP FUNCTION IF EXISTS v33_block_audit();")
    psql("DELETE FROM task_outbox WHERE id = 'ob-29';"
         "DELETE FROM analysis_jobs WHERE id = 'job-29';")

    # ---------- 幂等：第二次执行 ----------
    print("\n== 5. 第二次执行 V33（幂等性）")
    rows_2_before, audit_2_before = rows, audit
    v33_out2 = psql(v33_sql, with_notices=True)
    for line in v33_out2.splitlines():
        if line.strip():
            print("      " + line.strip())
    rows_2, audit_2 = snapshot()
    cases_executed.append("第二次执行 V33：行数据与审计行完全不变")

    check(rows_2 == rows_2_before, "第二次执行后 task_outbox 快照与第一次完全相同")
    check(audit_2 == audit_2_before, "第二次执行后审计表与第一次完全相同")
    # 断言完整的计数三元组，而不是只找一个子串：只含 "repaired 0 event(s)"
    # 的输出也可能在 parked/skipped 上出了问题，那样幂等同样被破坏。
    check("repaired 0 event(s), parked 0 event(s), skipped 0 event(s)" in v33_out2,
          f"第二次执行三个计数必须全为 0（输出：{v33_out2.strip()!r}）")

    def _json_obj(text):
        """Parse JSON into a dict, or None when it is not a JSON object."""
        try:
            value = json.loads(text) if text else None
        except (ValueError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    def envelope_nonnull(row):
        payload = _json_obj(row["payload"])
        if payload is None:
            return False
        return all(payload.get(k) is not None for k in ("id", "type", "priority"))

    def payload_id_equals_job(row):
        payload = _json_obj(row["payload"])
        return payload is not None and payload.get("id") == row["aggregate_id"]

    def payload_keys_ok(row):
        """payload 的键必须恰好 = params 顶层键 ∪ 信封三键（不许多、不许少）。"""
        payload = _json_obj(row["payload"])
        if payload is None:
            return False
        params = _json_obj(row["job_params"]) or {}
        return set(payload) == set(params) | {"id", "type", "priority"}

    # ---------- [R11-1] last_error IS NULL 的行必须被修复 ----------
    print("\n== 5b. [R11-1] last_error IS NULL + 信封缺失：必须修复")
    cases_executed.append(
        "ob-18: [R11-1] last_error NULL（已成功投递过、无信封）-> 必须修复为完整信封")
    r18 = by_job["job-18"]
    check(r18["last_error"] == "repaired by V33",
          "ob-18 已被修复（实际 last_error=%r）；若仍为 NULL，说明 NULL 不安全"
          "的比较把该行整条漏掉，V33 对它完全失效" % (r18["last_error"],))
    check(safe_payload_type(r18["payload"]) == "object",
          "ob-18 payload 是 JSON 对象（实际 jsonb_typeof=%s）" % safe_payload_type(r18["payload"]))
    check(envelope_nonnull(r18) is True,
          "ob-18 信封三键均非 null（payload=%r）" % (r18["payload"],))
    check(payload_id_equals_job(r18) is True,
          "ob-18 payload.id == 任务 id（payload=%r）" % (r18["payload"],))
    check(payload_keys_ok(r18) is True,
          "ob-18 payload 键集合 = params 顶层键 + 信封三键")
    check(r18["publisher_would_send"] is True, "ob-18 修复后重新进入可投递状态")
    entries18 = [a for a in audit if a["outbox_id"] == r18["outbox_id"]]
    check(len(entries18) == 1 and entries18[0]["outcome"] == "repaired",
          "ob-18 有且仅有一条 repaired 审计记录（否则就是静默失修）")

    # ---------- [R11-7] params 不得覆盖信封 ----------
    print("\n== 5c. [R11-7] params 自带 null 的 id/type/priority 不得覆盖信封")
    cases_executed.append(
        "ob-19: [R11-7] params 含 null 的 id/type/priority -> 信封必须胜出")
    r19 = by_job["job-19"]
    check(r19["last_error"] == "repaired by V33",
          "ob-19 已修复（实际 last_error=%r）" % (r19["last_error"],))
    check(envelope_nonnull(r19) is True,
          "ob-19 信封三键均非 null（否则 worker claim 404；payload=%r）" % (r19["payload"],))
    check(payload_id_equals_job(r19) is True,
          "ob-19 payload.id == 任务 id，未被 params 替换（payload=%r）" % (r19["payload"],))

    # ---------- 审计表结构 ----------
    print("\n== 6. 审计表存在且带注释")
    meta = psql_json_rows(r"""
        SELECT row_to_json(t)::text FROM (
          SELECT c.relname AS table_name,
                 obj_description(c.oid, 'pg_class') IS NOT NULL AS has_table_comment,
                 (SELECT count(*) FROM information_schema.columns
                   WHERE table_name = 'outbox_repair_audit') AS column_count
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE c.relname = 'outbox_repair_audit' AND n.nspname = 'public'
        ) t;""")
    check(len(meta) == 1, "outbox_repair_audit 表已创建")
    if meta:
        check(meta[0]["has_table_comment"] is True, "outbox_repair_audit 有 COMMENT ON TABLE")
        # 表结构是固定的 6 列（id/outbox_id/aggregate_id/outcome/detail/created_at）；
        # 用 == 才能发现"多了一列"这种同样破坏契约的漂移。
        check(int(meta[0]["column_count"]) == 6,
              f"outbox_repair_audit 列数 = {meta[0]['column_count']}（应为 6）")

    payload_comment = psql_json_rows(r"""
        SELECT row_to_json(t)::text FROM (
          SELECT col_description('task_outbox'::regclass, attnum) AS comment
            FROM pg_attribute
           WHERE attrelid = 'task_outbox'::regclass AND attname = 'payload'
        ) t;""")
    check(bool(payload_comment) and (payload_comment[0]["comment"] or "").find("V33") >= 0,
          "task_outbox.payload 的 COMMENT 已由 V33 更新（含 V33 字样）")

    # ---------- 汇总 ----------
    summary = {
        "generatedAt": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "container": CONTAINER,
        "database": PROOF_DB,
        "migrations": {
            "prerequisiteReplay": applied,
            "underTest": V33.name,
            "note": "V1..V32 用 psql 按数字顺序真实重放（含 V29/V31 原始语句），随后插入夹具，再执行真实 V33 文件；V29/V31 文本未改动",
        },
        "casesExecuted": cases_executed,
        "observedPayloads": observed,
        "mustNotSend": {
            oid: {
                "payload": by_id[oid]["payload"],
                "last_error": by_id[oid]["last_error"],
                "next_retry_at": by_id[oid]["next_retry_at"],
                "publisherWouldSend": by_id[oid]["publisher_would_send"],
            }
            for oid in ("ob-04", "ob-05", "ob-06", "ob-07", "ob-11")
        },
        "auditRows": audit,
        "idempotency": {
            "secondRunRowSnapshotIdentical": rows_2 == rows_2_before,
            "secondRunAuditSnapshotIdentical": audit_2 == audit_2_before,
            "secondRunOutput": v33_out2.strip(),
        },
        "v33Output": v33_out.strip(),
        "failures": failures,
        "passed": not failures,
    }
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nevidence written: {EVIDENCE}")

    print("\n" + json.dumps(
        {k: summary[k] for k in ("container", "database", "passed", "failures")},
        ensure_ascii=False, indent=2))

    if failures:
        print(f"\n{len(failures)} assertion(s) FAILED", file=sys.stderr)
        return 1
    print("\nALL ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    if not V33.exists():
        raise SystemExit(f"missing migration: {V33}")
    sys.exit(main())
