#!/usr/bin/env python
"""`scan_log_secrets.py` 的判定规则测试（不读真实日志、不连任何服务）。

## 这个文件守的是什么

敏感字段扫描最容易犯两类错，而且两类都是"看起来在工作"：

1. **把所有命中都当泄漏** → 报出测试夹具的 `ticket=deadbeef…`，运维很快
   学会忽略告警（狼来了），真泄漏时也没人看。
2. **把豁免写成形状匹配** → 一旦 `32 位十六进制`被整体豁免，任何**真实**票据
   泄漏都会被静默放过。这比第一类危险得多，因为它是静默的。

所以下面同时断言两件事：已知占位符必须被豁免，**而另一个形状相同但值不同的
串必须仍被判为 REAL**。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scan_log_secrets.py"


@pytest.fixture(scope="module")
def scanner():
    spec = importlib.util.spec_from_file_location("scan_log_secrets_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def probe_dir():
    """仓库内的临时目录——**不能**用 pytest 的 ``tmp_path``。

    两个原因：
    * ``scan()`` 用 ``path.relative_to(ROOT)`` 取相对路径，仓库外的文件会抛
      ``ValueError``；
    * 更重要的是本项目的硬约束：所有临时文件只能落在 ``E:/Personal_Design`` 下。
      pytest 的 ``tmp_path`` 在 ``C:/Users/.../AppData/Local/Temp``，属于禁止区。
    """
    d = Path(__file__).resolve().parents[3] / ".cache" / "tmp" / "secret-scan-test"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    for f in d.glob("*.log"):
        f.unlink(missing_ok=True)


def _scan_text(scanner, text: str, probe_dir: Path):
    f = probe_dir / "probe.log"
    f.write_text(text, encoding="utf-8")
    return scanner.scan([f])


# ---------------------------------------------------------------------------
# 1) 真凭据必须被抓
# ---------------------------------------------------------------------------
def test_detects_jwt_triple_segment(scanner, probe_dir):
    real, _, _ = _scan_text(
        scanner, "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlc2ln", probe_dir)
    assert any(h["pattern"] == "jwt-triple-segment" for h in real)


def test_detects_random_ticket_of_same_shape(scanner, probe_dir):
    """**关键断言**：与占位符同形状但不同值 → 必须 REAL。"""
    real, placeholder, _ = _scan_text(
        scanner, "GET /?ticket=ffffffffffffffffffffffffffffffff", probe_dir)
    assert any(h["pattern"] == "ws-ticket-query" for h in real), (
        "同形状但不同值的票据必须判为 REAL；若判成占位符，说明豁免变成了形状匹配，"
        "真实泄漏会被静默放过。")
    assert not placeholder


def test_detects_sigv4_signature(scanner, probe_dir):
    real, _, _ = _scan_text(
        scanner, "GET /x?X-Amz-Signature=0123456789abcdef0123", probe_dir)
    assert any(h["pattern"] == "sigv4-signature" for h in real)


def test_detects_minio_secret_literal(scanner, probe_dir):
    real, _, _ = _scan_text(scanner, "MINIO_SECRET_KEY=urig_secret", probe_dir)
    assert any(h["pattern"] == "minio-secret-literal" for h in real)


def test_detects_jwt_secret_env(scanner, probe_dir):
    real, _, _ = _scan_text(
        scanner, "MACT_JWT_SECRETS=abcdefghijklmnopqrstuvwxyz012345", probe_dir)
    assert any(h["pattern"] == "jwt-secret-env" for h in real)


def test_detects_bearer_header(scanner, probe_dir):
    real, _, _ = _scan_text(
        scanner, "Authorization: Bearer abcdefghijklmnopqrstuvwxyz01", probe_dir)
    assert any(h["pattern"] == "bearer-token" for h in real)


# ---------------------------------------------------------------------------
# 2) 已知占位符必须被豁免（否则告警会被忽略）
# ---------------------------------------------------------------------------
def test_known_placeholder_is_exempt(scanner, probe_dir):
    real, placeholder, _ = _scan_text(
        scanner, "GET /?ticket=deadbeefdeadbeefdeadbeefdeadbeef", probe_dir)
    assert not real, "已知夹具值不应判为 REAL"
    assert any(h["pattern"] == "ws-ticket-query" for h in placeholder)


# ---------------------------------------------------------------------------
# 3) 证据里不许出现完整凭据
# ---------------------------------------------------------------------------
def test_redacts_to_six_chars(scanner):
    assert scanner.redact("abcdef1234567890") == "abcdef***"


def test_redacts_short_values_entirely(scanner):
    assert scanner.redact("abc") == "***"


def test_finding_samples_are_redacted(scanner, probe_dir):
    real, _, _ = _scan_text(
        scanner, "GET /?ticket=ffffffffffffffffffffffffffffffff", probe_dir)
    assert real
    for hit in real:
        assert hit["sample"].endswith("***")
        assert "ffffffffffffffffffffffffffffffff" not in hit["sample"]


# ---------------------------------------------------------------------------
# 4) 退出码语义
# ---------------------------------------------------------------------------
def test_placeholder_set_is_exact_values_not_patterns(scanner):
    """白名单必须是精确值集合，不能是正则对象。"""
    assert isinstance(scanner.PLACEHOLDERS, set)
    for value in scanner.PLACEHOLDERS:
        assert isinstance(value, str)
    # 若有人改成 re.Pattern，下面的类型断言会失败
    assert not any(hasattr(v, "match") for v in scanner.PLACEHOLDERS)
