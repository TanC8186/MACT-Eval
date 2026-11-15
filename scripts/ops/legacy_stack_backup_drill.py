#!/usr/bin/env python
"""旧存量栈的备份 → 隔离恢复演练（任务书 A3，2026-10-28）。

    python -I scripts/ops/legacy_stack_backup_drill.py
    python -I scripts/ops/legacy_stack_backup_drill.py --skip-objects

**只读源，写入隔离目标。** 这是任务书 A3 的要求：在把 `personal_design-*`
切到新的 E 盘 bind mount 之前，必须先证明备份能恢复。具体边界：

* 源（**只读**）：`personal_design-postgres-1` 的 `macteval` 库、`:9000` 的 MinIO。
  只执行 `pg_dump` 与 `list_objects/fget_object`，不写、不删、不停。
* 目标（**可写，但是隔离的**）：`mact-ui-postgres` 里新建
  `macteval_legacy_restore_<ts>` 库、`mact-ui-minio` 里新建
  `restore-<ts>-<bucket>` 桶。演练结束即删除。
* 备份落 `data/tmp/legacy-backup-<date>/`（E 盘，被 .gitignore 排除）。

为什么不用 `scripts/ops/backup_restore.py drill`：它的 `PG_CONTAINER` 默认指向
用户容器，且 `drill` 会在**同一个** postgres 容器里建库/删库。对用户容器做写操作
不在本任务的授权范围内。本脚本把"读源"和"写目标"分到两个容器上。

诚实边界：这验证的是"备份文件可恢复且行数/对象数一致"，**不是**"新 compose
起来后应用能正常读这些数据"。后者需要跑容器，被 Docker 数据盘阻塞。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

from minio import Minio

ROOT = Path(__file__).resolve().parents[2]

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# --- 源：用户的存量栈（只读）------------------------------------------------
SRC_PG = "personal_design-postgres-1"
SRC_DB = "macteval"
SRC_PG_USER = "macteval"
SRC_MINIO = ("localhost:9000", "macteval", "macteval_secret")

# --- 目标：隔离 rig（可写）--------------------------------------------------
DST_PG = "mact-ui-postgres"
DST_MINIO = ("127.0.0.1:19100", "uirig", "uirig_secret")

#: 参加比对的对象桶。temporary-uploads 是纯写入口，本地可能为空，一并记录。
BUCKETS = ["classroom-raw-media", "analysis-artifacts", "report-attachments",
           "teaching-resources", "student-submissions", "temporary-uploads"]

#: 表清单**从源库动态读取**，不写死。
#: 为什么：写死的清单会与真实 schema 漂移——本轮就发现
#: `scripts/ops/backup_restore.py` 的 TABLES 里列了 `media_assets`，而用户库
#: 停在 Flyway V21、这张表（V24 才建）根本不存在，于是那次 drill 的第一步
#: `select count(*) from media_assets` 就会报 relation does not exist。
#: 动态读表能让"这次备份覆盖了哪些表"成为事实，而不是期望。


def run(cmd: list[str], stdin=None, stdout=None) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, stdin=stdin, stdout=stdout, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise SystemExit(
            f"命令失败 ({proc.returncode}): {' '.join(cmd)}\n"
            f"{proc.stderr.decode('utf-8', 'replace')[:800]}")
    return proc


def psql(container: str, user: str, db: str, sql: str) -> str:
    proc = run(["docker", "exec", container, "psql", "-U", user, "-d", db,
                "-t", "-A", "-F", "|", "-c", sql], stdout=subprocess.PIPE)
    return proc.stdout.decode("utf-8", "replace").strip()


def source_tables() -> list[str]:
    sql = ("select table_name from information_schema.tables "
           "where table_schema='public' and table_type='BASE TABLE' order by 1")
    out = psql(SRC_PG, SRC_PG_USER, SRC_DB, sql)
    return [line.strip() for line in out.splitlines() if line.strip()]


def counts(container: str, user: str, db: str, tables: list[str]) -> dict[str, int]:
    if not tables:
        return {}
    sql = " UNION ALL ".join(
        f"SELECT '{t}', count(*) FROM \"{t}\"" for t in tables)
    out = psql(container, user, db, sql)
    result = {}
    for line in out.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        name, value = line.split("|", 1)
        result[name] = int(value)
    return result


def minio(host: str, access: str, secret: str) -> Minio:
    return Minio(host, access_key=access, secret_key=secret, secure=False)


def object_inventory(client: Minio) -> dict[str, list[dict]]:
    inventory: dict[str, list[dict]] = {}
    existing = {b.name for b in client.list_buckets()}
    for bucket in BUCKETS:
        if bucket not in existing:
            inventory[bucket] = []
            continue
        items = [{"key": o.object_name, "size": o.size}
                 for o in client.list_objects(bucket, recursive=True)]
        inventory[bucket] = items
    return inventory


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-objects", action="store_true",
                        help="只演练数据库（对象共 388 MB，慢）")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    stamp = time.strftime("%Y%m%dT%H%M%S")
    out_dir = Path(args.out) if args.out else (
        ROOT / "data" / "tmp" / f"legacy-backup-{time.strftime('%Y-%m-%d')}")
    out_dir.mkdir(parents=True, exist_ok=True)
    if not str(out_dir.resolve()).lower().startswith(str(ROOT).lower()):
        raise SystemExit(f"备份目录必须在项目内：{out_dir}")

    evidence_path = (ROOT / "docs" / "reviews" / "evidence"
                     / f"legacy-backup-drill-{stamp}.json")

    print(f"==> 源:   {SRC_PG}/{SRC_DB} + minio {SRC_MINIO[0]}（只读）")
    print(f"==> 目标: {DST_PG} + minio {DST_MINIO[0]}（隔离，演练后删除）")
    print(f"==> 备份: {out_dir}")

    tables = source_tables()
    print(f"==> 源库表数: {len(tables)}")
    before = counts(SRC_PG, SRC_PG_USER, SRC_DB, tables)

    dump = out_dir / "macteval.dump"
    print(f"==> pg_dump -> {dump}")
    with dump.open("wb") as handle:
        run(["docker", "exec", SRC_PG, "pg_dump", "-U", SRC_PG_USER,
             "-Fc", SRC_DB], stdout=handle)
    dump_sha = hashlib.sha256(dump.read_bytes()).hexdigest()
    print(f"    {dump.stat().st_size:,} B  sha256={dump_sha[:16]}…")

    objects = {}
    if not args.skip_objects:
        src_minio = minio(*SRC_MINIO)
        objects = object_inventory(src_minio)
        total = sum(len(v) for v in objects.values())
        size = sum(i["size"] for v in objects.values() for i in v)
        print(f"==> 源对象: {total} 个 / {size / 1024 / 1024:.2f} MB")
        print("==> 镜像对象（只读源）")
        for bucket, items in objects.items():
            target = out_dir / "objects" / bucket
            target.mkdir(parents=True, exist_ok=True)
            for item in items:
                local = target / item["key"]
                local.parent.mkdir(parents=True, exist_ok=True)
                src_minio.fget_object(bucket, item["key"], str(local))
            print(f"    {bucket:<22} {len(items):>4}")

    # ---- 恢复到隔离目标 ----
    # S3 桶名只允许小写字母/数字/连字符（3–63 字符），而 stamp 里的 `T`
    # 是大写——实测 MinIO 直接返回 InvalidBucketName。库名不受此限制，
    # 但两者用同一个后缀更便于对照，所以统一用一个小写安全后缀。
    safe = "".join(ch for ch in stamp if ch.isalnum()).lower()[-12:]
    restore_db = f"macteval_legacy_restore_{safe}"
    prefix = f"restore-{safe}"
    print(f"==> 恢复数据库到 {DST_PG}/{restore_db}")
    psql(DST_PG, "macteval", "postgres", f'DROP DATABASE IF EXISTS "{restore_db}"')
    psql(DST_PG, "macteval", "postgres", f'CREATE DATABASE "{restore_db}"')

    # 从这一行起，任何异常都必须仍然清理隔离目标。第一版没有 try/finally，
    # 实测在 fput 抛错后把 `macteval_legacy_restore_...` 留在了 rig postgres 里
    # ——对"可重复执行的演练"来说这是缺陷，不是意外。
    restored_objects: dict[str, int] = {}
    after: dict[str, int] = {}
    try:
        with dump.open("rb") as handle:
            run(["docker", "exec", "-i", DST_PG, "pg_restore", "-U", "macteval",
                 "-d", restore_db, "--no-owner", "--no-privileges"], stdin=handle)

        after = counts(DST_PG, "macteval", restore_db, tables)

        if objects:
            dst_minio = minio(*DST_MINIO)
            print(f"==> 恢复对象到 {DST_MINIO[0]}（前缀 {prefix}）")
            for bucket, items in objects.items():
                target_bucket = f"{prefix}-{bucket}"
                if not dst_minio.bucket_exists(target_bucket):
                    dst_minio.make_bucket(target_bucket)
                for item in items:
                    local = out_dir / "objects" / bucket / item["key"]
                    dst_minio.fput_object(target_bucket, item["key"], str(local))
                restored_objects[bucket] = len(
                    list(dst_minio.list_objects(target_bucket, recursive=True)))
                print(f"    {target_bucket:<32} {restored_objects[bucket]:>4}")
    finally:
        print("==> 清理隔离目标")
        psql(DST_PG, "macteval", "postgres", f'DROP DATABASE IF EXISTS "{restore_db}"')
        if objects:
            dst_minio = minio(*DST_MINIO)
            for bucket in objects:
                target_bucket = f"{prefix}-{bucket}"
                try:
                    for obj in dst_minio.list_objects(target_bucket, recursive=True):
                        dst_minio.remove_object(target_bucket, obj.object_name)
                    dst_minio.remove_bucket(target_bucket)
                except Exception as exc:  # noqa: BLE001
                    print(f"    清理 {target_bucket} 时: {type(exc).__name__}")

    failures: list[str] = []
    for table in tables:
        if before.get(table) != after.get(table):
            failures.append(f"表 {table}: 源 {before.get(table)} ≠ 恢复 {after.get(table)}")
    for bucket, items in objects.items():
        if restored_objects.get(bucket) != len(items):
            failures.append(f"桶 {bucket}: 源 {len(items)} ≠ 恢复 {restored_objects.get(bucket)}")

    # 额外核对：报告版本行、窗口行、证据帧行——它们是"报告能不能用"的关键。
    critical = [t for t in ("evaluation_reports", "lesson_window_features",
                            "behavior_observations", "analysis_jobs")
                if t in tables]
    critical_check = {t: {"source": before.get(t), "restored": after.get(t)}
                      for t in critical}

    # 源栈必须毫发无伤：再数一次
    verify_untouched = counts(SRC_PG, SRC_PG_USER, SRC_DB, tables)
    if verify_untouched != before:
        failures.append("演练后源库行数与演练前不一致（源被改动！）")

    payload = {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": {"container": SRC_PG, "database": SRC_DB,
                   "minio": SRC_MINIO[0], "flywayVersion": _flyway(SRC_PG, SRC_DB)},
        "target": {"container": DST_PG, "minio": DST_MINIO[0],
                   "database": restore_db, "bucketPrefix": prefix},
        "backup": {"dir": str(out_dir), "dumpBytes": dump.stat().st_size,
                   "dumpSha256": dump_sha},
        "tables": {"count": len(tables), "source": before, "restored": after},
        "objects": {b: {"source": len(v), "restored": restored_objects.get(b)}
                    for b, v in objects.items()},
        "criticalTables": critical_check,
        "sourceUntouchedAfterDrill": verify_untouched == before,
        "failures": failures,
        "passed": not failures,
        "scope": "备份可恢复 + 行数/对象数一致。未验证新 compose 下应用可读这些数据"
                 "（需跑容器，被 Docker 数据盘阻塞）",
    }
    evidence_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    print()
    if failures:
        print(f"{len(failures)} 项不一致：", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        print(f"证据：{evidence_path}")
        return 1
    print(f"演练通过：{sum(before.values())} 行 / "
          f"{sum(len(v) for v in objects.values())} 对象 全部一致；源库未改动")
    print(f"证据：{evidence_path}")
    return 0


def _flyway(container: str, db: str) -> str:
    try:
        out = psql(container, SRC_PG_USER, db,
                   "select max(version::numeric) from flyway_schema_history")
        return out.strip() or "unknown"
    except SystemExit:
        return "unknown"


if __name__ == "__main__":
    sys.exit(main())
