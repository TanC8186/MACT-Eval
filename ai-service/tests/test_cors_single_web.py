"""单端交付（2026-10-27 范围决定）CORS 默认来源回归。

存在理由：``cors_origins`` 的默认值原本写着 ``3001``（已下线的学生端）和
``5174``/``5175``（双端时代的第二个 Vite 端口）。默认值不是无害的注释——
它会被 ``main.py`` 直接喂给 ``CORSMiddleware``，因此只要有部署没显式覆盖
``MACT_AI_CORS_ORIGINS``，这三个来源就仍然被浏览器跨域放行，配置层面继续
宣称"还有第二个正式 Web 端"。

因此本测试断言的是**默认值集合**，而不是"包含 3000/5173"：

1. 默认值只含教师端的两个来源（容器态 3000、开发态 5173），
   localhost 与 127.0.0.1 各一份；
2. 已下线端口**不在**默认值里——按完整 origin 的端口段比较，而不是子串搜索
   （否则 ``:3001`` 会命中 ``:30010`` 之类）。

中间件预检（第三个测试）另建一个**只在该测试内存在**的应用实例，
不去改进程级的 ``settings`` 单例：设置是模块级单例，任何 monkeypatch 改动都会
泄漏到同进程的其它测试与真实运行路径上，导致"测试顺序决定行为"。
"""

from __future__ import annotations

import pytest

from app.core.config import Settings

# 单端交付允许的默认来源：教师端容器态 3000 + 开发态 5173，两种主机写法。
ALLOWED_DEFAULT_ORIGINS = {
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
}

# 已下线 / 从未是正式入口的端口：学生端 3001，双端时代第二个 Vite 5174、5175。
RETIRED_PORTS = ("3001", "5174", "5175")


def default_origins() -> set[str]:
    """按 main.py 的解析方式切分默认值，确保测的是真实生效集合。"""
    raw = Settings.model_fields["cors_origins"].default
    return {origin.strip() for origin in str(raw).split(",") if origin.strip()}


def test_default_origins_are_exactly_the_teacher_web_sources():
    assert default_origins() == ALLOWED_DEFAULT_ORIGINS


@pytest.mark.parametrize("port", RETIRED_PORTS)
def test_retired_ports_are_absent_from_default_origins(port: str):
    """按完整 origin 的端口部分比较，避免 ``:3001`` 命中 ``:30010`` 这种假阳性。"""
    for origin in default_origins():
        _, _, origin_port = origin.rpartition(":")
        assert origin_port != port, f"默认 CORS 仍然放行已下线端口 {port}: {origin}"


def build_client(origins: str):
    """用给定来源白名单临时构造一个等价应用（不碰 settings 单例）。

    这里复刻 main.py 的中间件配置，而不是 import ``app.main``：后者的中间件在
    import 时就用当时的 ``settings.cors_origins`` 固化了，改单例不会改变它，
    于是"改 settings 再看 app"这种测试要么恒真、要么恒假，取决于谁先 import。
    """
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in origins.split(",") if o.strip()],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health():  # pragma: no cover - 只为让预检有目标
        return {"status": "ok"}

    return TestClient(app)


def preflight(client, origin: str):
    return client.options(
        "/health",
        headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
    )


@pytest.mark.parametrize(
    "origin",
    ["http://localhost:3000", "http://localhost:5173",
     "http://127.0.0.1:3000", "http://127.0.0.1:5173"],
)
def test_default_origins_are_allowed_by_preflight(origin: str):
    """默认值里的每个来源都必须真的能通过预检（否则"放行"只是一行字符串）。"""
    client = build_client(Settings.model_fields["cors_origins"].default)
    assert preflight(client, origin).headers.get("access-control-allow-origin") == origin


@pytest.mark.parametrize("port", RETIRED_PORTS)
def test_retired_ports_are_rejected_by_preflight(port: str):
    """已下线端口不仅不能出现在默认值里，还要真的被预检拒绝。"""
    client = build_client(Settings.model_fields["cors_origins"].default)
    for host in ("http://localhost", "http://127.0.0.1"):
        origin = f"{host}:{port}"
        assert preflight(client, origin).headers.get("access-control-allow-origin") is None, \
            f"预检不应放行已下线来源 {origin}"


def test_main_reads_origins_from_settings_not_a_literal():
    """main.py 必须从 settings 派生来源，不能另写一份列表。

    做法是读源码而不是实例化中间件：``CORSMiddleware`` 会把配置吞进 ``self``，
    外部读不到；而"有没有硬编码列表"是**源码性质**，只能看源码。
    """
    from pathlib import Path

    import app.main as main_module

    source = Path(main_module.__file__).read_text(encoding="utf-8")
    assert "settings.cors_origins" in source, "main.py 必须从 settings.cors_origins 派生"
    # 不允许再把 origin 字面量直接写进 main.py
    for port in RETIRED_PORTS + ("3000", "5173"):
        assert f":{port}" not in source, f"main.py 不应出现硬编码端口 :{port}"


def test_origin_override_via_env_is_documented(monkeypatch):
    """覆盖方式必须存在且可用：``MACT_AI_CORS_ORIGINS``。"""
    monkeypatch.setenv("MACT_AI_CORS_ORIGINS", "http://localhost:5173")
    settings = Settings()
    assert settings.cors_origins == "http://localhost:5173"
