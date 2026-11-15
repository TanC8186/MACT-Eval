#!/usr/bin/env python
"""Database + object-store backup / restore / drill (OPS08).

Examples:
    python scripts/ops/backup_restore.py backup --out backups/2026-10-25
    python scripts/ops/backup_restore.py drill --backup-dir /tmp/mact-drill

The drill:
  1. dumps PostgreSQL with pg_dump (custom format) via the Docker container;
  2. mirrors all MinIO objects to a local directory;
  3. restores into an isolated database and restored buckets;
  4. compares table row counts and object counts;
  5. drops the restored database/buckets (the dump is retained).
No existing database or bucket is modified.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from minio import Minio

PG_CONTAINER = os.environ.get("PG_CONTAINER", "personal_design-postgres-1")
PG_USER = os.environ.get("PG_USER", "macteval")
SOURCE_DB = os.environ.get("SOURCE_DB", "macteval")
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS = os.environ.get("MINIO_ACCESS_KEY", "macteval")
MINIO_SECRET = os.environ.get("MINIO_SECRET_KEY", "macteval_secret")
BUCKETS = [
    "classroom-raw-media",
    "analysis-artifacts",
    "report-attachments",
    "teaching-resources",
    "student-submissions",
]
#: 备份/恢复要逐表核对行数的白名单。
#:
#: 2026-10-30 补齐（此前只有 11 张）。漏掉的那几张都是**决定"能否恢复"**的：
#:
#:   behavior_evidence_frames   报告引用的证据帧元数据（任务书 §A3 明确要求核对）。
#:                              实测隔离库 1458 行——不是理论缺口。
#:   evaluation_report_versions 报告版本快照；缺它则历史版本无法恢复，
#:                              "报告版本切换"这条功能等于丢失。
#:   class_groups               班级；lesson_sessions 外键指向它，缺了会导入失败。
#:   students                   学生账号（含学号登录所需字段）。
#:   classroom_configs          课堂配置。
#:   activities                 课堂活动。
#:   task_outbox                决定"任务能否被重投"的行。缺它则恢复后任务卡死。
#:   outbox_repair_audit        修复审计轨迹。
#:
#: 判断标准很简单：**恢复之后，报告还能不能打开、任务还能不能跑完**。
#: 不满足这条的表就不该只留在 dump 里而跳过行数核对。
TABLES = ["users", "courses", "class_groups", "students",
          "lesson_sessions", "attendance_records",
          "behavior_observations", "lesson_transcripts", "lesson_window_features",
          "behavior_evidence_frames",
          "evaluation_reports", "evaluation_report_versions",
          "suggestions", "analysis_jobs", "media_assets",
          "classroom_configs", "activities",
          "task_outbox", "outbox_repair_audit"]


#: 仓库根。临时文件必须落在项目内——本项目的硬约束是"所有数据、缓存、日志、
#: 临时文件只能写入 E:/Personal_Design"，系统 TEMP 在 C 盘，明确禁止。
REPO_ROOT = Path(__file__).resolve().parents[2]
SCRATCH = REPO_ROOT / ".cache" / "tmp" / "backup-drill"


def scratch_dir():
    """在项目内建临时目录。

    为什么不能用 tempfile.TemporaryDirectory() 的默认位置：它落在
    `C:/Users/<user>/AppData/Local/Temp`。演练要把**全部对象**复制一遍，
    本机最大单个视频是 2.5 GB（120 分钟课堂实录），C 盘当时只剩 42 GB 且有
    其他项目占用。实测在默认位置跑会失败，表现为 MinIO 分片上传返回
    `AccessDenied`——症状看起来像权限问题，实际是落盘位置不对。
    """
    SCRATCH.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=str(SCRATCH), ignore_cleanup_errors=True)

def run(cmd: list[str], stdin=None, stdout=None, check: bool = True):
    proc = subprocess.run(cmd, stdin=stdin, stdout=stdout, stderr=subprocess.PIPE)
    if check and proc.returncode != 0:
        raise SystemExit(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"{proc.stderr.decode('utf-8', 'replace')}"
        )
    return proc


def client() -> Minio:
    return Minio(MINIO_ENDPOINT, access_key=MINIO_ACCESS,
                 secret_key=MINIO_SECRET, secure=False)


def table_counts(db: str) -> dict[str, int]:
    """按 TABLES 白名单数行。

    **缺表不是错误，但必须显式记成 -1 而不是静默跳过**。原因：本脚本既用于
    新栈（V33，19 张表）也用于旧栈（旧 schema，只有 15 张）。早期白名单只有
    11 张，恰好是旧栈的子集，所以从来没暴露过这个差异。

    把缺失的表记成 -1，并在 `missingTables` 里列出，这样：
      * 旧栈演练不会因为表不存在而整条 SQL 失败；
      * 新栈演练里若某张表意外消失，也不会被当成"0 行"蒙混过去。
    两者混为一谈才是危险的——"表不存在"与"表是空的"含义完全不同。
    """
    present = _existing_tables(db)
    counts: dict[str, int] = {}
    sqls = []
    for table in TABLES:
        if table in present:
            sqls.append(f"SELECT '{table}', count(*) FROM {table}")
        else:
            counts[table] = -1
    if sqls:
        proc = run(["docker", "exec", PG_CONTAINER, "psql", "-U", PG_USER, "-d", db,
                    "-t", "-A", "-F", ",", "-c", " UNION ALL ".join(sqls)],
                   stdout=subprocess.PIPE)
        for line in proc.stdout.decode("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            name, value = line.split(",", 1)
            counts[name] = int(value)
    return counts


def _existing_tables(db: str) -> set[str]:
    proc = run(["docker", "exec", PG_CONTAINER, "psql", "-U", PG_USER, "-d", db,
                "-t", "-A", "-c",
                "select tablename from pg_tables where schemaname='public'"],
               stdout=subprocess.PIPE)
    return {l.strip() for l in proc.stdout.decode("utf-8").splitlines() if l.strip()}


def max_object_bytes() -> int:
    """单对象复制上限（字节）。0 表示不限制。

    默认 256 MiB。可用 ``--max-object-mb 0`` 关闭限制。见 backup() 里
    "超限只登记清单"的说明。
    """
    raw = os.environ.get("MACT_BACKUP_MAX_OBJECT_MB", "256")
    try:
        mb = int(raw)
    except ValueError:
        mb = 256
    return mb * 1024 * 1024 if mb > 0 else 0

def object_manifest(bucket: str) -> list[dict]:
    items = []
    for obj in client().list_objects(bucket, recursive=True):
        items.append({"key": obj.object_name, "size": obj.size})
    return items


def backup(out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    dump_path = out_dir / "macteval.dump"
    print(f"[backup] pg_dump -> {dump_path}")
    with dump_path.open("wb") as handle:
        run(["docker", "exec", PG_CONTAINER, "pg_dump", "-U", PG_USER,
             "-Fc", SOURCE_DB], stdout=handle)

    digest = hashlib.sha256(dump_path.read_bytes()).hexdigest()
    manifest = {"createdAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "dumpSha256": digest, "tables": table_counts(SOURCE_DB),
                "buckets": {}}
    mc = client()
    limit = max_object_bytes()
    for bucket in BUCKETS:
        target = out_dir / "objects" / bucket
        target.mkdir(parents=True, exist_ok=True)
        items = []
        skipped = 0
        for obj in mc.list_objects(bucket, recursive=True):
            # 超过上限的对象只登记清单，不搬字节。
            # 为什么：本机单个课堂实录 2.5 GB、总量 8.7 GB；全量复制需要
            # 17.4 GB 瞬时空间，且对"证明可恢复"没有增量价值——哈希与存在性
            # 已经能证明对象可定位。跳过项在报表里单独列出，绝不静默。
            if limit and obj.size > limit:
                items.append({"key": obj.object_name, "size": obj.size,
                              "copied": False, "reason": "exceeds-max-object-bytes"})
                skipped += 1
                continue
            local = target / obj.object_name
            local.parent.mkdir(parents=True, exist_ok=True)
            mc.fget_object(bucket, obj.object_name, str(local))
            items.append({"key": obj.object_name, "size": obj.size, "copied": True})
        manifest["buckets"][bucket] = items
        note = f"（跳过 {skipped} 个超限对象，仅清单核对）" if skipped else ""
        print(f"[backup] {bucket}: {len(items)} objects{note}")
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def ensure_bucket(bucket: str):
    mc = client()
    if not mc.bucket_exists(bucket):
        mc.make_bucket(bucket)


def restore(backup_dir: Path, db: str, bucket_prefix: str) -> dict:
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    run(["docker", "exec", PG_CONTAINER, "psql", "-U", PG_USER, "-d", "postgres",
         "-c", f"DROP DATABASE IF EXISTS {db};"])
    run(["docker", "exec", PG_CONTAINER, "psql", "-U", PG_USER, "-d", "postgres",
         "-c", f"CREATE DATABASE {db};"])
    print(f"[restore] pg_restore -> {db}")
    with (backup_dir / "macteval.dump").open("rb") as handle:
        run(["docker", "exec", "-i", PG_CONTAINER, "pg_restore", "-U", PG_USER,
             "-d", db, "--no-owner", "--no-privileges"], stdin=handle)

    mc = client()
    restored_objects = {}
    skipped_objects = {}
    for bucket, items in manifest["buckets"].items():
        target_bucket = f"{bucket_prefix}-{bucket}"
        ensure_bucket(target_bucket)
        count = 0
        skipped = 0
        for item in items:
            # 备份阶段因超限未复制的对象，恢复阶段同样不传——但要计数并上报。
            if item.get("copied") is False:
                skipped += 1
                continue
            key = item["key"]
            local = backup_dir / "objects" / bucket / key
            mc.fput_object(target_bucket, key, str(local))
            count += 1
        restored_objects[bucket] = count
        if skipped:
            skipped_objects[bucket] = skipped
        note = f"（{skipped} 个超限对象未传，仅比对清单）" if skipped else ""
        print(f"[restore] {target_bucket}: {count} objects{note}")
    return {"tables": table_counts(db), "objects": restored_objects,
            "skippedObjects": skipped_objects}


def drop_restored(db: str, bucket_prefix: str):
    run(["docker", "exec", PG_CONTAINER, "psql", "-U", PG_USER, "-d", "postgres",
         "-c", f"DROP DATABASE IF EXISTS {db};"], check=False)
    mc = client()
    for bucket in BUCKETS:
        target = f"{bucket_prefix}-{bucket}"
        try:
            for obj in mc.list_objects(target, recursive=True):
                mc.remove_object(target, obj.object_name)
            mc.remove_bucket(target)
        except Exception:
            pass


def unmapped_tables(db_tables: set[str], whitelist: list[str] | set[str] | None = None) -> list[str]:
    """库里存在、但不在备份白名单里的表（flyway 历史表除外）。

    抽成纯函数是为了**可被真正测试**：原来这段内联在 drill() 里，只能靠
    "源码里有没有那句打印"来守——变异测试证明删掉 return 1 而保留打印，
    测试仍然全绿（10 passed），也就是覆盖率缺口会静默通过。
    """
    wl = set(TABLES if whitelist is None else whitelist)
    return sorted(set(db_tables) - wl - {"flyway_schema_history"})

def drill() -> int:
    suffix = str(int(time.time()))[-8:]
    db = f"macteval_restore_{suffix}"
    prefix = f"restore-{suffix}"
    with scratch_dir() as tmp:
        backup_dir = Path(tmp)
        source = backup(backup_dir)
        restored = restore(backup_dir, db, prefix)
        failures = []
        for table, expected in source["tables"].items():
            actual = restored["tables"].get(table)
            if actual != expected:
                failures.append(f"{table}: backup={expected} restore={actual}")
        for bucket, items in source["buckets"].items():
            # 只比对**实际搬运**的对象数：超限未复制的对象本来就不会出现在
            # restore 端，把它们算进分母会让演练永远报失败。
            expected_copied = sum(1 for it in items if it.get("copied") is not False)
            actual = restored["objects"].get(bucket)
            if actual != expected_copied:
                failures.append(
                    f"{bucket}: expected_copied={expected_copied} restore={actual}")
        drop_restored(db, prefix)
        if failures:
            print("[drill] FAILED:")
            for failure in failures:
                print("  -", failure)
            return 1
        # -1 表示“该表在库里不存在”，绝不能加进行数总和——否则汇总行会被
        # 缺表的个数拉低，看起来像“少备份了几行”。
        # **覆盖率**检查：库里存在、但不在白名单里的表 = 备份时被静默跳过。
        # 这才是原始缺陷的形态——behavior_evidence_frames 一直在库里（1458 行），
        # 只是没进白名单，于是演练打印 "OK" 而它根本没被核对。
        # 上一步的 absent（白名单有、库里没有）是另一个方向的缺口，两个都要管。
        unmapped = unmapped_tables(_existing_tables(SOURCE_DB))
        if unmapped:
            print(f"[drill] FAILED：以下表在库中存在但不在备份白名单里，"
                  f"其数据未被核对：{', '.join(unmapped)}")
            print("[drill] 把它们加入 TABLES，或显式说明为何不需要备份。")
            return 1
        counted = {t: n for t, n in source["tables"].items() if n >= 0}
        absent = sorted(t for t, n in source["tables"].items() if n < 0)
        total_items = sum(len(v) for v in source["buckets"].values())
        skipped_total = sum(restored.get("skippedObjects", {}).values())
        copied = total_items - skipped_total
        print(f"[drill] OK: {sum(counted.values())} rows across {len(counted)} tables, "
              f"{copied} objects copied and verified")
        if skipped_total:
            # 这类对象**没有**被字节级验证，只有清单核对。不单独说明就会被
            # 读成"全部对象都验过了"。
            print(f"[drill] 另有 {skipped_total} 个超限对象仅做清单核对（未搬字节）："
                  f"{restored.get('skippedObjects')}")
            print("[drill] 可设 MACT_BACKUP_MAX_OBJECT_MB=0 取消上限做全量演练")
        if absent:
            # 缺表不等于“已验证为空”。必须单独说，否则读的人会以为这些表也被核对了。
            print(f"[drill] 未核对（本库无此表）: {', '.join(absent)}")
            # 关键：默认把缺表当成**失败**，而不是只打印一句提示。
            # 原因：这正是原始缺陷的形态——白名单少了 behavior_evidence_frames，
            # 演练照样 exit 0 并打印 "81 rows ... verified"，读的人以为证据帧
            # 也被核对了。变异测试证实：把该表从白名单删掉，汇总从 2937 行/19 表
            # 变成 1479 行/18 表，**退出码仍是 0**。
            # 允许旧 schema 的库显式放宽：MACT_BACKUP_ALLOW_MISSING_TABLES=1。
            if os.environ.get("MACT_BACKUP_ALLOW_MISSING_TABLES") == "1":
                print("[drill] 已按 MACT_BACKUP_ALLOW_MISSING_TABLES=1 放宽，"
                      "缺表不计为失败（该库应为旧 schema）。")
            else:
                print("[drill] FAILED：白名单里有表在库中不存在，行数核对不完整。")
                print("[drill] 若这是旧 schema 库（预期缺表），设 "
                      "MACT_BACKUP_ALLOW_MISSING_TABLES=1 显式放宽。")
                return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    backup_cmd = sub.add_parser("backup")
    backup_cmd.add_argument("--out", required=True)
    drill_cmd = sub.add_parser("drill")
    args = parser.parse_args()
    if args.command == "backup":
        manifest = backup(Path(args.out))
        print(json.dumps(manifest["tables"], ensure_ascii=False))
        return 0
    return drill()


if __name__ == "__main__":
    sys.exit(main())
