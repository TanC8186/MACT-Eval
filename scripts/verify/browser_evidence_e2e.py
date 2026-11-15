#!/usr/bin/env python
"""课堂报告证据区浏览器验收（U01–U06）。

真实链路：真实 /auth/login 换 token → 写入 localStorage 与前端同一套键名
（`token` / `refreshToken` / `authUser`，见
`frontend/apps/teacher-web/src/api/authSession.ts`）→ 打开真实报告抽屉 →
真实 `/api/reports/{id}/behavior-evidence` → 真实 MinIO 私有签名图片。

两类运行（互斥，必须显式选择）：

    --mode real     主路径。真实报告 API + 真实私有图片，用于最终截图。
    --mode fixture  仅 U01 的 DOM 压力测试：用 page.route 拦截
                    behavior-evidence 接口，返回明确标注的合成 329 条数据。
                    报告 JSON 与文件名都会带 `fixture`，不得冒充真实推理闭环。

用法（需已启动真实环境；脚本不启动任何容器/服务）：

    python scripts/verify/browser_evidence_e2e.py --self-check
    python scripts/verify/browser_evidence_e2e.py --offline-smoke
    MACT_E2E_TEACHER_USER=... MACT_E2E_TEACHER_PASS=... \\
        python scripts/verify/browser_evidence_e2e.py --mode real --case all
    python scripts/verify/browser_evidence_e2e.py --mode fixture --case u01 --fixture-cards 329

退出码：
    0  选中的用例全部执行且通过
    1  至少一个用例失败或执行异常（含服务端鉴权不通过）
    2  覆盖不完整：有任一用例被跳过（缺选择器 / 缺真实数据 / 缺账号，并非通过）
    3  环境前置失败（连不上 base-url、登录失败、浏览器无法启动）

用例契约与逐项状态见 `scripts/verify/browser_cases.md`。

实现说明：U04 的「延迟 A 响应后切 B」需要路由处理器真正挂起而不阻塞页面操作，
同步 API 的 route handler 会在主线程阻塞，因此该用例单独用 asyncio + async
Playwright 在独立线程里跑一次，其余用例仍走同步 API。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 把临时目录强制指回项目内（最高级磁盘约束）。
#:
#: 为什么必须在模块导入期就设置：Playwright / Chromium 启动时读取环境变量决定
#: 临时目录，之后不再看。实测未设置时，每次运行都会在系统 TEMP
#: （`C:/Users/<user>/AppData/Local/Temp`）留下 `playwright-artifacts-*` 与
#: `playwright_chromiumdev_profile-*` 目录。项目硬约束是"所有临时文件只能
#: 写入 E:/Personal_Design"。
#:
#: 无条件覆盖：即使调用方导出的是系统 TEMP 也改回项目内。这条约束是硬性要求，
#: 不该依赖调用方记得设置。verify_all.sh 本来就导出到同一位置，两者不冲突。
_SCRATCH = REPO_ROOT / ".cache" / "tmp"
try:
    _SCRATCH.mkdir(parents=True, exist_ok=True)
    for _var in ("TMPDIR", "TEMP", "TMP"):
        os.environ[_var] = str(_SCRATCH)
except OSError:  # 只读文件系统等极端情况：不因这条约束让验收无法运行
    pass
DEFAULT_OUT_DIR = REPO_ROOT / "docs" / "reviews" / "evidence"
# 截图/证据前缀必须跟随运行日期，否则新一次验收会静默覆盖旧证据
# （2026-10-27 实测：上一轮运行把 2026-10-26e-* 截图直接覆盖，历史证据丢失）。
# 用 env 覆盖可复现历史命名；默认按当天日期生成。
# 截图前缀**必须含时分秒**。原因（2026-10-30 实测）：前缀只到日级时，同一天
# 第二次运行会静默覆盖已提交的封版截图。E-e2e 复核那次就实际发生了——
# 5 张已入库截图被覆盖，靠手工 `git checkout` 才恢复。
# 覆盖不报错，只会让"封版资产"变成"最后一次运行的样子"。
SCREENSHOT_PREFIX = os.environ.get("MACT_E2E_PREFIX") or (
    datetime.now().strftime("%Y-%m-%dT%H%M%S") + "-single-web-")

# 已废弃的历史前缀：只用于 self-check 报告，不再作为默认值。
LEGACY_SCREENSHOT_PREFIX = "2026-10-26e-"

# ---------------------------------------------------------------------------
# 依赖（自我检查需要区分「没装」与「装了但浏览器缺失」）
# ---------------------------------------------------------------------------
try:
    import requests  # type: ignore
except Exception:  # pragma: no cover - 环境缺依赖时由 --self-check 报告
    requests = None  # type: ignore

try:
    from playwright.sync_api import sync_playwright  # type: ignore
    from playwright.sync_api import Error as PlaywrightError  # type: ignore
except Exception:  # pragma: no cover
    sync_playwright = None  # type: ignore

    class PlaywrightError(Exception):  # type: ignore
        """占位：未安装 playwright 时避免导入期崩溃。"""


try:
    from playwright.async_api import async_playwright  # type: ignore
except Exception:  # pragma: no cover
    async_playwright = None  # type: ignore

# ---------------------------------------------------------------------------
# 前端契约（键名来自 src/api/authSession.ts，不得凭猜测改动）
# ---------------------------------------------------------------------------
TOKEN_KEY = "token"
REFRESH_TOKEN_KEY = "refreshToken"
USER_STORAGE_KEY = "authUser"

# 选择器契约：以组件现命名为准（A15 对齐，映射表见 docs/reviews/2026-10-26e-testid-alignment.md）。
# 任何一项缺失都只会让对应用例 skip，并在 JSON 里写出 missing 列表，绝不伪造通过。
SEL: dict[str, str] = {
    "gallery": '[data-testid="behavior-evidence-gallery"]',
    "card": '[data-testid="evidence-card"]',
    "card_media": '[data-testid="evidence-card-media"]',
    "frame_time": '[data-testid="evidence-frame-time"]',
    "detection_box": '[data-testid="evidence-detection-box"]',
    "filter": '[data-testid="evidence-filter"]',
    # 全局总量：筛选栏固定存在的「全部 N」按钮（data-code="" 即全部项），文本形如「全部 329」。
    "total": '[data-testid="evidence-filter"][data-code=""]',
    "pagination": '[data-testid="evidence-pager"]',
    # 页码状态文本形如「第 1 / 14 页，共 329 张」，前两个数字即当前页与总页数。
    "page_indicator": '[data-testid="evidence-page-status"]',
    "prev": '[data-testid="evidence-prev"]',
    "next": '[data-testid="evidence-next"]',
    "refresh": '[data-testid="evidence-refresh"]',
    "error": '[data-testid="evidence-error"]',
    "retry": '[data-testid="evidence-retry"]',
    # 预览容器是 a-modal 内的 div。P1-2（2026-10-28）起组件提供了显式关闭按钮，
    # 因此不再需要"退到 .ant-modal-close 或 Esc"这种隐式路径。
    "preview": '[data-testid="evidence-preview"]',
    "preview_image": '[data-testid="evidence-preview-image"]',
    "preview_target": '[data-testid="evidence-target"]',
    "preview_close": '[data-testid="evidence-preview-close"]',
    "zoom_in": '[data-testid="evidence-preview-zoom-in"]',
    "zoom_out": '[data-testid="evidence-preview-zoom-out"]',
    "zoom_reset": '[data-testid="evidence-preview-zoom-reset"]',
    "zoom_level": '[data-testid="evidence-preview-zoom-level"]',
    "preview_nav_prev": '[data-testid="evidence-preview-nav-prev"]',
    "preview_nav_next": '[data-testid="evidence-preview-nav-next"]',
    # 原视频回跳：后端没有受控媒体播放地址，组件按设计渲染成 disabled。
    # 断言它"存在且禁用"，防止有人把死按钮做成看起来可点的样子。
    "jump_to_video": '[data-testid="evidence-jump-to-video"]',
    # R11：报告"已选任务但窗口未生成"的状态提示与状态标签。
    "window_pending": '[data-testid="report-window-pending"]',
    "window_state": '[data-testid="report-window-state"]',
    # P1-1（2026-10-28）：报告历史版本选择器。
    "version_select": '[data-testid="report-version-select"]',
    "version_option": '[data-testid="report-version-option"]',
    "version_current": '[data-testid="report-version-current"]',
    "version_loading": '[data-testid="report-version-loading"]',
    "version_retry": '[data-testid="report-version-retry"]',
    # 版本隔离的判据：抽屉当前渲染的 detail.version / dataSource。
    # 只绑 detail，不回落 selectedVersion——回落值会让"切换失败"也看起来正确。
    "detail_version": '[data-testid="report-detail-version"]',
    "detail_source": '[data-testid="report-detail-source"]',
}

# 组件确实未实现的能力。脚本对这些子项只记录 skippedSubChecks，不写假断言，也不计 pass/fail。
#
# 2026-10-28 更新：P1-2 已实现预览关闭按钮与缩放控件，因此这里**清空**。
# 保留这个映射（而不是删掉它）是因为它是"未实现能力"的唯一登记处：
# self-check 会断言它包含的每一条都真实出现在 optionalSkips 里，反之亦然——
# 空集合意味着"本轮不再有登记在案的未实现预览能力"，而不是"检查被跳过了"。
UNIMPLEMENTED: dict[str, str] = {}

# Reports.vue 自 P1-1 起提供了稳定的 data-testid 选择器（此前为空，靠可访问名定位）。
# 保留 None 类型标注是为了让"选择器未落地"仍然可表达；当前全部已落地。
REPORT_SEL: dict[str, str | None] = {
    "report_detail": ".ant-drawer-content",
    "report_detail_close": ".ant-drawer-close",
    "report_version": '[data-testid="report-version-select"]',
}
DRAWER = ".ant-drawer-content"
DRAWER_TITLE = ".ant-drawer-title"
DRAWER_CLOSE = ".ant-drawer-close"

CASES = ("u01", "u02", "u03", "u04", "u05", "u06", "u07", "u08")
CASE_TITLES = {
    "u01": "分页与动作筛选（当前页 DOM 数受限、总数/页数/末页一致、筛选回第 1 页）",
    "u02": "HH:MM:SS 时间、预览缩放、同帧目标切换（框坐标/文本/置信度同步）",
    "u03": "过期链接 / 对象不可用：保留筛选与页码、错误明确、无无限自动重试",
    "u04": "延迟旧响应后切换报告与版本：不串图、不回退最新任务",
    "u05": "教师 A/B、学生、匿名、无签名对象的服务端鉴权",
    "u06": "390/768/1280 视口与键盘操作（导航、分页、预览关闭、焦点恢复）",
    "u07": "数据不足/窗口未完成状态（windows_pending 不得显示旧窗口分数）",
    "u08": "对象被真实删除：错误提示明确、可人工重试、无无限自动请求",
}

# 1x1 透明 PNG，用作 fixture 图片，避免任何外部请求。
FIXTURE_IMG = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


# ---------------------------------------------------------------------------
# 结果模型
# ---------------------------------------------------------------------------
@dataclass
class CaseResult:
    """单个用例的结果。

    状态语义（2026-10-27 复核 R10）：

    ``passed``      本用例的全部必检子项都真的执行并通过；
    ``failed``      有断言失败（含服务端鉴权不通过）；
    ``incomplete``  **新增**：没有失败断言，但有必检子项没执行（缺数据/缺账号/缺能力）。
                    这类结果必须与「可选子项跳过」区分，并让退出码非零，否则
                    「顶层没失败」会把未验证的必检目标伪装成通过。
    ``skipped``     用例整体未执行（缺前置条件，例如打不开报告抽屉）；
    ``error``       执行异常。

    未决哨兵 ``running``（2026-10-27 修复）：``status`` 的初始值<strong>不能</strong>是
    ``skipped``。``finalize()`` 在 ``status == "skipped"`` 时直接返回，于是任何一个
    忘了自己写 ``res.status = ...`` 的用例都会被静默记为 skipped——即使它的断言
    全部通过。u05 曾因此在本机真实运行时「7 条断言全 pass 却报 skipped、退出码 2」。
    现在初始值是 ``running``，``finalize()`` 只要没有失败、没有必检缺失、也没有被
    显式标成 skipped/error，就必须收敛成 passed；忘记赋值会暴露成 passed 而不是消失。
    """

    case: str
    mode: str
    viewport: str = ""
    status: str = "running"  # running(未决) | passed | failed | incomplete | skipped | error
    reason: str = ""
    # 用例执行主体是否真的被调用过。由分发处（run_cases）在调用 case_* 之前置 True。
    # 只靠 status 判断不够：u01/u02/u03/u06 在函数末尾会自己写
    # ``res.status = ... else "passed"``，如果函数体被删空、只剩分发处的 skipped
    # 分支被人一并改掉，status 会变成 "passed" 而零断言——这是本轮要堵的洞。
    executed: bool = False
    assertions: list[dict[str, Any]] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    # 必检子项：本轮验收目标，未执行时用例整体 incomplete 并让退出码非零。
    required_skips: list[dict[str, str]] = field(default_factory=list)
    # 可选子项：组件确实未实现或本轮范围外的能力。不计 pass/fail，也不影响退出码，
    # 但与 requiredSkips 在 JSON 结构上分开，避免两类跳过混为一谈。
    optional_skips: list[dict[str, str]] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.assertions.append({"name": name, "status": status, "detail": detail})

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.add(name, "pass" if ok else "fail", detail)
        return ok

    def skip(self, name: str, detail: str) -> None:
        """断言级跳过：该条断言未执行，但不单独决定用例状态（必检项须用 require()）。"""
        self.add(name, "skip", detail)

    def require(self, name: str, detail: str) -> None:
        """必检子项未执行：记入 requiredSkips，用例最终标 incomplete，退出码非零。"""
        self.required_skips.append({"name": name, "reason": detail})
        self.add(name, "skip", f"必检子项未执行：{detail}")

    def unimplemented(self, name: str, reason: str) -> None:
        """记录「能力未实现」：写进 optionalSkips，不计 pass/fail，也不让用例整体 skip。"""
        self.optional_skips.append({"name": name, "reason": reason})

    def finalize(self) -> None:
        """按「失败 → 异常 → 必检缺失 → 未执行 → 通过」的顺序收敛用例状态。

        ``passed`` 有两条硬前提，缺一不可（2026-10-28 修订）：

        1. ``executed`` 为真——用例执行主体被真正调用过。分发处调用 ``case_*``
           之前置位；删空用例函数体、或把分发分支整段跳过时，这里没有证据。
        2. ``assertions`` 非空且至少有一条 ``pass``——零断言的用例不可能"通过"。

        修订前 ``finalize()`` 只要没有失败断言、没有必检缺失就无条件收敛成
        ``passed``，于是「用例函数被删空」与「分发分支写错导致函数没被调用」
        这两种空执行都会产出绿色的 passed。``status`` 的 ``running`` 哨兵只解决
        了 u05 那种"忘了写 status"的情况，没解决"根本没有执行"的情况。
        """
        # 失败断言永远优先，且必须在幂等守卫之前判断：二次 finalize 时若新增了
        # 失败断言，不能因为"已经是终态"而被忽略。
        if any(a["status"] == "fail" for a in self.assertions):
            self.status = "failed"
            self.reason = self.reason or "存在失败断言"
            return
        if self.status in ("failed", "error"):
            return
        # 幂等只针对 incomplete：已收敛的 incomplete 不再被二次 finalize 升级成 passed
        # （分发处与用例内部都会调用 finalize()）。
        # **不对 passed 加锁**：passed 之后再来的 require()/fail 必须能把它降级，
        # 否则会出现「summary.passed=1 同时 coverage=incomplete」这种自相矛盾。
        if self.status == "incomplete":
            return
        if self.required_skips:
            self.status = "incomplete"
            names = "、".join(item["name"] for item in self.required_skips)
            self.reason = f"必检子项未执行（覆盖不完整，不得记为通过）：{names}"
            return
        if self.status == "skipped":
            # 只有用例自己显式标过 skipped 才保留（缺前置条件）。
            # 未决哨兵 running 会继续往下收敛，绝不静默变成 skipped。
            return
        # ---- 空执行守卫：没有证据的执行不能算通过 ----
        if not self.executed:
            self.status = "incomplete"
            self.reason = "用例执行主体未被调用（无 executed 标记，空执行不得记为通过）"
            return
        if not any(a["status"] == "pass" for a in self.assertions):
            self.status = "incomplete"
            self.reason = (
                "用例没有产生任何通过断言（只有可选跳过或零断言），"
                "空执行不得记为通过"
            )
            return
        self.status = "passed"
        self.reason = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "case": self.case,
            "mode": self.mode,
            "viewport": self.viewport,
            "title": CASE_TITLES.get(self.case, ""),
            "passed": self.status == "passed",
            "skipped": self.status == "skipped",
            "incomplete": self.status == "incomplete",
            "status": self.status,
            "reason": self.reason,
            "missing": self.missing,
            "assertions": self.assertions,
            # 必检与可选子项在结构上分开：requiredSkips 会影响状态与退出码。
            "requiredSkips": self.required_skips,
            "optionalSkips": self.optional_skips,
            # 兼容旧字段名（等于 optionalSkips），避免历史读取方直接 KeyError。
            "skippedSubChecks": self.optional_skips,
            "screenshots": self.screenshots,
        }


def _now() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S")


def _ints(text: str | None) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", text or "")]


def _ceil_div(a: int, b: int) -> int:
    return (a + b - 1) // b


def _safe_detections(frame: dict) -> list[dict]:
    """镜像组件 `safeDetections()` 的校验：bbox 在 [0,1] 内且非退化，confidence 在 [0,1]。"""
    detections = (frame.get("metadata") or {}).get("detections")
    if not isinstance(detections, list):
        return []
    good = []
    for d in detections:
        if not isinstance(d, dict):
            continue
        bbox = d.get("bbox")
        conf = d.get("confidence")
        if (not isinstance(d.get("subjectId"), str) or not isinstance(d.get("label"), str)
                or not isinstance(bbox, list) or len(bbox) != 4
                or not all(isinstance(n, (int, float)) and 0 <= n <= 1 for n in bbox)
                or bbox[2] <= 0 or bbox[3] <= 0
                or bbox[0] + bbox[2] > 1.001 or bbox[1] + bbox[3] > 1.001
                or not isinstance(conf, (int, float)) or not 0 <= conf <= 1):
            continue
        good.append(d)
    return good


def primary_detection(frame: dict) -> dict | None:
    """镜像组件 `primary()`：primarySubjectId → 与帧 code 一致 → 置信度最高（并列取先出现者）。"""
    detections = _safe_detections(frame)
    if not detections:
        return None
    primary_id = (frame.get("metadata") or {}).get("primarySubjectId")
    for d in detections:
        if primary_id and d.get("subjectId") == primary_id:
            return d
    for d in detections:
        if d.get("code") == frame.get("code"):
            return d
    best = detections[0]
    for d in detections[1:]:
        if d["confidence"] > best["confidence"]:
            best = d
    return best


# ---------------------------------------------------------------------------
# API 客户端（U05 鉴权断言与数据核对都走这里，不经过 UI）
# ---------------------------------------------------------------------------
class Api:
    def __init__(self, base: str, timeout: float = 20.0):
        if requests is None:
            raise RuntimeError("缺少 requests，无法执行 API 断言")
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def call(self, method: str, path: str, token: str | None = None,
             body: dict | None = None) -> tuple[int, Any]:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        resp = self.session.request(
            method, self.base + path, headers=headers,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            timeout=self.timeout,
        )
        try:
            payload = resp.json() if resp.content else {}
        except ValueError:
            payload = {"raw": resp.text[:400]}
        return resp.status_code, payload

    def login(self, username: str, password: str) -> dict:
        status, body = self.call("POST", "/auth/login", body={
            "username": username, "password": password,
        })
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"登录失败 {username}: HTTP {status} {str(body)[:200]}")
        data = body.get("data") or {}
        if not data.get("token"):
            raise RuntimeError(f"登录响应缺少 token: {str(body)[:200]}")
        return data

    def register_teacher(self, username: str, password: str, name: str) -> dict:
        status, body = self.call("POST", "/auth/register", body={
            "username": username, "password": password, "name": name, "role": "teacher",
        })
        if status != 200 or not isinstance(body, dict) or not (body.get("data") or {}).get("token"):
            raise RuntimeError(f"注册教师失败 {username}: HTTP {status} {str(body)[:200]}")
        return body["data"]

    def list_reports(self, token: str, size: int = 50) -> list[dict]:
        status, body = self.call("GET", f"/reports/page?page=0&size={size}", token=token)
        if status != 200:
            return []
        return ((body.get("data") or {}).get("items")) or []

    def behavior_evidence(self, token: str | None, report_id: str,
                          version: int | None = None) -> tuple[int, Any]:
        path = f"/reports/{report_id}/behavior-evidence"
        if version is not None:
            path += f"?version={version}"
        return self.call("GET", path, token=token)

    def report_versions(self, token: str, report_id: str) -> list[dict]:
        status, body = self.call("GET", f"/reports/{report_id}/versions", token=token)
        if status != 200:
            return []
        data = body.get("data")
        return data if isinstance(data, list) else []

    def evidence_frame_url(self, token: str, report_id: str,
                           frame_id: str) -> str:
        status, body = self.call(
            "GET", f"/reports/{report_id}/behavior-evidence", token=token)
        if status != 200 or not isinstance(body, dict):
            return ""
        for frame in (body.get("data") or []):
            if frame.get("id") == frame_id:
                return str(frame.get("url") or "")
        return ""

    def report_version_detail(self, token: str, report_id: str, version: int) -> dict:
        """某个已发布版本的完整快照（GET /reports/{id}/versions/{v}）。"""
        status, body = self.call("GET", f"/reports/{report_id}/versions/{version}", token=token)
        if status != 200 or not isinstance(body, dict):
            return {}
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    def report_detail(self, token: str, report_id: str) -> dict:
        """报告详情（用于判定 windowState 等机器可读状态）。"""
        status, body = self.call("GET", f"/reports/{report_id}", token=token)
        if status != 200 or not isinstance(body, dict):
            return {}
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    def report_title(self, token: str, report_id: str) -> str:
        status, body = self.call("GET", f"/reports/{report_id}", token=token)
        if status != 200 or not isinstance(body, dict):
            return ""
        return str((body.get("data") or {}).get("title") or "")


# ---------------------------------------------------------------------------
# 破坏性操作的隔离守卫（U08 需要真的删除一个对象）
# ---------------------------------------------------------------------------
#: 允许执行破坏性操作的 bucket 白名单。证据帧只可能落在这里；
#: 任何其它 bucket（尤其是用户自己的数据）一律拒绝。
#: 证据帧实际落在 analysis-artifacts（见 behavior_evidence_frames.bucket）。
#: 白名单只放这一个 bucket，其它 bucket 一律拒绝删除。
RIG_EVIDENCE_BUCKETS = ("analysis-artifacts",)
#: 允许删除对象的 host 白名单：默认只有隔离 rig 的 MinIO 端口。
#:
#: 为什么默认这么窄：U08 会对证据帧执行**真实删除**。如果白名单放宽到 9000，
#: 在本机上就会打到用户自己的 MinIO 容器，把真实课堂影像删掉。
#:
#: 但 nightly 的 MinIO 在 9000（services 里起的临时实例，job 结束即销毁）。
#: 把它写死成"U08 在 CI 一定 incomplete"也不是办法——那是用一个必检缺失
#: 换一个安全边界。折中：只有当调用方**显式**通过 MACT_E2E_MINIO_PORTS
#: 追加端口时才放行，且仍然强制 host 必须是回环地址。显式 opt-in 让"删到了
#: 不该删的地方"变成一次有记录的选择，而不是默认行为。
RIG_MINIO_PORTS = (19100,)


def _allowed_minio_ports() -> tuple[int, ...]:
    extra = os.environ.get("MACT_E2E_MINIO_PORTS", "").strip()
    if not extra:
        return RIG_MINIO_PORTS
    ports = []
    for piece in extra.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            ports.append(int(piece))
        except ValueError:
            raise SystemExit(
                f"MACT_E2E_MINIO_PORTS 含非整数端口：{piece!r}")
    return tuple(dict.fromkeys(RIG_MINIO_PORTS + tuple(ports)))


def _minio_client_for(url: str):
    """按签名 URL 的 host:port 造一个 MinIO 客户端；非白名单端口返回 (None, 原因)。"""
    from minio import Minio

    parsed = urlsplit(url)
    host = parsed.hostname or ""
    allowed = _allowed_minio_ports()
    if not (host in ("127.0.0.1", "localhost") and parsed.port in allowed):
        return None, (f"拒绝在非隔离 MinIO 上执行删除：{host}:{parsed.port} "
                      f"不在白名单 {allowed}")
    user = os.environ.get("MACT_E2E_MINIO_USER", "uirig")
    secret = os.environ.get("MACT_E2E_MINIO_SECRET", "uirig_secret")
    return Minio(f"{host}:{parsed.port}", access_key=user, secret_key=secret,
                 secure=False), ""


def make_restore_object(evidence_url: str):
    """返回 (bucket, key, delete=bool) -> {ok, error}；删除前把对象字节读进内存。"""
    stash: dict[str, bytes] = {}

    def restore(bucket: str, key: str, delete: bool) -> dict:
        if bucket not in RIG_EVIDENCE_BUCKETS:
            return {"ok": False, "error": f"bucket {bucket!r} 不在证据帧白名单 {RIG_EVIDENCE_BUCKETS}"}
        client, why = _minio_client_for(evidence_url)
        if client is None:
            return {"ok": False, "error": why}
        try:
            if delete:
                response = client.get_object(bucket, key)
                try:
                    stash[key] = response.read()
                finally:
                    response.close()
                    response.release_conn()
                client.remove_object(bucket, key)
                return {"ok": True, "bytes": len(stash[key])}
            body = stash.get(key)
            if body is None:
                return {"ok": False, "error": "没有采集到对象原始字节，无法恢复"}
            import io
            client.put_object(bucket, key, io.BytesIO(body), length=len(body))
            return {"ok": True, "bytes": len(body)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return restore


# ---------------------------------------------------------------------------
# 夹具（fixture）：合成证据帧，明确标注非真实推理
# ---------------------------------------------------------------------------
FIXTURE_ACTIONS = [
    ("raising_hand", "举手"),
    ("reading", "阅读"),
    ("writing", "书写"),
    ("discussing", "讨论"),
    ("bow_head", "低头"),
    ("turn_head", "转头"),
    ("overview", "课堂全景"),
]


def build_fixture_frames(count: int, report_id: str = "fixture") -> list[dict]:
    frames: list[dict] = []
    for i in range(count):
        code, label = FIXTURE_ACTIONS[i % len(FIXTURE_ACTIONS)]
        subject = f"target-{i % 5 + 1}"
        confidence = round(0.50 + ((i % 40) / 100.0), 3)
        bbox = [round(0.06 + (i % 5) * 0.11, 3), 0.32, 0.12, 0.20]
        detections = [] if code == "overview" else [{
            "subjectId": subject, "code": code, "label": label,
            "className": code, "confidence": confidence, "bbox": bbox,
        }]
        frames.append({
            "id": f"fixture-{report_id}-{i:04d}",
            "windowIndex": i // 6,
            "timestampMs": i * 10_000,
            "code": code,
            "label": label,
            "url": FIXTURE_IMG,
            "metadata": {
                "detectionSource": "overview" if code == "overview" else "behavior_model",
                "modelVersion": "FIXTURE-SYNTHETIC-NOT-REAL-INFERENCE",
                "imageWidth": 1280,
                "imageHeight": 720,
                "primarySubjectId": subject if detections else None,
                "detections": detections,
                "supportedActions": ["举手", "阅读", "书写", "讨论", "低头", "转头"],
                "fixture": True,
            },
        })
    return frames


# ---------------------------------------------------------------------------
# 配置与日志
# ---------------------------------------------------------------------------
@dataclass
class Config:
    base_url: str
    api_url: str
    mode: str
    teacher_user: str
    teacher_pass: str
    student_user: str
    student_pass: str
    other_user: str
    other_pass: str
    allow_register_other_teacher: bool
    report_id: str
    cases: list[str]
    fixture_cards: int
    page_size: int
    headless: bool
    out_dir: Path
    viewports: list[str]
    timeout_ms: int
    login_mode: str  # storage | ui


class Log:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, message: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
        self.lines.append(line)
        print(line, flush=True)


# ---------------------------------------------------------------------------
# 同步浏览器会话
# ---------------------------------------------------------------------------
class Session:
    """包装一次 Playwright 同步 context，负责登录注入与报告抽屉导航。"""

    def __init__(self, cfg: Config, auth: dict, log: Log, viewport: str = "1280x900"):
        self.cfg = cfg
        self.auth = auth
        self.log = log
        self.viewport = viewport
        self._pw = None
        self.browser = None
        self.context = None
        self.page = None
        self.open_report_via = ""

    def __enter__(self) -> "Session":
        if sync_playwright is None:
            raise RuntimeError("未安装 playwright，无法启动浏览器")
        width, height = (int(x) for x in self.viewport.lower().split("x"))
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=self.cfg.headless)
        self.context = self.browser.new_context(
            viewport={"width": width, "height": height},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        self.page = self.context.new_page()
        self.page.set_default_timeout(self.cfg.timeout_ms)
        self.page.set_default_navigation_timeout(max(self.cfg.timeout_ms, 30_000))
        self.inject_session()
        return self

    def __exit__(self, *exc) -> None:
        for closer in (
            lambda: self.context and self.context.close(),
            lambda: self.browser and self.browser.close(),
            lambda: self._pw and self._pw.stop(),
        ):
            try:
                closer()
            except Exception:
                pass

    # -- 登录 ---------------------------------------------------------------
    def auth_user_object(self) -> dict:
        return {
            "id": self.auth.get("userId", ""),
            "name": self.auth.get("name", ""),
            "role": self.auth.get("role", "teacher"),
            "mustChangePassword": bool(self.auth.get("mustChangePassword", False)),
        }

    def inject_session(self) -> None:
        page = self.page
        page.goto(f"{self.cfg.base_url}/login", wait_until="domcontentloaded")
        if self.cfg.login_mode == "ui":
            self.ui_login(page)
            return
        page.evaluate(
            """([token, refresh, user]) => {
                localStorage.setItem('token', token);
                localStorage.setItem('refreshToken', refresh);
                localStorage.setItem('authUser', JSON.stringify(user));
            }""",
            [self.auth["token"], self.auth.get("refreshToken", ""), self.auth_user_object()],
        )
        page.goto(f"{self.cfg.base_url}/reports", wait_until="domcontentloaded")
        if "/login" in page.url:
            self.log("localStorage 注入后被重定向回登录页，回退到 UI 登录表单")
            self.ui_login(page)

    def ui_login(self, page) -> None:
        page.goto(f"{self.cfg.base_url}/login", wait_until="domcontentloaded")
        page.get_by_label("用户名").fill(self.cfg.teacher_user)
        page.get_by_label("密码").fill(self.cfg.teacher_pass)
        page.get_by_role("button", name="登录").click()
        page.wait_for_url(re.compile(r"/(dashboard|reports|admin|profile)"), timeout=20_000)

    # -- 导航 ---------------------------------------------------------------
    def goto_reports(self) -> None:
        self.page.goto(f"{self.cfg.base_url}/reports", wait_until="domcontentloaded")

    def open_report(self, report_id: str, max_list_pages: int = 8) -> bool:
        """在报告列表里找到并打开某份报告的详情抽屉。

        定位方式（`Reports.vue` 本轮没有 testid，不在本轮范围，因此不臆造选择器）：
        1. 表格行 `data-row-key="<reportId>"`（antd 自带，稳定）；
        2. 行内的「查看」链接（`get_by_role("link", name="查看")`）；
        3. 行定位失败时，退化为「查看」链接与行文本（标题/ID）的匹配。
        实际使用的路径写进 `open_report_via`，并进入 JSON 的 `report_open_via`。
        """
        page = self.page
        self.open_report_via = ""
        if "/reports" not in page.url:
            self.goto_reports()
        try:
            page.wait_for_selector(".ant-table-row", timeout=20_000)
        except PlaywrightError:
            return False
        row_sel = f'.ant-table-row[data-row-key="{report_id}"]'
        for _ in range(max_list_pages):
            row = page.locator(row_sel)
            if row.count():
                row.first.scroll_into_view_if_needed()
                # `<a @click>` 无 href → 无 link 角色；必须用文本定位。
                link = row.first.locator("a", has_text="查看")
                target = link if link.count() else row.first.get_by_text("查看", exact=True)
                if target.count():
                    target.first.click()
                    self.open_report_via = "ant-table-row[data-row-key] + 「查看」文本链接"
                    return self.wait_detail_open()
                row.first.click()
                self.open_report_via = "ant-table-row[data-row-key] 整行点击（未找到「查看」链接）"
                return self.wait_detail_open()
            if not self._next_report_page():
                break
        return False

    def _next_report_page(self) -> bool:
        page = self.page
        for selector in (".ant-pagination-next button",):
            loc = page.locator(selector)
            if loc.count() and loc.first.is_enabled():
                loc.first.click()
                page.wait_for_timeout(800)
                return True
        return False

    def wait_detail_open(self, timeout_ms: int | None = None) -> bool:
        page = self.page
        deadline = time.time() + (timeout_ms or self.cfg.timeout_ms) / 1000
        while time.time() < deadline:
            for selector in (DRAWER, SEL["gallery"]):
                try:
                    if page.locator(selector).count():
                        return True
                except PlaywrightError:
                    pass
            page.wait_for_timeout(200)
        return False

    def drawer_title(self) -> str:
        """抽屉标题（`Reports.vue` 用 `detail?.title || '报告详情'`），用于 U04 判定抽屉里是哪份报告。"""
        try:
            loc = self.page.locator(DRAWER_TITLE)
            return (loc.first.inner_text() or "").strip() if loc.count() else ""
        except PlaywrightError:
            return ""

    def close_drawer(self) -> str:
        """关闭报告抽屉：优先 antd 关闭按钮，退化为 Esc。返回实际使用的方式。"""
        page = self.page
        close = page.locator(DRAWER_CLOSE)
        if close.count():
            close.first.click()
            page.wait_for_timeout(300)
            return "ant-drawer-close"
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        return "Escape"

    def wait_gallery(self, timeout_ms: int | None = None) -> bool:
        page = self.page
        try:
            page.wait_for_selector(SEL["gallery"], timeout=timeout_ms or self.cfg.timeout_ms)
            return True
        except PlaywrightError:
            return False

    # -- 状态重置 -----------------------------------------------------------
    def reset_gallery_filters(self, page_size: int) -> dict[str, Any]:
        """把画廊筛选重置为「全部」并回到第 1 页。

        复核 R10：U01 会把筛选停在某个动作上，U02 却按**全部帧**算页码，两处不一致
        会让长视频样本定位到错误的页。每个用例开始前重置筛选，或按当前筛选集合计算页码。
        """
        page = self.page
        state: dict[str, Any] = {"filterReset": False, "pageReset": False}
        option = page.locator(f'{SEL["filter"]}[data-code=""]')
        if option.count():
            try:
                option.first.click()
                page.wait_for_timeout(400)
                state["filterReset"] = True
            except PlaywrightError:
                pass
        # 从末页/中间页退回第 1 页：最多退 page_size 次（正常一页一次即可）。
        for _ in range(max(1, page_size)):
            prev = page.locator(SEL["prev"])
            if not prev.count():
                break
            try:
                if not prev.first.is_enabled():
                    state["pageReset"] = True
                    break
                prev.first.click()
                page.wait_for_timeout(150)
            except PlaywrightError:
                break
        if not state["pageReset"]:
            state["pageReset"] = not (page.locator(SEL["prev"]).count()
                                      and page.locator(SEL["prev"]).first.is_enabled())
        state["pageIndicator"] = _ints(self.text("page_indicator"))
        return state

    # -- 断言辅助 -----------------------------------------------------------
    def count(self, selector: str) -> int:
        try:
            return self.page.locator(selector).count()
        except PlaywrightError:
            return -1

    def missing(self, keys: Iterable[str]) -> list[str]:
        missing = []
        for key in keys:
            if self.count(SEL[key]) <= 0:
                missing.append(key)
        return missing

    def wait_count(self, selector: str, predicate: Callable[[int], bool], timeout: float = 10.0) -> int:
        deadline = time.time() + timeout
        value = -1
        while time.time() < deadline:
            value = self.count(selector)
            if value >= 0 and predicate(value):
                return value
            self.page.wait_for_timeout(150)
        return self.count(selector)

    def visible(self, selector: str) -> bool:
        """以可见性判断（Ant Design 的 modal/drawer 关闭后仍留在 DOM 里）。"""
        loc = self.page.locator(selector)
        if not loc.count():
            return False
        try:
            return loc.first.is_visible()
        except PlaywrightError:
            return False

    def text(self, key_or_selector: str) -> str:
        """接受契约键（如 `total`）或直接的 CSS 选择器。"""
        selector = SEL.get(key_or_selector, key_or_selector)
        loc = self.page.locator(selector)
        if not loc.count():
            return ""
        try:
            return (loc.first.inner_text() or "").strip()
        except PlaywrightError:
            return ""

    def screenshot(self, case: str, mode: str, viewport: str, tag: str = "") -> str | None:
        self.cfg.out_dir.mkdir(parents=True, exist_ok=True)
        suffix = f"-{tag}" if tag else ""
        name = f"{SCREENSHOT_PREFIX}{case}-{mode}-{viewport}{suffix}.png"
        path = self.cfg.out_dir / name
        try:
            self.page.screenshot(path=str(path), full_page=False)
            return str(path)
        except PlaywrightError:
            return None


# ---------------------------------------------------------------------------
# 报告发现（真实数据）
# ---------------------------------------------------------------------------
def discover_report(api: Api, token: str, log: Log) -> tuple[str, list[dict]]:
    """挑选一份报告；返回 (reportId, frames)。

    选择顺序（这是一个**可判定的**优先级，不是偏好）：

    1. 证据帧非空 **且** 已发布版本 >= 2，且其中至少两个版本的
       ``analysisVideoJobId`` 不同。
       理由：U04 的两半各需要一个条件——延迟响应需要帧，版本隔离需要
       "两个版本、两个 job"。此前只按帧数最多挑，在本机挑中的是
       329 帧 / 1 个版本的报告，版本隔离那条因此**永远**不可能通过，
       U04 只能长期停在 incomplete。那不是"数据未就绪"，是选错了对象。
    2. 退化为帧数最多的一份（并让 `require()` 把缺口显式报出来）。

    遍历仍然覆盖全部报告，不是"找到就走"就降低覆盖：候选打分在循环里完成，
    第 1 类候选出现时不会提前退出（否则报告顺序会影响结果）。
    """
    best_plain: tuple[str, list[dict]] = ("", [])
    best_eligible: tuple[str, list[dict]] = ("", [])
    reports = api.list_reports(token)
    log(f"报告列表返回 {len(reports)} 条")
    for report in reports:
        rid = report.get("id")
        if not rid or not report.get("generated", True):
            continue
        status, body = api.behavior_evidence(token, rid)
        if status != 200:
            continue
        frames = (body.get("data") or []) if isinstance(body, dict) else []
        if len(frames) > len(best_plain[1]):
            best_plain = (rid, frames)
        if not frames:
            continue
        versions = api.report_versions(token, rid)
        jobs = {str(v.get("analysisVideoJobId") or "") for v in versions
                if isinstance(v, dict)}
        jobs.discard("")
        # 需要 >=2 个已发布版本，且它们的 job 不全相同。
        if len(versions) >= 2 and len(jobs) >= 2 and len(frames) > len(best_eligible[1]):
            best_eligible = (rid, frames)
    if best_eligible[0]:
        log(f"报告选择：{best_eligible[0]}（{len(best_eligible[1])} 帧，"
            f">=2 个版本且 job 不同——U04 版本隔离可用）")
        return best_eligible
    log("报告选择：没有同时满足「有证据帧 + >=2 个不同 job 的已发布版本」的报告；"
        "退回帧数最多的一份（U04 的版本隔离将记为必检缺失，而不是通过）")
    return best_plain


# ---------------------------------------------------------------------------
# 用例实现
# ---------------------------------------------------------------------------
def case_u01(s: Session, res: CaseResult, cfg: Config, frames: list[dict], fixture: bool) -> None:
    # 空执行守卫：函数体第一句就置位。函数被删空或分发处漏调用时，
    # finalize() 拿不到这个标记，结果只能是 incomplete，不会是 passed。
    res.executed = True
    required = ["gallery", "card", "pagination", "page_indicator", "next", "filter", "total"]
    missing = s.missing(required)
    if missing:
        res.status, res.missing = "skipped", missing
        res.reason = f"缺少 data-testid：{', '.join(missing)}（组件契约缺失，不是环境问题）"
        return

    # 全局总量取「全部 N」筛选按钮（data-code=""）的文本，形如「全部 329」；
    # 页码状态取「第 X / Y 页，共 N 张」。两者都随全量数据变化，筛选只改页码状态里 N。
    total_expected = len(frames)
    page_size = cfg.page_size
    pages_expected = max(1, _ceil_div(total_expected, page_size))

    n = s.wait_count(SEL["card"], lambda c: c > 0, timeout=15)
    res.check("当前页卡片数 > 0", n > 0, f"count={n}")
    res.check("当前页 DOM 卡片数 <= 每页上限",
             0 < n <= page_size, f"count={n}, page_size={page_size}")

    total_text = s.text("total")
    total_seen = _ints(total_text)
    res.check("总数展示与数据一致（evidence-filter[data-code=''] 的「全部 N」）",
             bool(total_seen) and total_seen[0] == total_expected,
             f"DOM='{total_text}', 期望 {total_expected}")

    status_text = s.text("page_indicator")
    indicator = _ints(status_text)
    res.check("页码状态含「共 N 张」且等于数据条数",
             len(indicator) >= 3 and indicator[2] == total_expected,
             f"DOM='{status_text}', 期望共 {total_expected} 张")
    res.check("页码指示器总页数正确",
             len(indicator) >= 2 and indicator[1] == pages_expected,
             f"DOM='{status_text}', 期望总页数 {pages_expected}")
    res.check("页码指示器起始为第 1 页",
             bool(indicator) and indicator[0] == 1, f"DOM={indicator}")

    for _ in range(pages_expected - 1):
        nxt = s.page.locator(SEL["next"]).first
        if not nxt.is_enabled():
            break
        nxt.click()
        s.page.wait_for_timeout(400)
    s.wait_count(SEL["card"], lambda c: c > 0, timeout=10)
    last_expected = total_expected - page_size * (pages_expected - 1)
    res.check("末页卡片数等于余数",
             s.count(SEL["card"]) == last_expected,
             f"count={s.count(SEL['card'])}, 期望 {last_expected}")
    last_indicator = _ints(s.text("page_indicator"))
    res.check("末页页码正确",
             bool(last_indicator) and last_indicator[0] == pages_expected,
             f"DOM={last_indicator}")

    # 筛选：此时仍停在末页（页数 > 1 时必然不是第 1 页），点击筛选后必须回到第 1 页
    res.check("筛选前不在第 1 页（使回首页断言有意义）",
             pages_expected == 1 or (bool(last_indicator) and last_indicator[0] > 1),
             f"当前页={last_indicator}, 总页数={pages_expected}")
    counts: dict[str, int] = {}
    for frame in frames:
        counts[frame.get("code", "")] = counts.get(frame.get("code", ""), 0) + 1
    target_code = max(counts, key=lambda k: counts[k]) if counts else ""
    res.check("筛选计数之和等于总数",
             sum(counts.values()) == total_expected,
             f"sum={sum(counts.values())}, total={total_expected}")

    if target_code:
        option = s.page.locator(f'{SEL["filter"]}[data-code="{target_code}"]')
        if not option.count():
            res.skip("动作筛选点击", f"缺少 [data-testid=evidence-filter][data-code={target_code}]")
        else:
            option.first.click()
            s.page.wait_for_timeout(500)
            after = _ints(s.text("page_indicator"))
            res.check("筛选后回到第 1 页",
                     bool(after) and after[0] == 1, f"DOM={after}")
            filtered = counts[target_code]
            filtered_pages = max(1, _ceil_div(filtered, page_size))
            res.check("筛选后总页数与筛选结果一致",
                     len(after) >= 2 and after[1] == filtered_pages,
                     f"DOM={after}, 期望 {filtered_pages} 页 / {filtered} 条")
            n_filtered = s.wait_count(SEL["card"], lambda c: c >= 0, timeout=6)
            res.check("筛选后当前页卡片数 <= 每页上限",
                     0 <= n_filtered <= page_size, f"count={n_filtered}")
            codes = set()
            cards = s.page.locator(SEL["card"])
            for i in range(min(cards.count(), 40)):
                codes.add(cards.nth(i).get_attribute("data-code") or "")
            res.check("筛选后可见卡片动作一致（data-code）",
                     codes == {target_code}, f"data-code 集合={sorted(codes)}")
            res.check("总数展示不随筛选变化（全局总量）",
                     (_ints(s.text("total")) or [0])[0] == total_expected,
                     f"DOM='{s.text('total')}'")

    if fixture:
        res.add("fixture 标注", "pass",
                f"本用例使用合成夹具 {total_expected} 条，不是真实推理闭环")
    shot = s.screenshot("u01", res.mode, "1280x900")
    if shot:
        res.screenshots.append(shot)
    # 统一走 finalize()：手写 `else "passed"` 会绕过「至少一条通过断言」与
    # 「executed 标记」两道守卫，让零断言用例也能写成 passed。
    res.finalize()


def _frame_page(frames: list[dict], frame_id: str, page_size: int) -> int | None:
    """帧在**当前渲染顺序**（组件按 API 顺序渲染全部帧）里的页码；找不到返回 None。

    复核 R10：旧实现按 timestamp 排序推算页码，与组件实际渲染顺序不一致，
    且没有考虑 U01 遗留的筛选，长视频样本因此定位失败。
    """
    for index, frame in enumerate(frames):
        if frame.get("id") == frame_id:
            return index // page_size + 1
    return None


def _goto_page(s: Session, target_page: int, page_size: int) -> bool:
    """从第 1 页翻到 target_page（每步都读页码状态，不做盲目点击）。"""
    for _ in range(max(1, page_size)):
        current = _ints(s.text("page_indicator"))
        if current and current[0] >= target_page:
            return current[0] == target_page
        nxt = s.page.locator(SEL["next"])
        if not nxt.count() or not nxt.first.is_enabled():
            return False
        nxt.first.click()
        s.page.wait_for_timeout(350)
    current = _ints(s.text("page_indicator"))
    return bool(current) and current[0] == target_page


def _card_image_decoded(s: Session, frame_id: str, timeout: float = 12.0) -> bool:
    """等卡片图片真正解码（naturalWidth > 0）。

    组件现在只在解码成功后才叠加检测框（R08），因此「框没出现」有两种原因：
    图片没解码，或组件没画。必须先区分，否则会把加载慢误判成缺陷。
    """
    deadline = time.time() + timeout
    expr = """(el) => {
        const img = el.querySelector('[data-testid="evidence-card-image"]');
        return img ? (img.complete && img.naturalWidth > 0) : false;
    }"""
    while time.time() < deadline:
        card = s.page.locator(f'{SEL["card"]}[data-frame-id="{frame_id}"]')
        if card.count():
            try:
                if card.first.evaluate(expr):
                    return True
            except PlaywrightError:
                pass
        s.page.wait_for_timeout(250)
    return False


def _gallery_images_decoded(s: Session) -> bool:
    """当前页是否至少有一张卡片图片真正解码完成（naturalWidth > 0）。

    U03 的重试闭环用它判断「恢复」是否真的发生：只看重试按钮消失或
    error 横幅清空都不够，必须证明图片字节确实被浏览器解码了。
    """
    try:
        return bool(s.page.evaluate(
            """() => Array.from(
                    document.querySelectorAll('[data-testid="evidence-card-image"]'))
                .some(img => img.complete && img.naturalWidth > 0)"""))
    except PlaywrightError:
        return False


def case_u02(s: Session, res: CaseResult, cfg: Config, frames: list[dict], fixture: bool) -> None:
    # 空执行守卫：函数体第一句就置位。函数被删空或分发处漏调用时，
    # finalize() 拿不到这个标记，结果只能是 incomplete，不会是 passed。
    res.executed = True
    # preview 只在打开后存在，不能放进静态能力探测，否则永远误判缺失。
    required = ["gallery", "card", "frame_time", "card_media", "detection_box"]
    missing = s.missing(required)
    if missing:
        res.status, res.missing = "skipped", missing
        res.reason = f"缺少 data-testid：{', '.join(missing)}"
        return

    # 用例开始前重置筛选到「全部」并回到第 1 页：U01 会停在某个动作筛选与末页，
    # 不重置就按全部帧算页码，长视频样本必然定位失败（复核 R10 第 3 条）。
    reset = s.reset_gallery_filters(cfg.page_size)
    res.check("用例开始前筛选重置为「全部」且回到第 1 页",
             reset["filterReset"] and reset["pageReset"],
             f"filterReset={reset['filterReset']}, pageReset={reset['pageReset']}, "
             f"页码={reset['pageIndicator']}")
    active_filters = s.page.locator(f'{SEL["filter"]}.active')
    res.check("重置后生效筛选为「全部」",
             bool(active_filters.count())
             and (active_filters.first.get_attribute("data-code") or "") == "",
             f"active data-code={active_filters.first.get_attribute('data-code') if active_filters.count() else None}")

    times = s.page.locator(SEL["frame_time"]).all_inner_texts()
    times = [t.strip() for t in times if t and t.strip()]
    res.check("时间标签存在", bool(times), f"{len(times)} 个")
    unknown = [t for t in times if t == "时间未知"]
    bad = [t for t in times if t != "时间未知" and not re.fullmatch(r"\d{2,}:\d{2}:\d{2}", t)]
    res.check("时间统一为 HH:MM:SS 或「时间未知」", not bad, f"不合规样本={bad[:5]}")
    if unknown:
        res.add("缺失时间如实显示", "pass",
                f"{len(unknown)} 个帧显示「时间未知」，未伪装成 00:00:00")

    # ---- >=1 小时样本：必检 ------------------------------------------------
    long_frames = [f for f in frames if (f.get("timestampMs") or 0) >= 3_600_000]
    if not long_frames:
        res.require("以 HH:MM:SS 展示 >=1 小时的时间样本",
                    f"当前报告 {len(frames)} 帧中没有任何 >=3600s 的证据帧；"
                    "该必检项需要 120 分钟报告，本次未验证")
    else:
        checked = False
        for frame in long_frames[:5]:
            target_page = _frame_page(frames, frame["id"], cfg.page_size)
            if target_page is None:
                continue
            if not _goto_page(s, target_page, cfg.page_size):
                continue
            card = s.page.locator(f'{SEL["card"]}[data-frame-id="{frame["id"]}"]')
            if not card.count():
                continue
            shown_text = (card.first.locator(SEL["frame_time"]).inner_text() or "").strip()
            secs = int((frame.get("timestampMs") or 0) // 1000)
            expected = f"{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}"
            res.check("以 HH:MM:SS 展示 >=1 小时的时间样本（逐字比对 DOM）",
                      shown_text == expected,
                      f"frame={frame['id']} 第 {target_page} 页, DOM='{shown_text}', 期望 '{expected}'")
            checked = True
            break
        if not checked:
            res.require("以 HH:MM:SS 展示 >=1 小时的时间样本",
                        f"数据集有 {len(long_frames)} 个 >=3600s 帧，但按渲染顺序翻页后均未定位到对应卡片")
        # 回到第 1 页（保留「全部」筛选），后续断言按首页元素做
        s.reset_gallery_filters(cfg.page_size)

    # ---- 检测框坐标 / 文本 / 置信度：必检 ----------------------------------
    # 只在「当前已渲染的卡片」里找，避免目标帧不在第 1 页时误判。
    rendered_ids = s.page.eval_on_selector_all(
        SEL["card"], "els => els.map(e => e.getAttribute('data-frame-id'))")
    rendered = [f for f in frames if f.get("id") in set(rendered_ids)]
    with_det = [f for f in rendered if primary_detection(f)]
    if not rendered:
        res.require("检测框坐标核对", "当前页没有带 data-frame-id 的卡片")
    elif not with_det:
        res.require("检测框坐标核对", "当前页卡片没有目标级检测框")
    else:
        frame = with_det[0]
        det = primary_detection(frame)
        card_sel = f'{SEL["card"]}[data-frame-id="{frame["id"]}"]'
        # R08 之后检测框只在解码成功后才渲染：先等解码，再判断框。
        decoded = _card_image_decoded(s, frame["id"])
        if not res.check("卡片图片已解码（检测框渲染的前置条件）", decoded,
                         f"frame={frame['id']} naturalWidth>0 未在 12s 内出现"):
            res.require("检测框坐标核对", "卡片图片未解码，组件按 R08 语义不渲染检测框")
        else:
            boxes = s.page.locator(card_sel).first.locator(SEL["detection_box"])
            box_count = s.wait_count(f"{card_sel} {SEL['detection_box']}", lambda c: c >= 1, timeout=6)
            res.check("卡片只渲染 1 个主检测框", box_count == 1,
                     f"count={box_count}（组件 primary() 只高亮主框）")
            if box_count >= 1:
                box = boxes.first
                style = box.get_attribute("style") or ""
                left = re.search(r"left:\s*([\d.]+)%", style)
                res.check("检测框 left 与主框 bbox 一致",
                         bool(left) and abs(float(left.group(1)) - det["bbox"][0] * 100) < 0.6,
                         f"style='{style}', 期望 left≈{det['bbox'][0] * 100:.1f}%")
                caption = (box.inner_text() or "").strip()
                expected_conf = f"{det['confidence'] * 100:.1f}%"
                res.check("框文本含动作与置信度",
                         det["label"] in caption and expected_conf in caption,
                         f"caption='{caption}', 期望含 '{det['label']}' 与 '{expected_conf}'")

    # ---- 预览 + 自适应宽度 -------------------------------------------------
    # R10 第 4 条：不要固定打开首张 overview（目标数为 0，于是永远 skip 目标切换）。
    # 按 API 元数据挑选「safeDetections 过滤后 >= 2」的帧，翻到它所在页再打开。
    multi = [f for f in frames if len(_safe_detections(f)) >= 2]
    target_frame = multi[0] if multi else None
    opened_frame = None
    if target_frame is None:
        res.require("同帧目标切换",
                    "当前报告的 API 元数据里没有任何 frames.metadata.detections 过滤后 >=2 个合法目标")
        s.page.locator(SEL["card_media"]).first.click()
    else:
        target_page = _frame_page(frames, target_frame["id"], cfg.page_size) or 1
        if not _goto_page(s, target_page, cfg.page_size):
            res.require("同帧目标切换", f"无法翻到帧 {target_frame['id']} 所在的第 {target_page} 页")
            s.page.locator(SEL["card_media"]).first.click()
        else:
            media = s.page.locator(f'{SEL["card_media"]}[data-frame-id="{target_frame["id"]}"]')
            if not media.count():
                res.require("同帧目标切换",
                            f"第 {target_page} 页没有帧 {target_frame['id']} 的卡片（渲染顺序与 API 不一致）")
                s.page.locator(SEL["card_media"]).first.click()
            else:
                res.add("目标切换选帧依据", "pass",
                        f"按 API 元数据选中 {target_frame['id']}（{len(_safe_detections(target_frame))} 个合法目标），"
                        f"第 {target_page} 页")
                media.first.click()
                opened_frame = target_frame

    preview_visible = False
    deadline = time.time() + cfg.timeout_ms / 1000
    while time.time() < deadline:
        if s.visible(SEL["preview"]):
            preview_visible = True
            break
        s.page.wait_for_timeout(150)
    res.check("点击卡片打开预览", preview_visible)
    if preview_visible:
        # R08 之后预览检测框同样只在图片解码成功后渲染：先等解码，再判断框与目标切换，
        # 否则会把「还在加载」误判成「没有框」。
        preview_decoded = False
        decode_deadline = time.time() + 12
        while time.time() < decode_deadline:
            loc = s.page.locator(SEL["preview_image"])
            if loc.count():
                try:
                    if loc.first.evaluate("el => el.complete && el.naturalWidth > 0"):
                        preview_decoded = True
                        break
                except PlaywrightError:
                    pass
            s.page.wait_for_timeout(250)
        res.check("预览图已解码（预览检测框渲染的前置条件）", preview_decoded)
        image = s.page.locator(SEL["preview_image"])
        if image.count():
            # 自适应：预览图宽/高 ≈ 图片自身宽高比（非固定像素尺寸）。
            ratio = image.first.evaluate(
                """el => {
                    const rect = el.getBoundingClientRect();
                    const natural = el.naturalWidth && el.naturalHeight
                        ? el.naturalWidth / el.naturalHeight : null;
                    return { boxW: rect.width, boxH: rect.height, natural };
                }""")
            box_ratio = (ratio["boxW"] / ratio["boxH"]) if ratio["boxH"] else 0
            natural_ratio = ratio["natural"]
            if not natural_ratio:
                # 图片未解码（403/未加载）时无法判定宽高比，不做空转通过。
                res.skip("预览图宽度按图片宽高比自适应（非固定像素）",
                         "预览图未成功解码，naturalWidth/Height 为空")
            else:
                res.check("预览图宽度按图片宽高比自适应（非固定像素）",
                         bool(ratio["boxW"]) and bool(ratio["boxH"])
                         and abs(box_ratio - natural_ratio) / max(natural_ratio, 0.01) < 0.08,
                         f"渲染 {ratio['boxW']:.1f}x{ratio['boxH']:.1f} (ratio≈{box_ratio:.3f}), "
                         f"图片自然比≈{natural_ratio:.3f}")

            # ---- P1-2：缩放控件必须真实改变渲染宽度并遵守上下限 ----
            # 只断言"按钮存在"是不够的：一个不接线的按钮也能过。这里点 zoom-in
            # 并重新量宽度，必须变大；连点到上限后 zoom-in 必须变 disabled；
            # 点 reset 必须回到与初始一致（±2px，避免亚像素抖动）。
            zoom_in = s.page.locator(SEL["zoom_in"])
            zoom_out = s.page.locator(SEL["zoom_out"])
            zoom_reset = s.page.locator(SEL["zoom_reset"])
            level = s.page.locator(SEL["zoom_level"])
            if zoom_in.count() and zoom_out.count() and zoom_reset.count():
                # 基准宽度必须在缩放**之前重新测量**：上面那次 ratio 是在预览刚打开、
                # antd 模态入场动画还没结束时量的，会得到 172px 这种过渡值，
                # 于是"reset 回到初始值"永远对不上（实测 172.9 → 864.7）。
                # 这里先等布局稳定，再把"当前宽度"同时当作基准。
                s.page.wait_for_timeout(600)
                width0 = image.first.evaluate("el => el.getBoundingClientRect().width")
                level0 = (level.first.inner_text().strip() if level.count() else "")
                zoom_in.first.click()
                s.page.wait_for_timeout(350)
                width1 = image.first.evaluate("el => el.getBoundingClientRect().width")
                level1 = (level.first.inner_text().strip() if level.count() else "")
                res.check("缩放 + 后渲染宽度变大", width1 > width0 + 1,
                         f"{width0:.1f}px → {width1:.1f}px")
                res.check("缩放级别文本随缩放变化", bool(level0) and level0 != level1,
                         f"'{level0}' → '{level1}'")

                # 连点到上限，然后确认 zoom-in 被禁用（而不是静默无效果）。
                for _ in range(16):
                    if zoom_in.first.is_disabled():
                        break
                    zoom_in.first.click()
                    s.page.wait_for_timeout(60)
                res.check("到达上限后 zoom-in 被禁用", zoom_in.first.is_disabled(),
                         f"level={level.first.inner_text().strip() if level.count() else '?'}")
                at_max = image.first.evaluate("el => el.getBoundingClientRect().width")
                res.check("缩放到上限确实大于初始宽度", at_max > width0 + 1,
                         f"{width0:.1f}px → {at_max:.1f}px")

                # 连点到下限，确认 zoom-out 被禁用且回到初始宽度。
                for _ in range(24):
                    if zoom_out.first.is_disabled():
                        break
                    zoom_out.first.click()
                    s.page.wait_for_timeout(60)
                res.check("到达下限后 zoom-out 被禁用", zoom_out.first.is_disabled(),
                         f"level={level.first.inner_text().strip() if level.count() else '?'}")
                width_min = image.first.evaluate("el => el.getBoundingClientRect().width")
                res.check("缩放到下限回到初始宽度（越小越不放大）",
                         abs(width_min - width0) <= 2,
                         f"初始 {width0:.1f}px，下限 {width_min:.1f}px")

                # reset 从任意状态回到 100%。
                for _ in range(3):
                    zoom_in.first.click()
                    s.page.wait_for_timeout(60)
                zoom_reset.first.click()
                s.page.wait_for_timeout(350)
                width_reset = image.first.evaluate("el => el.getBoundingClientRect().width")
                level_reset = (level.first.inner_text().strip() if level.count() else "")
                res.check("reset 把宽度恢复到初始值", abs(width_reset - width0) <= 2,
                         f"初始 {width0:.1f}px，reset 后 {width_reset:.1f}px（level={level_reset}）")
                res.check("reset 后缩放级别回到 100%",
                         "100" in level_reset, f"level='{level_reset}'")
            else:
                res.require("预览缩放控件（zoom-in/zoom-out/zoom-reset）",
                            "组件缺少缩放按钮 testid；P1-2 要求必须可缩放")

            # 原视频回跳：后端无受控媒体播放地址，按钮必须"存在且禁用"。
            jump = s.page.locator(SEL["jump_to_video"])
            if jump.count():
                res.check("原视频回跳按钮存在且明确禁用（后端未提供受控播放地址）",
                         jump.first.is_disabled(),
                         f"disabled={jump.first.is_disabled()}, "
                         f"title={(jump.first.get_attribute('title') or '')[:60]}")
            else:
                res.require("原视频回跳按钮", "缺少 evidence-jump-to-video（即使禁用也必须存在并给出原因）")
        else:
            # 预览图缺失时，缩放/关闭/Esc/焦点回收/同帧切换共 20+ 条断言全部不会执行。
            # 用 skip 会让用例照样 passed —— 这正是"缺证据也能绿"。改成 require。
            res.require("预览图（evidence-preview-image）", "缺少预览图，P1-2 的缩放/关闭断言无法执行")

        targets = s.page.locator(SEL["preview_target"])
        if targets.count() >= 2:
            labels = [targets.nth(i).inner_text().strip() for i in range(min(targets.count(), 6))]
            # 不能假设"第二个按钮"就是与当前高亮不同的目标：组件默认高亮
            # primary() 选中的那个框（通常是置信度最高者），它不一定排在第一位。
            # 实测就出现过 nth(1) 恰好已是选中项，于是"切换后位置变化"必然失败——
            # 那是断言的假设错了，不是产品缺陷。这里显式挑一个未选中的目标。
            active_subject = ""
            active = s.page.locator(SEL["preview_target"] + ".active")
            if active.count():
                active_subject = active.first.get_attribute("data-subject-id") or ""
            second = None
            for i in range(targets.count()):
                candidate = targets.nth(i)
                if (candidate.get_attribute("data-subject-id") or "") != active_subject:
                    second = candidate
                    break
            if second is None:
                res.require("同帧目标切换（需要 >=2 个不同目标）",
                            f"预览内 {targets.count()} 个按钮的 subject 全部相同（{active_subject}）")
                second = targets.nth(1)
            subject = second.get_attribute("data-subject-id") or ""
            boxes = s.page.locator(SEL["preview"] + " " + SEL["detection_box"])
            before_style = (boxes.first.get_attribute("style") or "") if boxes.count() else ""
            before_text = (boxes.first.inner_text() or "").strip() if boxes.count() else ""
            second.click()
            s.page.wait_for_timeout(400)
            after_boxes = s.page.locator(SEL["preview"] + " " + SEL["detection_box"])
            box_count = after_boxes.count()
            box_text = (after_boxes.first.inner_text() or "").strip() if box_count else ""
            after_style = (after_boxes.first.get_attribute("style") or "") if box_count else ""
            res.check("同帧目标切换后只高亮所选目标",
                     box_count == 1, f"count={box_count}, 已选={subject}")
            res.check("切换后检测框位置随之变化（style 与 caption 同步）",
                     bool(after_style) and (after_style != before_style or box_text != before_text),
                     f"before style='{before_style}' text='{before_text}' → "
                     f"after style='{after_style}' text='{box_text}'")
            if subject:
                digits = re.sub(r"^target-", "", subject)
                res.check("切换后框内目标号同步",
                         f"目标 {digits}" in box_text,
                         f"box='{box_text}', 目标 {digits}")
            caption = box_text.split("·", 1)[-1].strip() if "·" in box_text else box_text
            # 原写法 `any(part.strip() in caption for part in caption.split())` 对任何非空
            # caption 恒为真（每个词当然在自己里面），实际只测了 "%"。这里改成：
            # 必须含一个已知动作词（来自模型类别表）且含百分号，并给出期望动作词。
            action_words = ("举手", "站立", "离座", "低头", "转头", "讨论", "书写", "阅读")
            matched = [w for w in action_words if w in caption]
            res.check("切换后框内文本含已知动作词与置信度百分比",
                     "%" in caption and bool(matched),
                     f"caption='{caption}' 命中的动作词={matched}")
            summary = s.text(SEL["preview"])
            res.check("切换后置信度文本随之更新（非空且含%）",
                     "%" in summary and any(label.split("·")[-1].strip()[:2] in summary for label in labels),
                     f"预览文本片段='{summary[:120]}'")
        else:
            res.require("同帧目标切换（需要 >=2 个不同目标）",
                        f"预览内可选目标数={targets.count()}；缺第二目标时切换断言无法执行")

        # P1-2 起组件提供显式关闭按钮。仍然先断言显式按钮（这是契约），
        # 再单独验证 Esc 路径——两条路径都要能用，因为 Esc 由 antd 模态实现，
        # 换 antd 版本时它可能被改掉，而显式按钮不会。
        explicit_close = s.page.locator(SEL["preview_close"])
        if not explicit_close.count():
            res.require("预览显式关闭按钮（evidence-preview-close）",
                        "组件未提供关闭按钮 testid；P1-2 要求必须可键盘/鼠标关闭")
            s.page.keyboard.press("Escape")
            close_via = "Escape（显式按钮缺失）"
        else:
            explicit_close.first.click()
            close_via = "evidence-preview-close"
        closed = False
        deadline = time.time() + 10
        while time.time() < deadline:
            if not s.visible(SEL["preview"]):
                closed = True
                break
            s.page.wait_for_timeout(150)
        res.check("预览可通过显式关闭按钮关闭", closed, f"关闭方式={close_via}")

        # 重新打开再按 Esc：验证第二条关闭路径，并确认焦点回到触发卡片。
        if s.count(SEL["card_media"]):
            s.page.locator(SEL["card_media"]).first.click()
            s.page.wait_for_timeout(600)
            reopened = s.visible(SEL["preview"])
            res.check("可重新打开预览（Esc 路径的前置条件）", reopened, f"visible={reopened}")
            if reopened:
                s.page.keyboard.press("Escape")
                esc_closed = False
                deadline = time.time() + 10
                while time.time() < deadline:
                    if not s.visible(SEL["preview"]):
                        esc_closed = True
                        break
                    s.page.wait_for_timeout(150)
                res.check("预览可通过 Esc 关闭", esc_closed, "Esc 由 antd 模态处理")
                # 焦点回收：Esc 关闭后 activeElement 必须是卡片媒体按钮或分页容器，
                # 不能掉到 <body>（键盘用户会丢失位置）。
                active_testid = s.page.evaluate(
                    """() => {
                        const el = document.activeElement;
                        if (!el) return "";
                        return el.getAttribute("data-testid") || el.tagName.toLowerCase();
                    }""")
                res.check("Esc 关闭后焦点落在卡片/分页容器上（未掉到 body）",
                         active_testid in ("evidence-card-media", "evidence-pager"),
                         f"activeElement data-testid={active_testid!r}")

    shot = s.screenshot("u02", res.mode, "1280x900")
    if shot:
        res.screenshots.append(shot)
    # 统一走 finalize()：手写 `else "passed"` 会绕过「至少一条通过断言」与
    # 「executed 标记」两道守卫，让零断言用例也能写成 passed。
    res.finalize()


def case_u03(s: Session, res: CaseResult, cfg: Config, frames: list[dict], fixture: bool) -> None:
    # 空执行守卫：函数体第一句就置位。函数被删空或分发处漏调用时，
    # finalize() 拿不到这个标记，结果只能是 incomplete，不会是 passed。
    res.executed = True
    # error / retry 只在故障发生后渲染，不能做静态能力探测：
    # 静态只要求画廊与卡片存在，错误提示在注入后作为真实断言检查。
    required = ["gallery", "card"]
    missing = s.missing(required)
    if missing:
        res.status, res.missing = "skipped", missing
        res.reason = f"缺少 data-testid：{', '.join(missing)}"
        return

    # 只让首批签名图片请求返回 403，模拟签名过期 / 对象被删除；
    # 这是浏览器层故障注入，JSON 中如实标注，不冒充真实对象删除。
    signed = [f.get("url") for f in frames if (f.get("url") or "").startswith("http")]
    if not signed:
        res.status = "skipped"
        res.reason = "当前证据图片不是签名 HTTP URL（fixture 模式），U03 需要 --mode real 的真实签名图片"
        return
    # 故障注入必须在**任何** MinIO 请求之前注册：Chromium 对已加载过的图片
    # 可能直接从内存/磁盘缓存返回，`page.route` 在注册之后才生效，迟注册会
    # 让注入静默失效（本轮实测：刷新后 0 次拦截）。因此这里一开始就注册，
    # 用 armed 标志控制「何时开始返回 403」。
    state = {"fail_left": 0, "requests": {}, "armed": False, "intercepted": 0}

    def handle(route):
        url = route.request.url
        bucket = url.split("?")[0]
        if "X-Amz-Signature" in url or "19100" in url or ":9100" in url or ":9000" in url:
            state["intercepted"] += 1
        state["requests"][bucket] = state["requests"].get(bucket, 0) + 1
        if state["armed"] and state["fail_left"] > 0 and "X-Amz-Signature" in url:
            state["fail_left"] -= 1
            route.fulfill(status=403, body="AccessDenied",
                          headers={"Content-Type": "text/plain"})
            return
        route.continue_()

    s.page.route("**/*", handle)
    try:
        # 先把状态改成「非默认」：选一个动作筛选并翻到第 2 页，
        # 这样「刷新后保留筛选与页码」才不是空断言。
        counts: dict[str, int] = {}
        for frame in frames:
            counts[frame.get("code", "")] = counts.get(frame.get("code", ""), 0) + 1
        if s.count(SEL["filter"]) > 1 and counts:
            code = max(counts, key=lambda k: counts[k])
            option = s.page.locator(f'{SEL["filter"]}[data-code="{code}"]')
            if option.count():
                option.first.click()
                s.page.wait_for_timeout(400)
        page_before = _ints(s.text(SEL["page_indicator"])) if s.count(SEL["page_indicator"]) else []
        if s.count(SEL["next"]) and s.page.locator(SEL["next"]).first.is_enabled():
            s.page.locator(SEL["next"]).first.click()
            s.page.wait_for_timeout(400)
            page_before = _ints(s.text(SEL["page_indicator"]))
        filter_before = ""
        active = s.page.locator(f'{SEL["filter"]}.active')
        if active.count():
            filter_before = active.first.get_attribute("data-code") or ""

        if s.count(SEL["refresh"]):
            state["armed"] = True
            state["fail_left"] = 3
            s.page.locator(SEL["refresh"]).first.click()
            s.page.wait_for_timeout(1500)
            # 刷新会用新签名重新请求；图片是懒加载 + 解码后才触发 error 事件，
            # 固定 1.5s 常常早于浏览器真正发出请求。轮询到故障态出现为止
            # （最多 12s，超时仍按失败处理——这不是放宽断言，只是等待异步渲染）。
            has_error = False
            waited = 0
            while waited < 12000:
                if s.count(SEL["error"]) or s.count(SEL["retry"]):
                    has_error = True
                    break
                s.page.wait_for_timeout(500)
                waited += 500
            res.check("图片不可用时给出明确错误与重试入口", has_error,
                     f"error={s.count(SEL['error'])}, retry={s.count(SEL['retry'])}, "
                     f"waited={waited}ms, intercepted={state['intercepted']}")
            page_after = _ints(s.text(SEL["page_indicator"])) if s.count(SEL["page_indicator"]) else []
            active_after = s.page.locator(f'{SEL["filter"]}.active')
            filter_after = active_after.first.get_attribute("data-code") if active_after.count() else ""
            res.check("手动刷新后页码保留",
                     bool(page_before) and bool(page_after) and page_after[0] == page_before[0],
                     f"before={page_before}, after={page_after}")
            res.check("手动刷新后动作筛选保留",
                     filter_after == filter_before, f"before='{filter_before}', after='{filter_after}'")
        else:
            res.status = "skipped"
            res.missing = ["refresh"]
            res.reason = "缺少 data-testid=evidence-refresh，无法验证刷新后保留状态"
            return

        # 无限自动重试检测：清空计数后静置 3 秒，看是否还有自发请求
        state["requests"].clear()
        s.page.wait_for_timeout(3000)
        worst = max(state["requests"].values()) if state["requests"] else 0
        res.check("无无限自动重试（静置 3 秒内每个对象自发请求 <= 1 次）", worst <= 1,
                 f"最大自发请求次数={worst}, 对象数={len(state['requests'])}")
        res.add("故障注入标注", "pass",
                "本轮以浏览器层 403 模拟签名过期/对象不可用，未删除真实对象")

        # ------------------------------------------------------------------
        # 重试闭环（review R10）：解除注入 → 点重试 → 断言真正恢复解码。
        # 只断言「出现重试按钮」不足以证明恢复路径可用，必须走完整个闭环。
        # ------------------------------------------------------------------
        state["armed"] = False
        state["fail_left"] = 0
        retry_button = s.page.locator(SEL["retry"])
        if not retry_button.count():
            res.require("失效后可点击重试并恢复解码",
                        "故障态下未出现 evidence-retry，无法验证重试闭环")
        else:
            retry_button.first.click()
            recovered = False
            waited = 0
            # 重试会重新取签名 URL 并重新请求图片；解码成功需要真正加载完成。
            while waited < 15000:
                if _gallery_images_decoded(s):
                    recovered = True
                    break
                s.page.wait_for_timeout(500)
                waited += 500
            res.check("点击重试后图片重新解码成功（naturalWidth > 0）", recovered,
                     f"waited={waited}ms, decoded={_gallery_images_decoded(s)}")
            # 解码恢复后，检测框必须重新出现（R08：只有解码成功才画框）。
            res.check("恢复后检测框重新渲染（不是黑底叠加框）",
                     recovered and s.count(SEL["detection_box"]) > 0,
                     f"detection_box={s.count(SEL['detection_box'])}")
            res.check("恢复后不再显示失效占位", s.count(SEL["error"]) == 0 or recovered,
                     f"error={s.count(SEL['error'])}")
    finally:
        try:
            s.page.unroute("**/*", handle)
        except Exception:
            pass

    shot = s.screenshot("u03", res.mode, "1280x900")
    if shot:
        res.screenshots.append(shot)
    # 统一走 finalize()：手写 `else "passed"` 会绕过「至少一条通过断言」与
    # 「executed 标记」两道守卫，让零断言用例也能写成 passed。
    res.finalize()


def case_u05(s: Session | None, res: CaseResult, cfg: Config, api: Api,
             report_id: str, frames: list[dict]) -> None:
    """服务端鉴权：直接打后端，不依赖任何 UI 隐藏。"""
    # 空执行守卫：函数体第一句就置位。函数被删空或分发处漏调用时，
    # finalize() 拿不到这个标记，结果只能是 incomplete，不会是 passed。
    res.executed = True
    if not report_id:
        res.status = "skipped"
        res.reason = "没有可用的报告 ID，无法做服务端鉴权断言"
        return

    status, _ = api.behavior_evidence(None, report_id)
    res.check("匿名访问行为证据返回 401", status == 401, f"HTTP {status}")
    status, _ = api.behavior_evidence("not-a-real-token", report_id)
    res.check("伪造 token 返回 401", status == 401, f"HTTP {status}")

    # 正对照：教师本人必须能取到，否则后续 403 断言不成立
    teacher_token = s.auth["token"] if s else None
    if teacher_token:
        status, body = api.behavior_evidence(teacher_token, report_id)
        ok = status == 200 and isinstance(body, dict)
        res.check("教师本人读取成功（正对照）", ok, f"HTTP {status}")
    else:
        res.skip("教师本人读取成功（正对照）", "未建立教师会话")

    if cfg.student_user and cfg.student_pass:
        try:
            student = api.login(cfg.student_user, cfg.student_pass)
            status, _ = api.behavior_evidence(student["token"], report_id)
            res.check("学生访问行为证据返回 403", status == 403, f"HTTP {status}")
        except Exception as exc:
            res.check("学生访问行为证据返回 403", False, f"学生登录失败: {exc}")
    else:
        # 学生 403 是本轮明确的验收目标：缺账号时必须让用例 incomplete、退出码非零，
        # 不能用 skip 把它从结论里悄悄抹掉（review R10）。
        res.require("学生访问行为证据返回 403", "未提供 --student-user/--student-pass")

    other_token = None
    if cfg.other_user and cfg.other_pass:
        try:
            other_token = api.login(cfg.other_user, cfg.other_pass)["token"]
        except Exception as exc:
            res.check("另一教师访问返回 403", False, f"另一教师登录失败: {exc}")
    elif cfg.allow_register_other_teacher:
        suffix = str(int(time.time() * 1000))[-8:]
        try:
            other_token = api.register_teacher(
                f"e2e-teacher-b-{suffix}", f"E2e-{suffix}-pw", "E2E教师B")["token"]
            res.add("另一教师账号来源", "pass", f"运行期临时注册 e2e-teacher-b-{suffix}")
        except Exception as exc:
            res.check("另一教师访问返回 403", False, f"临时注册失败: {exc}")
    if other_token:
        status, _ = api.behavior_evidence(other_token, report_id)
        res.check("另一教师访问返回 403", status == 403, f"HTTP {status}")
    elif not (cfg.other_user or cfg.allow_register_other_teacher):
        # 与「学生 403」同理：另一教师的 403 是必检项，缺账号即覆盖不完整。
        res.require("另一教师访问返回 403",
                    "未提供 --other-teacher-user/--other-teacher-pass，也未允许临时注册")

    urls = [f.get("url") for f in frames if f.get("url")]
    if not urls:
        res.skip("无签名对象 URL 返回 403", "当前报告没有可用的签名图片 URL")
    else:
        unsigned = urls[0].split("?")[0]
        if not unsigned.startswith("http"):
            res.skip("无签名对象 URL 返回 403", f"图片不是 HTTP URL: {unsigned[:60]}")
        else:
            try:
                resp = requests.get(unsigned, timeout=20)
                res.check("无签名对象 URL 返回 403", resp.status_code == 403,
                         f"HTTP {resp.status_code} host={urlsplit(unsigned).netloc}")
            except Exception as exc:
                res.check("无签名对象 URL 返回 403", False, f"请求失败: {exc}")

    res.finalize()
    if res.status == "failed":
        res.reason = "存在失败断言（服务端鉴权不通过）"


# -- U04：延迟旧响应（async，避免同步 route handler 阻塞页面操作） -----------
async def _u04_async(cfg: Config, auth: dict, report_a: str, report_b: str,
                     frames_a: list[dict], frames_b: list[dict],
                     delay_seconds: float) -> list[dict]:
    results: list[dict] = []

    def add(name: str, status: str, detail: str = "") -> None:
        results.append({"name": name, "status": status, "detail": detail})

    if async_playwright is None:
        add("async Playwright 可用", "skip", "未安装 playwright.async_api")
        return results

    user = {
        "id": auth.get("userId", ""), "name": auth.get("name", ""),
        "role": auth.get("role", "teacher"),
        "mustChangePassword": bool(auth.get("mustChangePassword", False)),
    }
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=cfg.headless)
        context = await browser.new_context(viewport={"width": 1280, "height": 900}, locale="zh-CN")
        page = await context.new_page()
        page.set_default_timeout(cfg.timeout_ms)
        try:
            await page.goto(f"{cfg.base_url}/login", wait_until="domcontentloaded")
            await page.evaluate(
                """([token, refresh, u]) => {
                    localStorage.setItem('token', token);
                    localStorage.setItem('refreshToken', refresh);
                    localStorage.setItem('authUser', JSON.stringify(u));
                }""",
                [auth["token"], auth.get("refreshToken", ""), user],
            )

            delayed = {"done": False}

            async def handler(route):
                url = route.request.url
                if f"/reports/{report_a}/behavior-evidence" in url and not delayed["done"]:
                    delayed["done"] = True
                    await asyncio.sleep(delay_seconds)
                await route.continue_()

            await page.route("**/api/reports/*/behavior-evidence*", handler)
            await page.goto(f"{cfg.base_url}/reports", wait_until="domcontentloaded")
            await page.wait_for_selector(".ant-table-row", timeout=20_000)

            async def current_title() -> str:
                loc = page.locator(DRAWER_TITLE)
                if not await loc.count():
                    return ""
                return (await loc.first.inner_text() or "").strip()

            async def wait_drawer(expected_title_from: str = "",
                                  timeout_ms: int = 12_000) -> bool:
                """等抽屉**可见**且标题不再是旧值。

                为什么不能只等 `.ant-drawer-content` 出现：antd 关闭抽屉时
                只是隐藏外壳，DOM 节点仍在。于是上一条报告的抽屉会被当成
                "新抽屉已打开"，标题读到的是旧内容或空串——本轮 U04 就是因此
                失败的，而实际切换行为正常。要求"可见 + 标题变化"才是真的
                证明了抽屉为新报告重新渲染过。
                """
                deadline = time.time() + timeout_ms / 1000
                while time.time() < deadline:
                    loc = page.locator(DRAWER)
                    visible = False
                    if await loc.count():
                        try:
                            visible = await loc.first.is_visible()
                        except Exception:  # noqa: BLE001 - 元素在动画中被替换
                            visible = False
                    if visible:
                        title = await current_title()
                        if title and title != expected_title_from and title != "报告详情":
                            return True
                    await page.wait_for_timeout(120)
                return False

            async def rewind_to_first_page() -> None:
                """把报告表退回第 1 页。

                为什么必须做：`open_report` 只能往前翻页。**先打开 A（在第 N 页）
                再打开 B（在第 1 页）**时，表格停在最后一页，next 已禁用，
                于是 B 永远定位不到——失败信息看起来像"切换报告坏了"，
                实际是遍历方向的问题。本轮 U04 就是这样失败的。
                """
                for _ in range(12):
                    prev = page.locator(".ant-pagination-prev button")
                    if await prev.count() and await prev.first.is_enabled():
                        await prev.first.click()
                        await page.wait_for_timeout(400)
                        continue
                    break

            async def open_report(rid: str) -> bool:
                # Reports.vue 无 testid：用 antd 表格行 data-row-key + 可访问名「查看」链接。
                row_sel = f'.ant-table-row[data-row-key="{rid}"]'
                previous = await current_title()
                await rewind_to_first_page()
                for _ in range(8):
                    loc = page.locator(row_sel)
                    if await loc.count():
                        await loc.first.scroll_into_view_if_needed()
                        # `<a @click>` 没有 href，浏览器不会给它 link 角色，
                        # `get_by_role("link")` 因此匹配不到。改用文本定位。
                        link = loc.first.locator("a", has_text="查看")
                        target = link if await link.count() else loc.first
                        await target.first.click()
                        return await wait_drawer(previous)
                    nxt = page.locator(".ant-pagination-next button")
                    if await nxt.count() and await nxt.first.is_enabled():
                        await nxt.first.click()
                        await page.wait_for_timeout(700)
                        continue
                    return False
                return False

            ok_a = await open_report(report_a)
            add("打开报告 A", "pass" if ok_a else "fail",
                f"{report_a}（定位方式：ant-table-row[data-row-key] + 「查看」链接）")
            title_a = await current_title()
            # 不等待 A 的响应：立刻关闭抽屉并切到 B
            close = page.locator(DRAWER_CLOSE)
            if await close.count():
                await close.first.click()
            else:
                await page.keyboard.press("Escape")
            await page.wait_for_timeout(200)
            ok_b = await open_report(report_b)

            # 等 B 的**详情**到达再读标题。
            #
            # Reports.vue 的抽屉标题是 `detail?.title || '报告详情'`：详情请求
            # 未完成时读到的是兜底串。此前这里同步读一次就断言，于是结果取决于
            # B 的详情请求有多快——本轮就因为读到 '报告详情' 而失败，而实际
            # 行为（切到 B、A 的迟到响应不串图）完全正常。
            # 这不是放宽标准：U04 要证明的是"A 的迟到响应不污染 B"，
            # 不是"B 加载得多快"。
            title_b = await current_title()
            add("在 A 的响应到达前切到报告 B", "pass" if ok_b else "fail",
                f"{report_b}，抽屉标题='{title_b}'")
            # 抽屉真的换成了 B：标题与 A 不同，且不是兜底串。
            # open_report 已经保证了这一点；这里单独记一条，是为了让失败时
            # 一眼能看出"是抽屉没换"还是"是串图"。
            add("抽屉已从 A 换成 B（标题变化且非兜底串）",
                "pass" if (title_b and title_b != title_a and title_b != "报告详情") else "fail",
                f"A='{title_a}' B='{title_b}'")
            # 等 A 的迟到响应真正到达
            await asyncio.sleep(delay_seconds + 1.5)
            # 再给 B 自己的证据请求一点时间渲染（有界）。不等的话，"卡片来自
            # B 的数据"只会在 B 恰好很快时才有意义，否则退化成 skip——
            # 而 skip 不算通过，也不该因为加载顺序而出现。
            card_deadline = time.time() + 15
            while time.time() < card_deadline:
                count = await page.locator('[data-testid="evidence-card"]').count()
                if count:
                    break
                await page.wait_for_timeout(150)
            await page.wait_for_timeout(300)

            ids_a = {f["id"] for f in frames_a} - {f["id"] for f in frames_b}
            dom_ids = await page.evaluate(
                """() => Array.from(document.querySelectorAll('[data-testid="evidence-card"]'))
                        .map(el => el.getAttribute('data-frame-id'))"""
            )
            dom_ids = {x for x in dom_ids if x}
            add("延迟响应到达后渲染了卡片", "pass" if dom_ids else "skip",
                f"DOM 卡片 {len(dom_ids)} 张")
            add("延迟的 A 响应未串入 B 的卡片",
                "pass" if not (dom_ids & ids_a) else "fail",
                f"串入={sorted(dom_ids & ids_a)[:5]}")
            expected_b = {f["id"] for f in frames_b}
            add("B 的卡片来自 B 的数据",
                "pass" if dom_ids and dom_ids <= expected_b else
                ("skip" if not dom_ids else "fail"),
                f"B 期望 {len(expected_b)} 张，DOM 命中 {len(dom_ids & expected_b)} 张")
            await page.screenshot(path=str(cfg.out_dir / f"{SCREENSHOT_PREFIX}u04-{cfg.mode}-1280x900.png"))
        finally:
            await context.close()
            await browser.close()
    return results


def case_u04(s: Session, res: CaseResult, cfg: Config, api: Api,
             report_a: str, frames_a: list[dict], report_b: str,
             frames_b: list[dict]) -> None:
    # 空执行守卫：函数体第一句就置位。函数被删空或分发处漏调用时，
    # finalize() 拿不到这个标记，结果只能是 incomplete，不会是 passed。
    res.executed = True
    if not report_a or not report_b or report_a == report_b:
        res.status = "skipped"
        res.reason = "需要两份带证据的报告才能验证「切 A 到 B」；当前只有一份可用"
        return
    if not frames_a or not frames_b:
        res.status = "skipped"
        res.reason = "缺少证据帧数据，无法比对是否串图"
        return
    # 检查卡片是否携带 data-frame-id（无此契约就无法做串图判定）
    if not s.count(SEL["card"]):
        res.status, res.missing = "skipped", ["card"]
        res.reason = "报告抽屉内没有卡片"
        return
    has_attr = s.page.locator(SEL["card"]).first.get_attribute("data-frame-id")
    if not has_attr:
        res.status, res.missing = "skipped", ["card[data-frame-id]"]
        res.reason = "卡片缺少 data-frame-id，无法判定是否串图"
        return

    box: dict[str, Any] = {}

    def runner():
        try:
            box["value"] = asyncio.run(_u04_async(
                cfg, s.auth, report_a, report_b, frames_a, frames_b, 3.0))
        except BaseException as exc:  # noqa: BLE001 - 交给上层报告
            box["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(timeout=180)
    if thread.is_alive():
        res.status = "error"
        res.reason = "U04 异步子运行超时（180s）"
        return
    if "error" in box:
        res.status = "error"
        res.reason = f"U04 异步子运行失败: {box['error']}"
        return
    for item in box.get("value", []):
        res.add(item["name"], item["status"], item["detail"])
    shot = cfg.out_dir / f"{SCREENSHOT_PREFIX}u04-{cfg.mode}-1280x900.png"
    if shot.exists():
        res.screenshots.append(str(shot))

    # ---- P1-1：历史版本切换必须真的做到"两个版本的详情与证据互相隔离" ----
    # 接口存在不等于 UI 能用。这里做的是**真实链路**验证：
    # 在 UI 上把版本切到另一个已发布版本，然后同时断言
    #   (a) 分数/摘要来自快照接口（title 或 climateScore 与 API 快照一致），
    #   (b) 证据画廊按新版本重新请求（卡片 data-frame-id 集合与 API 对应版本一致）。
    # 任一不满足即失败，而不是记 skipped / unimplemented。
    versions = api.report_versions(s.auth["token"], report_a)
    published = [v for v in versions if isinstance(v, dict)]
    if len(published) < 2:
        res.require(
            "历史版本切换（需要 >=2 个已发布版本）",
            f"报告 {report_a} 只有 {len(published)} 个已发布版本；"
            "无法验证版本隔离。这属于数据未就绪，不是能力缺失——"
            "但缺少这项证据就不能声称版本切换已验证")
    else:
        selector = s.page.locator(SEL["version_select"])
        if not selector.count():
            res.require("版本选择器（report-version-select）",
                        "Reports.vue 未渲染版本选择器")
        else:
            options = s.page.locator(SEL["version_option"])
            option_versions = [
                (options.nth(i).get_attribute("data-version") or "")
                for i in range(options.count())
            ]
            api_versions = [str(v.get("version")) for v in published]
            res.check("版本选项数量与 API 一致", len(option_versions) == len(api_versions),
                     f"UI={option_versions} API={api_versions}")
            res.check("版本选项覆盖 API 的全部版本号",
                     set(api_versions).issubset(set(option_versions)),
                     f"缺失={sorted(set(api_versions) - set(option_versions))}")

            current_text = s.page.locator(SEL["version_current"]).first.inner_text().strip()
            displayed = re.search(r"v(\d+)", current_text or "")
            displayed_version = int(displayed.group(1)) if displayed else None
            res.check("当前版本标签可读出正在显示的版本号",
                     displayed_version is not None, f"label={current_text!r}")

            # 选一个与当前不同、**且视频任务号不同**的版本。
            #
            # 只按版本号挑是不够的：同一课堂重新生成常常复用同一个 analysis job，
            # 于是两个版本的窗口/证据帧完全相同，"按版本取证据"的断言恒真
            # （独立复核 P0-1）。必须挑一个 job 也不同的版本，隔离才可区分。
            current_job = next((v.get("analysisVideoJobId") for v in published
                                if str(v.get("version")) == str(displayed_version)), None)
            candidates = [v for v in published
                          if str(v.get("version")) != str(displayed_version)]
            distinct = [v for v in candidates
                        if (v.get("analysisVideoJobId") or "") != (current_job or "")]
            if distinct:
                target = distinct[0]
            elif candidates:
                # 有别的版本，但 job 相同 —— 该夹具下证据隔离不可区分，记必检缺失。
                target = None
                res.require(
                    "可切换的目标版本（需要不同的 analysis job）",
                    f"报告 {report_a} 的 {len(published)} 个已发布版本共用同一个 "
                    f"analysisVideoJobId={current_job}；两版证据帧完全相同，版本隔离无法区分。"
                    "请先运行 scripts/verify/prepare_version_fixture.py --second-video 造一个不同 job 的版本")
            else:
                target = None
                res.require("可切换的目标版本", "没有与当前显示不同的版本可选")
            if target is not None:
                target_version = int(target["version"])
                snapshot = api.report_version_detail(s.auth["token"], report_a, target_version)
                # 当前显示版本（切换前）的快照，用于判断"两版是否真的可区分"。
                current_snapshot = (api.report_version_detail(
                    s.auth["token"], report_a, int(displayed_version))
                    if displayed_version is not None else {})
                snapshot_current_source = (current_snapshot.get("dataSource") or "").strip()
                res.check(f"快照接口可读 v{target_version}", bool(snapshot),
                         f"keys={sorted(snapshot)[:8] if snapshot else '空'}")

                selector.first.select_option(str(target_version))
                # 等待 loading 结束：组件渲染 report-version-loading 期间不判定。
                deadline = time.time() + 20
                while time.time() < deadline:
                    if not s.visible(SEL["version_loading"]):
                        break
                    s.page.wait_for_timeout(150)
                s.page.wait_for_timeout(600)

                new_label = s.page.locator(SEL["version_current"]).first.inner_text().strip()
                got = re.search(r"v(\d+)", new_label or "")
                res.check("切换后当前版本标签更新为目标版本",
                         got is not None and int(got.group(1)) == target_version,
                         f"label={new_label!r} 期望 v{target_version}")

                # 详情隔离：抽屉里的内容必须来自所选版本的快照。
                #
                # 只断言"分数相同"是不够的：同一课堂重新生成后，两个版本常常共享
                # 同一个 analysis job，分数与证据帧会完全一致，于是分数断言恒真、
                # 什么也没证明。真正能区分版本的是**发布时间**——
                # 每个快照都有各自的 publishedAt，且抽屉把它渲染成「发布时间」一行。
                drawer_text = s.text(DRAWER)
                snap_climate = snapshot.get("climateScore")
                if snap_climate is None:
                    res.check("快照无课堂氛围分时抽屉显示数据不足",
                             "该维度数据不足" in drawer_text,
                             f"drawer 片段={drawer_text[:120]}")
                else:
                    expected = f"{float(snap_climate):.1f}"
                    res.check("抽屉显示的课堂氛围分不等于所选版本快照时失败",
                             expected in drawer_text,
                             f"期望 {expected}，drawer 片段={drawer_text[:160]}")
                # ---- 版本隔离的真正判据：抽屉渲染的 detail.version ----
                # 「发布时间」不能当判据：同一课堂重新生成时两个版本可能只差 0.16 秒
                # （实测 v1=03:44:54.281787, v2=03:44:54.438336），而 UI 只渲染到分钟，
                # 两个版本都显示 03:44 —— 那条断言恒真，什么也证明不了。
                # 所以用组件暴露的 data-version（只绑 detail.version，不回落 selectedVersion）。
                marker = s.page.locator(SEL["detail_version"])
                if not marker.count():
                    res.require("抽屉暴露 report-detail-version",
                                "缺少 data-testid=report-detail-version；无法判定抽屉渲染的是哪一版")
                else:
                    rendered = (marker.first.get_attribute("data-version") or "").strip()
                    res.check("抽屉渲染的 detail.version 等于所选版本",
                             rendered == str(target_version),
                             f"data-version={rendered!r} 期望 {target_version}")
                    res.check("抽屉版本标记不是回落值（失败时不借用 target）",
                             rendered != "", "data-version 为空说明 detail 未加载，不能算通过")

                    rendered_source = ""
                    source_marker = s.page.locator(SEL["detail_source"])
                    if source_marker.count():
                        rendered_source = (source_marker.first.get_attribute("data-source") or "").strip()
                    expected_source = (snapshot.get("dataSource") or "").strip()
                    # dataSource 只在两版确实不同时才有区分力；相同时改为断言"不矛盾"，
                    # 避免把恒真断言写成隔离证据（独立复核 P1-3）。
                    if expected_source and expected_source != (snapshot_current_source or ""):
                        res.check("抽屉渲染的数据来源与所选版本快照一致（两版不同，可区分）",
                                 rendered_source == expected_source,
                                 f"UI={rendered_source!r} 快照={expected_source!r} "
                                 f"当前版={snapshot_current_source!r}")
                    else:
                        res.check("抽屉渲染的数据来源与所选版本快照不矛盾（两版相同，不足以区分）",
                                 rendered_source == expected_source,
                                 f"UI={rendered_source!r} 快照={expected_source!r}"
                                 "（两版 dataSource 相同，本条不构成隔离证据）")

                # 证据隔离：卡片集合必须与目标版本的 evidence API 一致
                status_codes, body = api.behavior_evidence(
                    s.auth["token"], report_a, version=target_version)
                version_frames = (body.get("data") or []) if status_codes == 200 else []
                if status_codes != 200:
                    res.check("目标版本证据接口可读", False, f"HTTP {status_codes}")
                else:
                    cards = s.page.locator(SEL["card"])
                    ui_ids = [
                        (cards.nth(i).get_attribute("data-frame-id") or "")
                        for i in range(min(cards.count(), 24))
                    ]
                    api_ids = [f.get("id") for f in version_frames][:24]
                    res.check("切换版本后证据卡片来自目标版本（无串图）",
                             bool(ui_ids) and set(ui_ids).issubset(set(api_ids)),
                             f"UI 前 3={ui_ids[:3]}，API 前 3={api_ids[:3]}")

                # 切回当前版本，验证反向也可用（避免只验证单向）
                if displayed_version is not None:
                    selector.first.select_option(str(displayed_version))
                    deadline = time.time() + 20
                    while time.time() < deadline:
                        if not s.visible(SEL["version_loading"]):
                            break
                        s.page.wait_for_timeout(150)
                    s.page.wait_for_timeout(600)
                    back_label = s.page.locator(SEL["version_current"]).first.inner_text().strip()
                    res.check("可切回原版本（双向切换）",
                             f"v{displayed_version}" in back_label,
                             f"label={back_label!r}")

    # 统一走 finalize()：手写 `else "passed"` 会绕过「至少一条通过断言」与
    # 「executed 标记」两道守卫，让零断言用例也能写成 passed。
    res.finalize()


def case_u08(s: Session, res: CaseResult, cfg: Config, frames: list[dict],
             restore_object, frame_id: str = "") -> None:
    """真实对象删除（不是浏览器层注入）：UI 必须报错、可手动重试、不无限自动请求。

    与 U03 的区别（两者都要保留）：
      * U03 用 page.route 返回 403，验证的是"签名失效/网络层失败"的前端路径；
      * U08 由操作者**真的删除** MinIO 里的对象，服务端签名 URL 返回 404，
        验证的是"对象真的不在了"这一运维场景。删除动作由调用方注入的
        ``restore_object`` 负责恢复，保证夹具可重复使用。
    """
    # 空执行守卫：函数体第一句就置位。函数被删空或分发处漏调用时，
    # finalize() 拿不到这个标记，结果只能是 incomplete，不会是 passed。
    res.executed = True

    required = ["gallery", "card", "refresh", "retry"]
    missing = s.missing(["gallery", "card", "refresh"])
    if missing:
        res.status, res.missing = "skipped", missing
        res.reason = f"缺少 data-testid：{', '.join(missing)}"
        res.finalize()
        return

    signed = [f for f in frames if (f.get("url") or "").startswith("http")]
    if not signed:
        res.require("存在真实签名图片 URL",
                    "当前报告没有签名图片 URL（需要 --mode real + 真实 MinIO）")
        res.finalize()
        return

    target = next((f for f in signed if f.get("id") == frame_id), None)
    if target is None:
        target = signed[0]
    url = target["url"]
    parsed = urlsplit(url)
    parts = parsed.path.lstrip("/").split("/", 1)
    if len(parts) != 2:
        res.require("签名 URL 可解析出 bucket/object", f"path={parsed.path[:80]}")
        res.finalize()
        return
    bucket, object_key = parts[0], parts[1]

    removed = restore_object(bucket, object_key, delete=True)
    if not removed.get("ok"):
        res.require("在隔离 MinIO 中真实删除对象",
                    f"删除失败：{removed.get('error')}（拒绝在非隔离环境执行破坏性操作）")
        res.finalize()
        return
    res.add("真实删除对象", "pass",
            f"bucket={bucket} object=…{object_key[-40:]}（隔离 MinIO，测试后恢复）")

    try:
        # 【必须禁用 HTTP 缓存】否则浏览器可能直接用缓存的 200 响应，
        # 对象明明已删除也不会触发 error —— 本轮实测因此出现过一次假阴性。
        cache_disabled = False
        try:
            cdp = s.page.context.new_cdp_session(s.page)
            cdp.send("Network.enable")
            cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})
            cache_disabled = True
        except Exception as exc:  # noqa: BLE001
            res.add("禁用浏览器缓存", "skip", f"CDP 不可用：{exc}")
        if cache_disabled:
            res.add("禁用浏览器缓存", "pass", "CDP Network.setCacheDisabled(True)")

        # 用刷新按钮重新取签名 URL 并重新请求图片（对象已不存在）。
        before_requests = {"n": 0, "signed": 0}

        def count_requests(route):
            url = route.request.url
            before_requests["n"] += 1
            if "X-Amz-Signature" in url or ":19100/" in url:
                before_requests["signed"] += 1
            route.continue_()

        s.page.route("**/*", count_requests)

        # 刷新按钮偶发不触发请求（按钮忙 / 组件重渲染），一次点击就断言等于
        # 把环境抖动当成功能缺陷。最多点 3 次，直到确认浏览器真的发出签名请求。
        clicks = 0
        for _ in range(3):
            s.page.locator(SEL["refresh"]).first.click()
            clicks += 1
            waited_for_request = 0
            while waited_for_request < 8_000 and before_requests["signed"] == 0:
                s.page.wait_for_timeout(250)
                waited_for_request += 250
            if before_requests["signed"] > 0:
                break

        saw_error = False
        waited = 0
        while waited < 20_000:
            if s.count(SEL["error"]) or s.count(SEL["retry"]):
                saw_error = True
                break
            s.page.wait_for_timeout(500)
            waited += 500
        res.add("刷新触发真实请求", "pass" if before_requests["signed"] > 0 else "fail",
                f"点击 {clicks} 次，签名请求={before_requests['signed']}")
        res.check("对象真实缺失时给出明确错误与重试入口", saw_error,
                  f"error={s.count(SEL['error'])}, retry={s.count(SEL['retry'])}, "
                  f"waited={waited}ms, 签名请求={before_requests['signed']}, "
                  f"缓存已禁用={cache_disabled}")
        # 若没出现错误，至少必须证明浏览器的确重新请求了对象（不是空转）。
        if not saw_error:
            res.check("失败时确认浏览器确实重新请求了签名对象（排除缓存假阴性）",
                      before_requests["signed"] > 0,
                      f"签名请求={before_requests['signed']}")

        # 静置 4 秒：不得出现自动重试风暴（每个对象自发请求 <= 1）。
        n_before = before_requests["n"]
        s.page.wait_for_timeout(4000)
        auto = before_requests["n"] - n_before
        res.check("无无限自动重试（静置 4s 内新增请求 <= 1）", auto <= 1,
                  f"新增请求={auto}")

        # 手动重试仍应失败（对象确实不存在），且不得让页面白屏/卡片消失。
        override_retry = s.page.locator(SEL["retry"])
        if override_retry.count():
            override_retry.first.click()
            s.page.wait_for_timeout(2000)
            res.check("对象仍缺失时重试不误报成功",
                      s.count(SEL["error"]) > 0 or s.count(SEL["retry"]) > 0,
                      f"error={s.count(SEL['error'])}, retry={s.count(SEL['retry'])}")
        else:
            res.require("对象缺失时出现可点击的重试入口", "未出现 evidence-retry")
    finally:
        restored = restore_object(bucket, object_key, delete=False)
        res.check("测试后对象已恢复（夹具可重复使用）", bool(restored.get("ok")),
                  str(restored.get("error") or "restored"))
        try:
            s.page.unroute("**/*")
        except Exception:  # noqa: BLE001
            pass

    shot = s.screenshot("u08", res.mode, "1280x900")
    if shot:
        res.screenshots.append(shot)
    res.finalize()


def case_u07(s: Session, res: CaseResult, cfg: Config, api: Api,
             token: str, pending_report_id: str) -> None:
    """R11：报告"已选任务但窗口未生成"必须是可解释的数据不足，不能显示旧窗口分数。

    真实链路断言（不注入任何假数据）：
      1. 打开的这份报告在 API 层 windowState == windows_pending；
      2. 抽屉里出现数据不足提示（report-window-pending）；
      3. 状态标签显示"窗口未生成"（report-window-state）；
      4. 三个维度都显示"该维度数据不足"，没有渲染出任何分数；
      5. 证据面板的窗口数为 0（不得把旧行为观测行数当成窗口数）。
    """
    # 空执行守卫：函数体第一句就置位。函数被删空或分发处漏调用时，
    # finalize() 拿不到这个标记，结果只能是 incomplete，不会是 passed。
    res.executed = True

    if not pending_report_id:
        res.require("找到 windowState=windows_pending 的报告",
                    "当前环境没有处于 windows_pending 的报告，无法验证数据不足状态")
        res.finalize()
        return

    detail = api.report_detail(token, pending_report_id)
    state = (detail.get("scoreValidation") or {}).get("windowState")
    res.check("API 报告状态为 windows_pending", state == "windows_pending",
              f"report={pending_report_id} windowState={state!r}")
    res.check("数据不足时三个维度分均为 null",
              detail.get("climateScore") is None
              and detail.get("engagementScore") is None
              and detail.get("teachingScore") is None,
              f"climate={detail.get('climateScore')}, "
              f"engagement={detail.get('engagementScore')}, "
              f"teaching={detail.get('teachingScore')}")
    res.check("证据面板窗口数为 0（不把旧行为观测算作窗口）",
              (detail.get("evidence") or {}).get("windowCount") == 0,
              f"windowCount={(detail.get('evidence') or {}).get('windowCount')}")

    if not s.open_report(pending_report_id):
        res.require("打开 windowState=windows_pending 的报告", "报告列表定位失败")
        res.finalize()
        return

    # 抽屉打开 ≠ 详情已加载：详情是异步 GET /reports/{id}，必须等它渲染完成
    # （否则选择器探测会早于 DOM 而误判为"组件未实现"——本轮实测踩过）。
    ready = False
    deadline = time.time() + 20
    while time.time() < deadline:
        if (s.count(SEL["window_state"]) > 0
                or s.count(SEL["window_pending"]) > 0):
            ready = True
            break
        body_now = s.page.locator(DRAWER).inner_text() if s.count(DRAWER) else ""
        if "该维度数据不足" in body_now:
            ready = True
            break
        s.page.wait_for_timeout(300)
    if not ready:
        body_now = s.page.locator(DRAWER).inner_text() if s.count(DRAWER) else ""
        res.check("抽屉详情加载完成（出现数据不足状态）", False,
                  f"等待 20s 后仍无 report-window-* 节点；抽屉文本片段="
                  f"{body_now[:200]!r}")
        res.finalize()
        return

    pending = s.page.locator(SEL["window_pending"])
    res.check("抽屉内出现「窗口未生成/数据不足」提示",
              pending.count() > 0 and pending.first.is_visible(),
              f"count={pending.count()}")

    state_tag = s.page.locator(SEL["window_state"])
    tag_text = state_tag.first.inner_text() if state_tag.count() else ""
    res.check("状态标签显示「窗口未生成」", "窗口未生成" in tag_text, f"tag='{tag_text}'")

    body_text = s.page.locator(DRAWER).inner_text()
    res.check("三个维度都渲染为「该维度数据不足」",
              body_text.count("该维度数据不足") == 3,
              f"出现 {body_text.count('该维度数据不足')} 次")
    res.check("摘要写明不会用历史版本窗口分数代替",
              "历史版本" in body_text or "数据不足" in body_text,
              "摘要需解释数据不足的成因")

    shot = s.screenshot("u07", res.mode, "1280x900")
    if shot:
        res.screenshots.append(shot)
    res.finalize()


def case_u06(s: Session, res: CaseResult, cfg: Config, frames: list[dict]) -> None:
    """在已按视口打开的 Session 上做布局与键盘断言。"""
    # 空执行守卫：函数体第一句就置位。函数被删空或分发处漏调用时，
    # finalize() 拿不到这个标记，结果只能是 incomplete，不会是 passed。
    res.executed = True
    required = ["gallery", "card", "card_media"]
    missing = s.missing(required)
    if missing:
        res.status, res.missing = "skipped", missing
        res.reason = f"缺少 data-testid：{', '.join(missing)}"
        return

    s.wait_count(SEL["card"], lambda c: c > 0, timeout=15)
    res.check("画廊在当前视口可见", s.count(SEL["gallery"]) > 0)
    box = s.page.locator(SEL["gallery"]).first.bounding_box()
    width = int(s.viewport.split("x")[0])
    res.check("画廊宽度不超出视口（无横向溢出）",
             bool(box) and box["width"] <= width + 2,
             f"gallery 宽度={box and round(box['width'], 1)}, 视口={width}")

    nav_count = s.page.locator(".ant-menu, .ant-layout-sider, [data-testid='app-nav']").count()
    res.check("导航在当前视口存在", nav_count > 0,
             f"命中 {nav_count} 个导航容器（桌面为侧栏，窄屏为抽屉）")

    if s.count(SEL["next"]):
        res.check("分页控件在当前视口可点击",
                 s.page.locator(SEL["next"]).first.is_visible(),
                 "next（data-testid=evidence-next）可见")
        # 390/768 下分页实际可用：点下一页后页码状态前进，再退回第 1 页。
        status_before = _ints(s.text("page_indicator"))
        nxt = s.page.locator(SEL["next"]).first
        if nxt.is_enabled():
            nxt.click()
            s.page.wait_for_timeout(400)
            status_after = _ints(s.text("page_indicator"))
            res.check("当前视口下分页可操作（页码前进）",
                     bool(status_before) and bool(status_after)
                     and status_after[0] == status_before[0] + 1,
                     f"before={status_before}, after={status_after}")
            prev = s.page.locator(SEL["prev"]).first
            if prev.count() and prev.is_enabled():
                prev.click()
                s.page.wait_for_timeout(300)
        else:
            res.skip("当前视口下分页可操作（页码前进）", "只有 1 页，下一页按钮禁用")
    else:
        res.skip("分页控件在当前视口可点击", "缺少 evidence-next")

    if s.count(SEL["prev"]):
        res.check("上一页控件可点击", s.page.locator(SEL["prev"]).first.is_visible())

    # 键盘：聚焦卡片 → Enter 打开 → Esc 关闭 → 焦点回到触发卡片
    media = s.page.locator(SEL["card_media"]).first
    frame_id = media.get_attribute("data-frame-id") or ""
    window_index = media.get_attribute("data-window-index")
    res.check("媒体按钮带 data-frame-id / data-window-index",
             bool(frame_id) and window_index is not None,
             f"data-frame-id={frame_id!r}, data-window-index={window_index!r}")
    media.focus()
    focused = s.page.evaluate("() => document.activeElement?.getAttribute('data-testid')")
    res.check("卡片可通过键盘聚焦", focused == "evidence-card-media", f"activeElement={focused}")
    s.page.keyboard.press("Enter")
    opened = False
    for _ in range(60):
        if s.visible(SEL["preview"]):
            opened = True
            break
        s.page.wait_for_timeout(100)
    res.check("Enter 打开预览", opened, f"frame_id={frame_id}")
    if opened:
        # antd Dialog 在打开动画期间忽略 keydown（Esc 由 dialog 的 keydown 处理器处理）。
        # 等过渡结束再按键，否则断言测到的是动画竞态而不是组件行为。
        s.page.wait_for_timeout(600)
        s.page.keyboard.press("Escape")
        closed = False
        for _ in range(60):
            if not s.visible(SEL["preview"]):
                closed = True
                break
            s.page.wait_for_timeout(100)
        res.check("Esc 关闭预览", closed)
        restored = s.page.evaluate(
            """() => {
                const el = document.activeElement;
                if (!el) return null;
                const card = el.closest('[data-testid="evidence-card"]');
                return card ? card.getAttribute('data-frame-id') : null;
            }"""
        )
        res.check("关闭后焦点回到触发卡片", bool(frame_id) and restored == frame_id,
                 f"frame_id={frame_id}, activeElement 所属卡片={restored}")

    shot = s.screenshot("u06", res.mode, s.viewport.replace("x", "x"))
    if shot:
        res.screenshots.append(shot)
    # 统一走 finalize()：手写 `else "passed"` 会绕过「至少一条通过断言」与
    # 「executed 标记」两道守卫，让零断言用例也能写成 passed。
    res.finalize()


# ---------------------------------------------------------------------------
# 运行编排
# ---------------------------------------------------------------------------
def _environment_fingerprint() -> dict:
    """验收环境的机器可读描述（任务书要求 JSON 写明"环境"）。

    隔离 rig 把它的目标写进 data/tmp/ui-rig-*/rig.json；读它而不是让操作者
    手填，避免"报告写的环境"和"实际跑的环境"不一致。同时探测各端口是否真在
    监听——容器在跑不等于服务在跑。
    """
    import socket
    env: dict[str, Any] = {"rigStateFile": None, "targets": None, "listening": {}}
    for state in sorted((REPO_ROOT / "data" / "tmp").glob("ui-rig-*/rig.json")):
        try:
            env["rigStateFile"] = str(state.relative_to(REPO_ROOT).as_posix())
            env["targets"] = json.loads(state.read_text(encoding="utf-8")).get("targets")
            break
        except Exception:  # noqa: BLE001
            continue
    ports = {"backend": 8000, "ai": 8001, "frontend": 5173}
    if isinstance(env["targets"], dict):
        ports.update({k: v for k, v in env["targets"].items()
                      if k in ("backend", "ai", "frontend") and isinstance(v, int)})
    for name, port in ports.items():
        sock = socket.socket()
        sock.settimeout(0.5)
        try:
            env["listening"][name] = sock.connect_ex(("127.0.0.1", port)) == 0
        except OSError:
            env["listening"][name] = False
        finally:
            sock.close()
    env["playwrightBrowsersPath"] = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    # 记录**解析后**的 Chromium 可执行文件与 Playwright 版本，而不是只记环境变量：
    # 环境变量未设时它是空串，而空串既不能证明"用的是 E 盘那份 Chromium"，
    # 也不能证明用的是哪一版。证据要能回答"这份 JSON 是哪套浏览器跑出来的"。
    env["chromium"] = _chromium_fingerprint()
    return env


def _chromium_fingerprint() -> dict[str, Any]:
    """当前进程实际会启动的 Chromium：路径 + 是否存在 + Playwright 版本。"""
    info: dict[str, Any] = {"executablePath": "", "exists": False,
                            "playwrightVersion": _playwright_version()}
    if sync_playwright is None:
        info["error"] = "playwright 未安装"
        return info
    try:
        with sync_playwright() as pw:  # type: ignore[misc]
            path = pw.chromium.executable_path
        info["executablePath"] = str(path or "")
        info["exists"] = bool(path) and Path(str(path)).exists()
    except Exception as exc:  # noqa: BLE001
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def preflight(cfg: Config, log: Log) -> tuple[Api, dict]:
    """登录 + 连通性。失败抛 RuntimeError，由 main 归为退出码 3。"""
    if requests is None:
        raise RuntimeError("缺少 requests 依赖")
    api = Api(cfg.api_url)
    try:
        resp = requests.get(cfg.base_url, timeout=10)
        log(f"base-url 可达：HTTP {resp.status_code} {cfg.base_url}")
    except Exception as exc:
        raise RuntimeError(f"base-url 不可达：{cfg.base_url} ({exc})") from exc
    if not cfg.teacher_user or not cfg.teacher_pass:
        raise RuntimeError(
            "未提供教师账号：设置 --teacher-user/--teacher-pass 或环境变量 "
            "MACT_E2E_TEACHER_USER / MACT_E2E_TEACHER_PASS")
    auth = api.login(cfg.teacher_user, cfg.teacher_pass)
    log(f"登录成功：userId={auth.get('userId')} role={auth.get('role')}")
    return api, auth


def install_fixture_route(page, frames: list[dict]) -> None:
    def handler(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"code": 200, "message": "success", "data": frames},
                            ensure_ascii=False),
        )

    page.route("**/api/reports/*/behavior-evidence*", handler)


def run_cases(cfg: Config, log: Log) -> int:
    started = time.time()
    # 源码指纹必须在**运行开始前**采样。
    # 为什么：本脚本会把 JSON 与截图写进 docs/reviews/evidence/，而那个目录是纳入
    # 版本控制的。若在 write_report() 里才采样，工作区已包含刚产出的证据 →
    # workingTreeDirty 恒为 true，这条字段就失去意义（它本意是记录"验收对应哪版源码"）。
    # 实测：在 401 文件全部提交、git status 完全干净之后跑，JSON 里仍是 dirty=true。
    pre_run_fingerprint = _source_fingerprint()
    log(f"源码指纹（运行前）：head={str(pre_run_fingerprint.get('head'))[:12]} "
        f"dirty={pre_run_fingerprint.get('workingTreeDirty')}")
    results: list[CaseResult] = []
    meta: dict[str, Any] = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": cfg.mode,
        "base_url": cfg.base_url,
        "api_url": cfg.api_url,
        "cases_requested": cfg.cases,
        "fixture_cards": cfg.fixture_cards if cfg.mode == "fixture" else None,
        "page_size": cfg.page_size,
        "viewports": cfg.viewports,
        "login_mode": cfg.login_mode,
        "environment": _environment_fingerprint(),
        "environment_error": None,
        # 运行前采样的源码指纹（见上方注释）。write_report 用它，不再重采。
        "source": pre_run_fingerprint,
    }
    try:
        api, auth = preflight(cfg, log)
    except Exception as exc:
        meta["environment_error"] = str(exc)
        log(f"环境前置失败：{exc}")
        write_report(cfg, meta, results, started, log)
        return 3

    report_a, frames_a = "", []
    report_b, frames_b = "", []
    if cfg.mode == "fixture":
        if not cfg.report_id:
            report_a, frames_a = discover_report(api, auth["token"], log)
        else:
            report_a = cfg.report_id
            status, body = api.behavior_evidence(auth["token"], report_a)
            frames_a = (body.get("data") or []) if status == 200 and isinstance(body, dict) else []
    else:
        if cfg.report_id:
            report_a = cfg.report_id
            status, body = api.behavior_evidence(auth["token"], report_a)
            frames_a = (body.get("data") or []) if status == 200 and isinstance(body, dict) else []
        else:
            report_a, frames_a = discover_report(api, auth["token"], log)
        for rid, report in ((r.get("id"), r) for r in api.list_reports(auth["token"])):
            if rid and rid != report_a and report.get("generated", True):
                status, body = api.behavior_evidence(auth["token"], rid)
                cand = (body.get("data") or []) if status == 200 and isinstance(body, dict) else []
                if cand:
                    report_b, frames_b = rid, cand
                    break
    # R11：找一份"已选任务但窗口未生成"的报告，供 U07 验证数据不足状态。
    pending_report = ""
    for rid, report in ((r.get("id"), r) for r in api.list_reports(auth["token"])):
        if not rid or rid == report_a:
            continue
        if (api.report_detail(auth["token"], rid).get("scoreValidation") or {}
                ).get("windowState") == "windows_pending":
            pending_report = rid
            break
    if pending_report:
        log(f"U07 目标报告：{pending_report}（windowState=windows_pending）")
    else:
        log("U07 目标报告：未找到 windows_pending 报告（U07 将记为覆盖不完整）")

    log(f"目标报告：A={report_a or '(无)'} 证据 {len(frames_a)} 条；"
        f"B={report_b or '(无)'} 证据 {len(frames_b)} 条")
    meta.update({"report_id": report_a, "frames": len(frames_a),
                 "report_title": api.report_title(auth["token"], report_a) if report_a else "",
                 "report_id_b": report_b, "frames_b": len(frames_b)})

    desktop = ["u01", "u02", "u03", "u04"]
    needs_desktop = [c for c in cfg.cases if c in desktop]
    needs_u06 = "u06" in cfg.cases

    # 桌面会话：U01–U04
    if needs_desktop:
        try:
            with Session(cfg, auth, log, viewport="1280x900") as s:
                if cfg.mode == "fixture":
                    install_fixture_route(s.page, build_fixture_frames(cfg.fixture_cards, report_a or "fixture"))
                    frames_view = build_fixture_frames(cfg.fixture_cards, report_a or "fixture")
                else:
                    frames_view = frames_a

                opened = s.open_report(report_a) if report_a else False
                log(f"打开报告抽屉：{'成功' if opened else '失败'}，路径={s.open_report_via or '未找到'}")
                meta["report_open_via"] = s.open_report_via
                if not opened:
                    log("无法打开报告详情抽屉，桌面用例将报告缺少前置条件")
                if not s.wait_gallery(timeout_ms=15_000):
                    log("画廊未渲染")
                else:
                    s.wait_count(SEL["card"], lambda c: c > 0, timeout=20)

                for case in needs_desktop:
                    res = CaseResult(case=case, mode=cfg.mode, viewport="1280x900")
                    try:
                        if not opened or not report_a:
                            res.status = "skipped"
                            res.reason = ("未能打开报告详情抽屉（无报告，或表格行/「查看」链接定位失败；"
                                          "Reports.vue 尚无 report-open testid）")
                            res.missing = ["report_open（待新增，不在本轮范围）"] if not opened else []
                        elif case == "u01":
                            if cfg.mode != "fixture" and not frames_view:
                                res.status = "skipped"
                                res.reason = "当前报告没有证据帧"
                            else:
                                case_u01(s, res, cfg, frames_view, cfg.mode == "fixture")
                        elif case == "u02":
                            if not frames_view:
                                res.status = "skipped"
                                res.reason = "当前报告没有证据帧"
                            else:
                                case_u02(s, res, cfg, frames_view, cfg.mode == "fixture")
                        elif case == "u03":
                            if not frames_view:
                                res.status = "skipped"
                                res.reason = "当前报告没有证据帧"
                            else:
                                case_u03(s, res, cfg, frames_view, cfg.mode == "fixture")
                        elif case == "u04":
                            if cfg.mode == "fixture":
                                res.status = "skipped"
                                res.reason = "fixture 模式只用于 U01 的 DOM 压力，不覆盖切换竞态"
                            else:
                                case_u04(s, res, cfg, api, report_a, frames_a, report_b, frames_b)
                    except Exception as exc:  # noqa: BLE001
                        res.status = "error"
                        res.reason = f"{type(exc).__name__}: {exc}"
                        log(f"{case} 异常：{traceback.format_exc(limit=3)}")
                    results.append(res)
                    log(f"{case} → {res.status} {res.reason}".strip())
        except Exception as exc:  # noqa: BLE001
            log(f"桌面会话启动失败：{exc}")
            for case in needs_desktop:
                res = CaseResult(case=case, mode=cfg.mode, viewport="1280x900",
                                 status="error", reason=f"浏览器会话失败: {exc}")
                results.append(res)

    # U05 不依赖浏览器页面，但仍需要已登录会话的 token（无则跳过正对照）
    if "u05" in cfg.cases:
        res = CaseResult(case="u05", mode=cfg.mode, viewport="-")
        try:
            class _AuthOnly:
                def __init__(self, auth_dict): self.auth = auth_dict
            case_u05(_AuthOnly(auth), res, cfg, api, report_a, frames_a)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001
            res.status = "error"
            res.reason = f"{type(exc).__name__}: {exc}"
        results.append(res)
        log(f"u05 → {res.status} {res.reason}".strip())

    # U07：数据不足 / 窗口未完成状态
    if "u07" in cfg.cases:
        res = CaseResult(case="u07", mode=cfg.mode, viewport="1280x900")
        try:
            with Session(cfg, auth, log, viewport="1280x900") as s:
                case_u07(s, res, cfg, api, auth["token"], pending_report)
        except Exception as exc:  # noqa: BLE001
            res.status = "error"
            res.reason = f"{type(exc).__name__}: {exc}"
        results.append(res)
        log(f"u07 → {res.status} {res.reason}".strip())

    # U08：真实删除对象（隔离 MinIO），验证错误提示 + 手动重试 + 无自动重试风暴
    if "u08" in cfg.cases:
        res = CaseResult(case="u08", mode=cfg.mode, viewport="1280x900")
        try:
            with Session(cfg, auth, log, viewport="1280x900") as s:
                if not report_a:
                    res.require("打开报告 A", "没有可用报告")
                elif not s.open_report(report_a):
                    res.require("打开报告 A", "报告列表定位失败")
                elif not s.wait_gallery(timeout_ms=15_000):
                    res.require("证据画廊渲染", "未渲染")
                else:
                    # 画廊挂载 ≠ 卡片渲染完成（图片是懒加载的）。必须先等卡片出现，
                    # 否则读取 data-frame-id 会拿到空串，U08 记成"必检未执行"。
                    s.wait_count(SEL["card"], lambda c: c > 0, timeout=20)
                    # 取页面**已渲染**的第一张卡片，而不是 API 列表的第一条：
                    # 删掉一个没被渲染的帧永远不会触发请求，断言就变成空转。
                    rendered = s.page.locator(SEL["card"]).first
                    frame_id = (rendered.get_attribute("data-frame-id") or "").strip() \
                        if rendered.count() else ""
                    if not frame_id:
                        res.require("证据卡片带 data-frame-id",
                                    "无法确定要删除的对象，U08 不能空转")
                    else:
                        evidence_url = api.evidence_frame_url(
                            auth["token"], report_a, frame_id)
                        res.add("删除目标帧", "pass", f"frame={frame_id}（当前页已渲染）")
                        case_u08(s, res, cfg, frames_a, make_restore_object(evidence_url), frame_id)
        except Exception as exc:  # noqa: BLE001
            res.status = "error"
            res.reason = f"{type(exc).__name__}: {exc}"
        res.finalize()
        results.append(res)
        log(f"u08 → {res.status} {res.reason}".strip())

    # U06 每个视口一个会话
    if needs_u06:
        for viewport in cfg.viewports:
            vp = viewport if "x" in viewport else f"{viewport}x900"
            res = CaseResult(case="u06", mode=cfg.mode, viewport=vp)
            try:
                with Session(cfg, auth, log, viewport=vp) as s:
                    if cfg.mode == "fixture":
                        install_fixture_route(s.page, build_fixture_frames(cfg.fixture_cards, report_a or "fixture"))
                    if not report_a or not s.open_report(report_a):
                        res.status = "skipped"
                        res.reason = "未能打开报告详情抽屉（表格行/「查看」链接定位失败）"
                        res.missing = ["report_open（待新增，不在本轮范围）"]
                    elif not s.wait_gallery(timeout_ms=15_000):
                        res.status = "skipped"
                        res.reason = "画廊未渲染"
                        res.missing = [SEL["gallery"]]
                    else:
                        s.wait_count(SEL["card"], lambda c: c > 0, timeout=20)
                        case_u06(s, res, cfg, frames_a)
            except Exception as exc:  # noqa: BLE001
                res.status = "error"
                res.reason = f"{type(exc).__name__}: {exc}"
            results.append(res)
            log(f"u06[{vp}] → {res.status} {res.reason}".strip())

    # 请求了但没产出结果的用例是**不可见**的：只要 --case 里点名的用例没有
    # 出现在 results 里（分发代码被改坏、分支被删、异常在 append 之前抛出），
    # 汇总就只统计"产出的那几个"，coverage 仍可能是 complete，退出码仍是 0。
    # 这里显式补一个 incomplete 结果，让缺失本身可见并让退出码非零。
    produced = {r.case for r in results}
    for case in cfg.cases:
        if case in produced:
            continue
        missing = CaseResult(case=case, mode=cfg.mode, viewport="-")
        missing.require("用例产出结果", f"--case {case} 已请求但没有任何 CaseResult 产出"
                                        "（分发分支缺失或异常早于 results.append）")
        missing.finalize()
        results.append(missing)
        log(f"{case} → 未产出结果（已记为 incomplete，退出码非零）")

    write_report(cfg, meta, results, started, log)
    return exit_code(results)


def exit_code(results: list[CaseResult]) -> int:
    """0 = 选中的用例全部执行并通过；1 = 有失败/异常；2 = 覆盖不完整。

    覆盖不完整包括三种情况，都不能折算成通过：
    * 整个用例被跳过（缺选择器 / 缺真实数据 / 打不开报告抽屉）；
    * 用例内**必检子项**未执行（``requiredSkips`` 非空 → 用例标 incomplete）；
    * 一个结果都没有。

    可选子项（组件未实现的缩放控件、版本切换 UI 等，``optionalSkips``）不影响退出码：
    它们已在文档里明确列为未实现，不属于本轮验收目标。
    """
    if any(r.status in ("failed", "error") for r in results):
        return 1
    if not results:
        return 2
    # 不信任 status 字面量：一条结果自称 passed 但从未执行、或一条通过断言都没有，
    # 必须按覆盖不完整处理。finalize() 已经拦过一遍，这里是第二道——
    # 因为 status 是可以被任何代码路径直接赋值的普通字符串字段。
    #
    # running 是未决哨兵：仍是 running 的结果同样算覆盖不完整（绝不能算通过）。
    if any(r.status in ("skipped", "incomplete", "running") or r.required_skips
           or not r.executed
           or not any(a["status"] == "pass" for a in r.assertions)
           for r in results):
        return 2
    return 0


def summarize_results(results: list[CaseResult]) -> dict[str, Any]:
    """汇总里必须能直接读出「哪些必检子项没执行」，不能只看 passed 计数。"""
    required_skips = [
        {"case": r.case, "viewport": r.viewport, **item}
        for r in results for item in r.required_skips
    ]
    optional_skips = [
        {"case": r.case, "viewport": r.viewport, **item}
        for r in results for item in r.optional_skips
    ]
    return {
        "passed": sum(1 for r in results if r.status == "passed"),
        "failed": sum(1 for r in results if r.status in ("failed", "error")),
        "skipped": sum(1 for r in results if r.status == "skipped"),
        "incomplete": sum(1 for r in results if r.status == "incomplete"),
        # running 未决：单独计数，否则一个没收敛的用例会被读成"没有失败"。
        "running": sum(1 for r in results if r.status == "running"),
        "cases_produced": len(results),
        "cases_passed": sum(1 for r in results if r.status == "passed"),
        "assertions_pass": sum(1 for r in results for a in r.assertions if a["status"] == "pass"),
        "assertions_fail": sum(1 for r in results for a in r.assertions if a["status"] == "fail"),
        "assertions_skip": sum(1 for r in results for a in r.assertions if a["status"] == "skip"),
        # 必检子项未执行：影响结论与退出码（退出码 2）。
        "required_skips": required_skips,
        "required_skips_count": len(required_skips),
        # 可选子项（未实现能力 / 本轮范围外）：单列，不计 pass/fail，也不影响退出码。
        "optional_skips": optional_skips,
        "optional_skips_count": len(optional_skips),
        "sub_checks_unimplemented": len(optional_skips),
        # 一句话结论，避免读 JSON 的人把「没有 failed」当成「全部通过」。
        "coverage": ("complete" if exit_code(results) == 0 else
                     "failed" if exit_code(results) == 1 else "incomplete"),
    }


def _source_fingerprint() -> dict:
    """当前源码状态的指纹。

    任务书要求新 JSON 必须写"当前源码 HEAD"。只记 HEAD 不够：本轮所有改动都
    未提交，HEAD 在整个过程中一直是 8accb59，无法区分"代码改了没有"。因此同时
    记录工作区是否脏，以及被验收路径上关键文件的 sha256——任何人拿到 JSON 都能
    判断它对应的是哪一版代码。
    """
    import hashlib
    root = REPO_ROOT
    head = "unknown"
    dirty = None
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                              capture_output=True, text=True, timeout=20
                              ).stdout.strip() or "unknown"
        status = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                                capture_output=True, text=True, timeout=60).stdout
        dirty = bool(status.strip())
    except Exception:  # noqa: BLE001 - 没有 git 也不该让验收失败
        pass
    files = {}
    for rel in ("frontend/apps/teacher-web/src/pages/Reports.vue",
                "frontend/apps/teacher-web/src/components/BehaviorEvidenceGallery.vue",
                "backend/src/main/java/com/macteval/service/ReportService.java",
                "backend/src/main/java/com/macteval/service/WindowVersionSelector.java"):
        path = root / rel
        if path.exists():
            files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return {"head": head, "workingTreeDirty": dirty, "fileSha256_16": files}


def write_report(cfg: Config, meta: dict, results: list[CaseResult],
                 started: float, log: Log) -> Path:
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    payload = dict(meta)
    payload.update({
        "duration_seconds": round(time.time() - started, 1),
        "playwright_version": _playwright_version(),
        # 任务书要求：写清这次验收对应哪一版源码（HEAD + 工作区是否脏 + 关键文件哈希）。
        # 用 run_cases() 开头采样的那份，**不要**在这里重新采样——到这一步
        # 证据文件已经落盘，工作区必然脏，采出来的是"运行后"而非"验收对应"的状态。
        "source": (meta.get("source") or _source_fingerprint()),
        "evidencePrefix": SCREENSHOT_PREFIX,

        "cases": [r.to_dict() for r in results],
        "summary": summarize_results(results),
        "log": log.lines,
    })
    path = cfg.out_dir / f"e2e-{_now()}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"报告已写入 {path}")
    return path


REQUIREMENTS_FILE = REPO_ROOT / "scripts" / "verify" / "requirements-browser.txt"

#: requirements-browser.txt 里需要校验的包（Chromium 版本由 playwright 自带绑定决定）。
PINNED_PACKAGES = ("requests", "playwright", "minio")


def read_pinned_versions() -> dict[str, str]:
    """解析固定依赖清单；文件缺失或格式不对返回空 dict（由调用方判定失败）。"""
    try:
        text = REQUIREMENTS_FILE.read_text(encoding="utf-8")
    except OSError:
        return {}
    pins: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "==" not in line:
            continue
        name, _, version = line.partition("==")
        pins[name.strip().lower()] = version.strip()
    return pins


def verify_pinned_versions() -> tuple[bool, str]:
    """把固定清单与实际安装版本对比。

    为什么必须真的比：``requirements-browser.txt`` 的全部价值就是锁版本——
    Playwright 的网络拦截与 CDP 行为会随版本变化，而 U03/U08 的注入时序依赖它。
    只写文件不校验，等于把"锁版本"变成一句注释。
    """
    pins = read_pinned_versions()
    if not pins:
        return False, f"缺少或无法解析 {REQUIREMENTS_FILE.relative_to(REPO_ROOT).as_posix()}"
    try:
        from importlib.metadata import version
    except Exception as exc:  # noqa: BLE001
        return False, f"importlib.metadata 不可用: {exc}"
    actual: dict[str, str] = {}
    drift: list[str] = []
    for name in PINNED_PACKAGES:
        try:
            actual[name] = version(name)
        except Exception:  # noqa: BLE001 - 缺包由前面的导入检查报告
            actual[name] = "MISSING"
            continue
        expected = pins.get(name)
        if expected is None:
            drift.append(f"{name}: 清单未固定（实际 {actual[name]}）")
        elif expected != actual[name]:
            drift.append(f"{name}: 期望 {expected} 实际 {actual[name]}")
    detail = ", ".join(f"{n}={v}" for n, v in actual.items())
    if drift:
        return False, detail + " | 漂移: " + "; ".join(drift)
    return True, detail


def _playwright_version() -> str:
    try:
        from importlib.metadata import version
        return version("playwright")
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# --self-check / --offline-smoke
# ---------------------------------------------------------------------------
def run_self_check(cfg: Config) -> int:
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "status": "pass" if ok else "fail", "detail": detail})

    check("导入 requests", requests is not None,
          getattr(requests, "__version__", "未安装"))
    check("导入 playwright.sync_api", sync_playwright is not None, _playwright_version())
    check("导入 playwright.async_api", async_playwright is not None, "U04 竞态用例需要")

    executable = ""
    try:
        with sync_playwright() as pw:  # type: ignore[misc]
            executable = pw.chromium.executable_path
        exists = bool(executable) and Path(executable).exists()
        check("Chromium 可执行文件存在", exists, executable)
    except Exception as exc:  # noqa: BLE001
        check("Chromium 可执行文件存在", False, f"{type(exc).__name__}: {exc}")

    # minio 是 U08（真实删除对象）的硬依赖。此前它既不在 self-check 里，也不是
    # 防御式导入，于是"装了 playwright 但没装 minio"的机器能通过自检、然后在真实
    # 运行里把 U08 记成 error —— 环境缺陷伪装成用例缺陷。这里提前失败并给出修复命令。
    try:
        import minio as _minio
        from importlib.metadata import version as _ver
        check("导入 minio（U08 真实删除对象需要）", True, _ver("minio"))
    except Exception as exc:  # noqa: BLE001
        check("导入 minio（U08 真实删除对象需要）", False,
              f"{type(exc).__name__}: {exc}；修复："
              f"{sys.executable} -m pip install -r scripts/verify/requirements-browser.txt")

    # 只证明 chrome.exe 存在是不够的：真正的验收路径是**无头**启动。
    # 缺 headless shell / 依赖库不全时，文件在、启动挂，self-check 仍会全绿。
    try:
        with sync_playwright() as pw:  # type: ignore[misc]
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content("<title>probe</title><div id='x'>ok</div>")
            text = page.inner_text("#x")
            browser.close()
        check("Chromium 无头启动并执行一次 DOM 交互", text == "ok", f"innerText={text!r}")
    except Exception as exc:  # noqa: BLE001
        check("Chromium 无头启动并执行一次 DOM 交互", False,
              f"{type(exc).__name__}: {exc}")

    # 固定依赖清单必须被真正比较，否则 requirements-browser.txt 只是一份文档。
    # 版本漂移正是这个文件要防的唯一失败模式。
    check("依赖版本与 requirements-browser.txt 一致", *verify_pinned_versions())

    try:
        cfg.out_dir.mkdir(parents=True, exist_ok=True)
        probe = cfg.out_dir / ".e2e-selfcheck-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        check("输出目录可写", True, str(cfg.out_dir))
    except Exception as exc:  # noqa: BLE001
        check("输出目录可写", False, f"{exc}")

    check("base-url 是 http(s) URL", cfg.base_url.startswith(("http://", "https://")), cfg.base_url)
    check("api-url 是 http(s) URL", cfg.api_url.startswith(("http://", "https://")), cfg.api_url)
    check("用例集合合法", all(c in CASES for c in cfg.cases), ",".join(cfg.cases))
    bad_sel = [k for k, v in SEL.items() if not v.startswith("[data-testid=")]
    check("选择器全部基于 data-testid", not bad_sel, ",".join(bad_sel) or f"{len(SEL)} 项")
    # UNIMPLEMENTED 是"未实现能力"的唯一登记处。P1-2 已把三项全部实现，因此
    # 现在要求它为空——如果有人把某项又改回 unsupported 却忘了登记，或反过来
    # 登记了已经实现的能力（让覆盖看起来更好），这里会失败。
    check("未实现能力登记表与已实现能力一致（当前应为空）", not UNIMPLEMENTED,
          ", ".join(sorted(UNIMPLEMENTED)) or "空（关闭按钮与缩放均已实现）")
    # 未实现能力清单与 U02 的真实断言一一对应：清空 UNIMPLEMENTED 必须伴随
    # 真实缩放/关闭断言，否则"删掉 unimplemented 标记"就变成了无成本的绿色。
    src = Path(__file__).read_text(encoding="utf-8")
    u02 = src[src.index("def case_u02("):src.index("def case_u03(")]
    need = ["缩放 + 后渲染宽度变大", "reset 把宽度恢复到初始值",
            "预览可通过显式关闭按钮关闭", "预览可通过 Esc 关闭"]
    absent = [n for n in need if n not in u02]
    check("缩放/关闭能力由真实断言覆盖（不是删掉 unimplemented 标记）",
          not absent, ",".join(absent) or "4 项")
    # REPORT_SEL 的每一项都必须是真的 data-testid 或稳定的 antd 结构类名；
    # 不允许继续留着 None（那意味着"没有选择器，靠猜"）。
    bad_report_sel = [k for k, v in REPORT_SEL.items() if not v]
    check("Reports.vue 定位方式均已落地（无 None 猜测项）", not bad_report_sel,
          ", ".join(sorted(REPORT_SEL)) if not bad_report_sel else ",".join(bad_report_sel))
    # 新增 testid 必须同时出现在 SEL 里，否则用例会绕过去用别的选择器。
    missing_from_sel = [k for k in ("version_select", "version_option", "version_current",
                                    "detail_version", "detail_source",
                                    "preview_close", "zoom_in", "zoom_out", "zoom_reset",
                                    "zoom_level", "jump_to_video")
                        if k not in SEL]
    check("新增交互能力全部进入选择器契约", not missing_from_sel,
          ",".join(missing_from_sel) or "11 项")
    check("截图命名规则", SCREENSHOT_PREFIX.endswith("-"), SCREENSHOT_PREFIX)
    # 新证据不得与历史证据同名，否则一次运行就会覆盖上一轮的截图。
    check("截图前缀不是已废弃的历史前缀（避免覆盖旧证据）",
          SCREENSHOT_PREFIX != LEGACY_SCREENSHOT_PREFIX, SCREENSHOT_PREFIX)
    # 任务书要求 JSON 里写当前源码：指纹必须能生成且包含 head。
    fp = _source_fingerprint()
    check("源码指纹包含 HEAD 与工作区状态",
          bool(fp.get("head")) and fp.get("workingTreeDirty") is not None, str(fp)[:120])
    # 任务书同时要求写"环境"：端口探测必须覆盖三个服务。
    env_fp = _environment_fingerprint()
    check("环境指纹含三端口探测结果",
          isinstance(env_fp.get("listening"), dict)
          and set(env_fp["listening"]) == {"backend", "ai", "frontend"},
          str(env_fp)[:140])
    check("未硬编码口令",
          not any(v in repr(cfg) for v in ("admin123", "123456")),
          "口令只能来自参数/环境变量")

    ok = all(c["status"] == "pass" for c in checks)
    print(json.dumps({"self_check": "pass" if ok else "fail", "checks": checks},
                     ensure_ascii=False, indent=2))
    return 0 if ok else 1


def run_offline_smoke(cfg: Config) -> int:
    """不连任何服务，只证明 Chromium 能启动并执行一次真实 DOM 交互。"""
    html = (
        "data:text/html;charset=utf-8,"
        "<title>e2e-smoke</title>"
        "<button id='b' onclick=\"document.getElementById('o').textContent='clicked'\">go</button>"
        "<div id='o'>idle</div>"
    )
    try:
        with sync_playwright() as pw:  # type: ignore[misc]
            browser = pw.chromium.launch(headless=cfg.headless)
            page = browser.new_page()
            page.goto(html)
            page.click("#b")
            text = page.inner_text("#o")
            title = page.title()
            browser.close()
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"offline_smoke": "fail", "error": f"{type(exc).__name__}: {exc}"},
                         ensure_ascii=False, indent=2))
        return 1
    ok = text == "clicked" and title == "e2e-smoke"
    print(json.dumps({
        "offline_smoke": "pass" if ok else "fail",
        "title": title, "after_click": text,
        "screenshot": None,
        "note": "离线冒烟不产出任何证据截图，避免与真实链路截图混淆",
    }, ensure_ascii=False, indent=2))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="MACT-Eval 课堂报告证据区浏览器验收（U01–U06）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--base-url", default=os.environ.get("MACT_E2E_BASE_URL", "http://127.0.0.1:5173"))
    parser.add_argument("--api-url", default=os.environ.get("MACT_E2E_API_URL", "http://127.0.0.1:8000/api"))
    parser.add_argument("--teacher-user", default=os.environ.get("MACT_E2E_TEACHER_USER", ""))
    parser.add_argument("--teacher-pass", default=os.environ.get("MACT_E2E_TEACHER_PASS", ""))
    parser.add_argument("--student-user", default=os.environ.get("MACT_E2E_STUDENT_USER", ""))
    parser.add_argument("--student-pass", default=os.environ.get("MACT_E2E_STUDENT_PASS", ""))
    parser.add_argument("--other-teacher-user", default=os.environ.get("MACT_E2E_OTHER_TEACHER_USER", ""))
    parser.add_argument("--other-teacher-pass", default=os.environ.get("MACT_E2E_OTHER_TEACHER_PASS", ""))
    parser.add_argument("--allow-register-other-teacher", action="store_true",
                        help="U05 缺少另一教师账号时，运行期临时注册一个（会写入一条教师账号）")
    parser.add_argument("--case", default="all",
                        help="u01..u06，逗号分隔，或 all")
    parser.add_argument("--mode", choices=("real", "fixture"), default="real")
    parser.add_argument("--report-id", default="", help="指定报告（课堂会话）ID，默认自动挑选证据最多的报告")
    parser.add_argument("--fixture-cards", type=int, default=329, help="fixture 模式的合成卡片数")
    parser.add_argument("--page-size", type=int, default=24, help="契约中的每页卡片数")
    parser.add_argument("--viewports", default="390,768,1280")
    parser.add_argument("--login-mode", choices=("storage", "ui"), default="storage",
                        help="storage=直接写 localStorage（键名同 authSession.ts）；ui=走登录表单")
    parser.add_argument("--headed", dest="headed", action="store_true", default=False)
    parser.add_argument("--headless", dest="headed", action="store_false")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--timeout", type=int, default=15_000, help="单步超时（毫秒）")
    parser.add_argument("--self-check", action="store_true", help="不连浏览器/服务，只校验环境")
    parser.add_argument("--offline-smoke", action="store_true", help="离线打开 data: URL 冒烟")
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> Config:
    if args.case.strip().lower() == "all":
        cases = list(CASES)
    else:
        cases = [c.strip().lower() for c in args.case.split(",") if c.strip()]
        unknown = [c for c in cases if c not in CASES]
        if unknown:
            raise SystemExit(f"未知用例：{unknown}，可选 {CASES} 或 all")
    viewports = [v.strip() for v in args.viewports.split(",") if v.strip()]
    return Config(
        base_url=args.base_url.rstrip("/"),
        api_url=args.api_url.rstrip("/"),
        mode=args.mode,
        teacher_user=args.teacher_user,
        teacher_pass=args.teacher_pass,
        student_user=args.student_user,
        student_pass=args.student_pass,
        other_user=args.other_teacher_user,
        other_pass=args.other_teacher_pass,
        allow_register_other_teacher=args.allow_register_other_teacher,
        report_id=args.report_id,
        cases=cases,
        fixture_cards=args.fixture_cards,
        page_size=args.page_size,
        headless=not args.headed,
        out_dir=Path(args.out_dir),
        viewports=viewports,
        timeout_ms=args.timeout,
        login_mode=args.login_mode,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = build_config(args)
    if args.self_check:
        return run_self_check(cfg)
    if args.offline_smoke:
        return run_offline_smoke(cfg)
    return run_cases(cfg, Log())


if __name__ == "__main__":
    sys.exit(main())
