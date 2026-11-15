#!/usr/bin/env python
"""`backup_restore.py` 白名单与缺失处理的契约测试（不连数据库、不连 MinIO）。

## 这个文件守的是什么

2026-10-30 修掉了一个**静默的**备份缺陷：`TABLES` 白名单只有 11 张表，
而 V33 schema 有 19 张业务表。漏掉的包括 `behavior_evidence_frames`（1458 行，
任务书 §A3 明确要求核对的对象）、`evaluation_report_versions`（报告版本快照）、
`task_outbox`（决定任务能否重投）。

原来的演练**照样打印 `OK: 81 rows ... verified` 并 exit 0**——因为白名单少的表
根本不出现在比对里。这正是"看起来验过了"的典型形态。

三道防线，缺一不可：

1. **覆盖率**：库里存在的业务表必须都在白名单里，否则失败。
2. **缺表**：白名单有、库里没有 → 默认失败；只有显式设置
   `MACT_BACKUP_ALLOW_MISSING_TABLES=1`（旧 schema 库）才放宽。
3. **超限对象**：只登记清单不搬字节，且必须单独上报，不能算进"已核对"。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "backup_restore.py"


@pytest.fixture(scope="module")
def br():
    spec = importlib.util.spec_from_file_location("backup_restore_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 1) 白名单必须覆盖决定"能否恢复"的表
# ---------------------------------------------------------------------------
CRITICAL = [
    "behavior_evidence_frames",     # 证据帧元数据；任务书 §A3 明确要求核对
    "evaluation_report_versions",   # 报告版本快照
    "task_outbox",                  # 决定任务能否重投
    "class_groups",                 # lesson_sessions 的外键目标
    "students",
    "media_assets",
    "evaluation_reports",
    "lesson_sessions",
    "lesson_window_features",
]


def test_whitelist_covers_critical_tables(br):
    missing = [t for t in CRITICAL if t not in br.TABLES]
    assert not missing, (
        f"备份白名单缺少关键表 {missing}。这些表决定了'恢复后报告能否打开、"
        f"任务能否跑完'；漏掉它们不会让演练报错，只会让它静默地少核对。")


def test_whitelist_has_no_duplicates(br):
    assert len(br.TABLES) == len(set(br.TABLES))


def test_whitelist_excludes_flyway_history(br):
    """flyway_schema_history 由迁移重建，不参与逐行核对。"""
    assert "flyway_schema_history" not in br.TABLES


# ---------------------------------------------------------------------------
# 2) 超限对象的取舍
# ---------------------------------------------------------------------------
def test_max_object_bytes_default_is_256mb(br, monkeypatch):
    monkeypatch.delenv("MACT_BACKUP_MAX_OBJECT_MB", raising=False)
    assert br.max_object_bytes() == 256 * 1024 * 1024


def test_max_object_bytes_zero_means_unlimited(br, monkeypatch):
    monkeypatch.setenv("MACT_BACKUP_MAX_OBJECT_MB", "0")
    assert br.max_object_bytes() == 0


def test_max_object_bytes_tolerates_garbage(br, monkeypatch):
    """环境变量写错不该让演练崩溃，回落到默认值。"""
    monkeypatch.setenv("MACT_BACKUP_MAX_OBJECT_MB", "not-a-number")
    assert br.max_object_bytes() == 256 * 1024 * 1024


# ---------------------------------------------------------------------------
# 3) 临时目录必须在仓库内（E 盘硬约束）
# ---------------------------------------------------------------------------
def test_scratch_dir_is_inside_repo(br):
    """不能落在系统 TEMP。

    本机系统 TEMP 在 C 盘（`C:\\Users\\...\\AppData\\Local\\Temp`），而项目
    硬约束是"所有临时文件只能写入 E:/Personal_Design"。原实现用
    `tempfile.TemporaryDirectory()` 的默认位置，演练要把 8.7 GB 对象复制一遍，
    实测在默认位置会失败（表现为 MinIO 分片上传 AccessDenied）。
    """
    with br.scratch_dir() as tmp:
        resolved = Path(tmp).resolve()
        assert br.REPO_ROOT.resolve() in resolved.parents, (
            f"临时目录 {resolved} 不在仓库 {br.REPO_ROOT} 内")
        assert os.path.exists(tmp)


# ---------------------------------------------------------------------------
# 4) 源码层面：三个提示都必须存在（删掉任一都会让缺口不可见）
# ---------------------------------------------------------------------------
def test_source_reports_uncopied_objects(br):
    src = SCRIPT.read_text(encoding="utf-8")
    assert "仅做清单核对" in src, "超限对象必须单独上报，否则会被读成'全部核对过'"
    assert "MACT_BACKUP_MAX_OBJECT_MB=0" in src, "应提示如何做全量演练"


def test_unmapped_tables_detects_untracked_table(br):
    """库里有、白名单没有的表必须被识别出来。

    这是**行为**测试，不是源码 grep。变异测试的反面教材：早期版本只断言
    "源码里有那句打印"，于是把 drill() 里的 `return 1` 删掉、只留打印，
    仍然 10 passed —— 覆盖率缺口会静默通过，正是原始缺陷换了副面孔。
    """
    got = br.unmapped_tables({"users", "courses", "behavior_evidence_frames"},
                             whitelist=["users", "courses"])
    assert got == ["behavior_evidence_frames"], got


def test_unmapped_tables_empty_when_fully_covered(br):
    assert br.unmapped_tables({"users", "courses"},
                              whitelist=["users", "courses"]) == []


def test_unmapped_tables_ignores_flyway_history(br):
    """flyway_schema_history 由迁移重建，不算未覆盖。"""
    assert br.unmapped_tables({"users", "flyway_schema_history"},
                              whitelist=["users"]) == []


def test_unmapped_tables_defaults_to_module_whitelist(br):
    """不传 whitelist 时用模块级 TABLES（drill() 的实际用法）。"""
    got = br.unmapped_tables({"users", "definitely_not_a_real_table"})
    assert "definitely_not_a_real_table" in got


def test_source_has_relaxation_switch(br):
    src = SCRIPT.read_text(encoding="utf-8")
    assert "MACT_BACKUP_ALLOW_MISSING_TABLES" in src, (
        "旧 schema 库确实会缺表；必须有一个**显式**放宽开关，"
        "而不是默认静默通过。")


def test_drill_exits_nonzero_when_whitelist_incomplete(br) -> None:
    """drill() 在发现未覆盖表时必须返回非零。

    **这是弱守，必须说清楚**：真正的端到端验证需要连数据库跑 drill()，
    而那依赖 Docker（本机数据根在 C 盘时被禁止）。这里退而求其次，
    用 AST 静态确认 `if unmapped:` 块内**存在** `return 1`。

    为什么不能只断言"源码里有那句打印"：变异测试证明，删掉 `return 1`
    而保留打印，旧测试仍然 10 passed —— 覆盖率缺口会静默通过，
    这正是原始缺陷（behavior_evidence_frames 未被核对）换了一副面孔。
    """
    import ast

    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    drill = next((n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "drill"), None)
    assert drill is not None, "找不到 drill()"

    found = False
    for node in ast.walk(drill):
        if not isinstance(node, ast.If):
            continue
        # 条件是调用 unmapped_tables(...) 的那个 if
        seg = ast.get_source_segment(SCRIPT.read_text(encoding="utf-8"), node.test) or ""
        if "unmapped_tables" not in seg and "unmapped" not in seg:
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Return) and isinstance(inner.value, ast.Constant):
                if inner.value.value not in (0, None):
                    found = True
    assert found, (
        "drill() 里 `if unmapped:` 分支没有非零 return —— "
        "白名单漏表时演练会报 OK 并通过，这正是要被防住的原始缺陷")
