#!/usr/bin/env python
"""临时目录纪律的契约测试：任何脚本都不得把临时文件写到项目盘之外。

## 为什么要单独一个文件守这件事

本项目的硬约束是"所有临时文件只能写入 `E:/Personal_Design`"。系统 TEMP 在
C 盘，而 Git Bash 下的 `/tmp` 就是 `C:/Users/<user>/AppData/Local/Temp`。

这类缺陷的共同特征：**不会报错，只会悄悄占满 C 盘**。实测已发生两次——
E2E 的 Playwright artifacts（5 个目录）、`e2e_acceptance.py` 的 "durable" WAV
（每跑一次留一个完整视频音频，从不删除）。

## 检查方式

用 `ast` 解析 Python 文件，只检查**真实调用**与**真实字符串字面量**，
因此注释和文档字符串里"原实现用了 gettempdir()"这类历史说明不会误报。
Shell 文件用逐行匹配，但跳过以 `#` 开头的行。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SELF = Path(__file__).resolve()

#: 允许例外 —— 空集合：本项目没有任何例外。若将来确有需要，
#: 必须在此显式登记并写明原因。
ALLOWLIST: set[str] = set()


def _iter_source_files():
    roots = [ROOT / "scripts", ROOT / "tools", ROOT / "ai-service" / "app"]
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.suffix not in {".py", ".sh"}:
                continue
            if "__pycache__" in path.parts or ".venv" in str(path):
                continue
            if path.resolve() == SELF:
                continue
            yield path


def _py_offences(path: Path) -> list[str]:
    """在 Python 文件里找：裸 TemporaryDirectory()、gettempdir()、"/tmp/ 字面量"。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return []
    out: list[str] = []
    rel = str(path.relative_to(ROOT)).replace("\\", "/")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = ""
            if isinstance(func, ast.Attribute):
                name = func.attr
                base = getattr(func.value, "id", "")
                if base == "tempfile" and name == "TemporaryDirectory" and not node.args and not node.keywords:
                    out.append(f"{rel}:{node.lineno}: 裸 tempfile.TemporaryDirectory()（缺 dir=）")
                if base == "tempfile" and name == "gettempdir":
                    out.append(f"{rel}:{node.lineno}: tempfile.gettempdir()")
            elif isinstance(func, ast.Name) and name == "":
                if func.id == "gettempdir":
                    out.append(f"{rel}:{node.lineno}: gettempdir()")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # 只匹配**以 /tmp/ 开头**的字面量。用子串匹配会大量误报：
            # "scripts/..." 里就含 "s/" 之后的巧合，实测 10+ 条假阳性。
            value = node.value.strip()
            if value.startswith("/tmp/") or value == "/tmp":
                out.append(f"{rel}:{node.lineno}: 路径字面量指向 /tmp/ -> {value[:60]!r}")
    return out


def _sh_offences(path: Path) -> list[str]:
    out: list[str] = []
    rel = str(path.relative_to(ROOT)).replace("\\", "/")
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for i, line in enumerate(lines, 1):
        if line.lstrip().startswith("#"):
            continue
        if re.search(r"""[="'\s]/tmp/""", line):
            out.append(f"{rel}:{i}: 硬编码 /tmp/ -> {line.strip()[:80]}")
    return out


def test_no_out_of_project_temp_write() -> None:
    """脚本不得出现会落到系统 TEMP 的写法。"""
    offenders: list[str] = []
    for path in _iter_source_files():
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        if rel in ALLOWLIST:
            continue
        if path.suffix == ".py":
            offenders.extend(_py_offences(path))
        else:
            offenders.extend(_sh_offences(path))
    assert not offenders, (
        f"发现 {len(offenders)} 处会写到系统 TEMP（C 盘）的写法：\n  "
        + "\n  ".join(offenders))


def test_e2e_acceptance_scratch_is_in_repo() -> None:
    """tools/e2e_acceptance.py 的临时根必须在仓库内。

    历史：这里曾把抽出的 WAV 复制到 `gettempdir()/mact-e2e-*.wav` 并命名
    "durable"，**从不删除** —— 每跑一次在 C 盘留一个完整音频。
    """
    import importlib.util
    import sys

    script = ROOT / "tools" / "e2e_acceptance.py"
    if not script.exists():
        pytest.skip("e2e_acceptance.py 不存在")
    spec = importlib.util.spec_from_file_location("e2e_acceptance_under_test", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    scratch = Path(module._SCRATCH).resolve()
    assert ROOT.resolve() in scratch.parents or scratch == ROOT.resolve(), (
        f"_SCRATCH={scratch} 不在仓库 {ROOT} 内")


def test_ai_service_media_work_dir_fallback_is_in_repo() -> None:
    """ai-service 的临时根不得回退到系统 TEMP。

    用**源码级断言**而非 import：test 环境不一定装了 pydantic_settings，
    而这条约束与依赖是否装好无关。检查 `media_work_dir()` 的实现：
    回退分支必须是项目内路径。
    """
    paths_py = ROOT / "ai-service" / "app" / "core" / "paths.py"
    if not paths_py.exists():
        pytest.skip("paths.py 不存在")
    src = paths_py.read_text(encoding="utf-8", errors="replace")

    assert "gettempdir" not in src, (
        "paths.py 里仍有 gettempdir()：裸本地 uvicorn 会把媒体拉到 C 盘")

    tree = ast.parse(src)
    func = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "media_work_dir"), None)
    assert func is not None, "找不到 media_work_dir()"
    seg = ast.get_source_segment(src, func) or ""
    assert ".cache" in seg or "Personal_Design" in seg, (
        "media_work_dir() 的回退分支看起来不指向项目内目录：\n" + seg[:400])


# ---------------------------------------------------------------------------
# 磁盘门禁的三个具体回归（2026-10-30 由独立审查 Agent 指出，均为真缺陷）
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def diskgate():
    import importlib.util
    import sys

    script = ROOT / "scripts" / "verify" / "check_disk_policy.py"
    spec = importlib.util.spec_from_file_location("disk_gate_under_test", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_maven_opts_is_not_treated_as_a_path(diskgate) -> None:
    """`MAVEN_OPTS` 是 JVM 参数字符串，不是路径。

    回归：它曾出现在 PATH_ENV_VARS 里，而 `verify_all.sh:71` 每次都设
    `MAVEN_OPTS="-Dmaven.repo.local=..."`。于是门禁在**每一次**门禁运行中都
    判它"不在项目根下"→ FAIL。这不是环境不合规，是判定规则用错了类型。
    """
    assert "MAVEN_OPTS" not in diskgate.PATH_ENV_VARS, (
        "MAVEN_OPTS 是 JVM 参数（-Dmaven.repo.local=...），不是路径；"
        "把它当路径会让门禁每次都红")
    # 但仓库缓存目录本身仍应被检查
    assert "MAVEN_REPO" in diskgate.PATH_ENV_VARS


def test_not_found_docker_disk_is_fail_not_skip(diskgate) -> None:
    """找不到任何 docker_data.vhdx 必须判 fail（AST 检查，不是字符串 grep）。

    回归：先前判 skip。用户把 Docker 迁到任意 E 盘目录后，门禁既看不到
    C 盘那块、也没找到新位置，于是 skip；而 verify_all 把 skip 算覆盖不完整
    （退出 3），表现像"迁移失败"。实际只是脚本没找到。

    为什么用 AST：字符串 grep 抓不到"把 fail 改成 skip 但保留同一句话"这种
    变异——那正是本文件上一版测试的漏洞，变异测试证明它 6 passed 照样绿。
    """
    import ast

    src = (ROOT / "scripts" / "verify" / "check_disk_policy.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "check_docker_root"), None)
    assert fn is not None, "找不到 check_docker_root()"

    # 找 `if not disks:` 分支，检查它构造的 Check 第二个位置参数是不是 "fail"
    verdicts: list[str] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        test_src = ast.get_source_segment(src, node.test) or ""
        if "not disks" not in test_src:
            continue
        # 只看 if 分支本体（node.body），不要走 else ——
        # ast.walk(node) 会连同 else 一起遍历，把别的分支的 Check 也算进来。
        for stmt in node.body:
            for inner in ast.walk(stmt):
                if (isinstance(inner, ast.Call)
                        and getattr(inner.func, "id", "") == "Check"
                        and len(inner.args) >= 2
                        and isinstance(inner.args[1], ast.Constant)):
                    verdicts.append(str(inner.args[1].value))

    assert verdicts, "`if not disks:` 分支里没有构造 Check"
    assert all(v == "fail" for v in verdicts), (
        f"找不到磁盘时判定为 {verdicts}，必须是 fail；"
        "skip 会让 verify_all 以退出码 3 结束并看起来像环境问题")


def test_discovery_does_not_claim_settings_store_is_authoritative(diskgate) -> None:
    """不得再把 settings-store.json 当作磁盘位置的权威来源。

    实测：该文件 19 个键里没有任何 disk-location 键（DataFolder /
    diskImageLocation / ...）。先前版本据此"找"位置，找不到就返回空，
    造成迁移后的假绿。
    """
    src = (ROOT / "scripts" / "verify" / "check_disk_policy.py").read_text(
        encoding="utf-8")
    fn_start = src.index("def _docker_desktop_disks()")
    fn_end = src.index("def check_docker_root()")
    body = src[fn_start:fn_end]
    # 允许在注释/文档字符串里提到它（说明为何不用），但不得真的去读这个文件。
    # 用 AST 取函数体里的真实调用，而不是全文 grep —— 否则"解释为何不用"
    # 这段文字本身就会被判违规。
    import ast as _ast
    fn_node = _ast.parse(src).body
    target = next((n for n in _ast.walk(_ast.Module(body=fn_node, type_ignores=[]))
                   if isinstance(n, _ast.FunctionDef)
                   and n.name == "_docker_desktop_disks"), None)
    assert target is not None
    # 只取 docstring 之外的语句
    stmts = [st for st in target.body
             if not (isinstance(st, _ast.Expr) and isinstance(st.value, _ast.Constant))]
    code = chr(10).join(_ast.get_source_segment(src, st) or "" for st in stmts)
    assert "settings-store.json" not in code, (
        "_docker_desktop_disks() 仍在读 settings-store.json；"
        "该文件不含磁盘位置键，读它会给出假绿")
