#!/usr/bin/env python
"""`verify_nginx_browser_csp.sh` 的契约测试（不启动 nginx、不开浏览器）。

## 为什么需要这个文件

那个脚本对着**真实** nginx + 构建产物 + 真实后端跑，是最慢的一步（~25 s），
所以它不在 `verify_all.sh` 的 fast 档里。问题随之而来：**没有任何机制阻止
有人把其中一条检查删掉**——脚本照样 exit 0，只是少验了一件事。

2026-10-29 真实发生过一次险情：任务书 §B1.4 要求浏览器验证五类场景
（Dashboard / ECharts / 证据图片 / **MinIO 上传** / WebSocket），而"MinIO 上传"
这一类**从来没被验过**。它和证据图片是不同 CSP 指令：

  * 证据图片是 `img-src`（GET）
  * 上传是浏览器直接 PUT 到 MinIO，受 `connect-src` 约束

`img-src` 通过完全不代表 `connect-src` 通过。因此补了一条快照检查。

本文件把这件事固定下来：断言脚本**仍然包含**那条上传探针、并且断言它
真的在检查"请求到达了 MinIO"（拿到 HTTP 状态码），而不是只看 fetch 没抛异常。
被 CSP 拦下时 fetch 会抛 `TypeError: Failed to fetch`，`status` 为 0——
所以"`status > 0`"才是正确的判据，只判 `ok` 是不够的（后者会把
script `ok: true` 但 status 0 的情形当通过）。

这些断言都很脆（字符串匹配），但它们的失败方式恰好是我们想要的：
有人删掉或改弱那条检查 → 本测试红 → 提醒他"你在减少覆盖"。
"""
from __future__ import annotations

from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "verify_nginx_browser_csp.sh"


@pytest.fixture(scope="module")
def source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_script_exists(source: str) -> None:
    assert source.strip(), "verify_nginx_browser_csp.sh 是空的"


def test_has_minio_upload_probe(source: str) -> None:
    """任务书 §B1.4 的五类场景之一：浏览器直传 MinIO 必须被验。"""
    assert "浏览器可向 MinIO 发起 PUT" in source, (
        "缺少 MinIO 直传检查。任务是 §B1.4 明确要求的五类场景之一；"
        "它测的是 connect-src，而证据图片测的是 img-src，两者不能互相替代。")


def test_upload_probe_asserts_status_not_just_ok(source: str) -> None:
    """被 CSP 拦时 fetch 抛错 → status=0，所以判据必须包含 status>0。"""
    assert 'upload_result.get("status", 0) > 0' in source, (
        "上传检查的判据必须是 status>0。只判 ok 会把被 CSP 拦截的请求"
        "（TypeError: Failed to fetch）当成通过。")


def test_minio_origin_threaded_into_browser_python(source: str) -> None:
    """CSP 注入值与浏览器探针用的 origin 必须来自同一个变量。"""
    assert '"$MINIO_ORIGIN" <<\'PYEOF\'' in source, (
        "MINIO_ORIGIN 没有传给内嵌的浏览器 Python 块；探针会拿到空 origin。")
    assert "port, out_path, user, password, work, minio = sys.argv[1:7]" in source, (
        "内嵌 Python 块没有解包 minio 参数——新增探针会 NameError。")


def test_checks_cover_all_five_b1_4_scenarios(source: str) -> None:
    """§B1.4 的五类场景，逐条对应脚本里的一条 record()。"""
    required = {
        "Dashboard": "Dashboard 图表渲染",
        "ECharts 运行时": "unsafe-eval",          # Dashboard 那条的注记
        "证据图片": "证据帧图片真实解码",
        "MinIO 上传": "浏览器可向 MinIO 发起 PUT",
        "WebSocket": "/ws/ 无票据被拒",
    }
    missing = [name for name, needle in required.items() if needle not in source]
    assert not missing, f"§B1.4 场景缺少检查：{missing}"


def test_records_zero_csp_violations_globally(source: str) -> None:
    """全局零违规断言必须在，否则单点检查全绿但整体仍有拦截。"""
    assert "全程零 CSP 违规" in source


def test_uses_cleanup_trap(source: str) -> None:
    """nginx 必须被清理；否则脚本退出后端口 3000 被占用，下一次必失败。"""
    assert "trap cleanup EXIT" in source


# ---------------------------------------------------------------------------
# 临时目录不得落到系统 TEMP（最高级磁盘约束）
# ---------------------------------------------------------------------------
def test_e2e_forces_tmpdir_into_repo() -> None:
    """E2E 必须在导入期把 TMPDIR/TEMP/TMP 指回项目内。

    为什么单独守这一条：Playwright/Chromium 启动时按环境变量决定临时目录，
    不设就在系统 TEMP（C 盘）留下 playwright-artifacts-*。实测本机曾累积
    5 个这样的目录——违反"所有临时文件只能写入 E:/Personal_Design"。
    """
    import importlib.util
    import os
    import sys

    script = Path(__file__).resolve().parents[1] / "browser_evidence_e2e.py"
    saved = {v: os.environ.get(v) for v in ("TMPDIR", "TEMP", "TMP")}
    try:
        for v in ("TMPDIR", "TEMP", "TMP"):
            os.environ.pop(v, None)
        spec = importlib.util.spec_from_file_location("e2e_tmpdir_under_test", script)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        root = str(module.REPO_ROOT)
        for var in ("TMPDIR", "TEMP", "TMP"):
            value = os.environ.get(var) or ""
            assert value.startswith(root), (
                f"{var}={value!r} 不在仓库 {root} 内；"
                f"Playwright 会把临时文件写到系统 TEMP（C 盘）")
    finally:
        for v, old in saved.items():
            if old is None:
                os.environ.pop(v, None)
            else:
                os.environ[v] = old
