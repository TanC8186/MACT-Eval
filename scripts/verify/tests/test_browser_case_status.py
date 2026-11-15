#!/usr/bin/env python
"""浏览器验收脚本的用例状态与退出码语义。

这些测试**不启动浏览器、不连任何服务**：它们只导入
``browser_evidence_e2e`` 并直接断言 ``CaseResult`` / ``exit_code`` 的判定规则。
真实浏览器链路由 ``--mode real`` 执行，本文件不声称任何用例已在真实环境通过。

被固化的核心语义：

* 必检子项（``require``）缺失 → 用例 ``incomplete``、退出码非零，
  绝不能因为「顶层没失败」而算通过；
* 可选子项（``unimplemented``）不影响用例状态与退出码；
* 断言失败 → ``failed`` / 退出码 1；
* 一个用例都没执行 → 退出码 2（「什么都没跑」不得伪装成绿色）。

2026-10-28 修订（P0-4）：``passed`` 增加两道硬前提——
``executed`` 为真，且至少有一条 ``pass`` 断言。修订前的 ``finalize()`` 只要
没有失败、没有必检缺失就无条件收敛成 ``passed``，于是「用例函数被删空」和
「分发分支写错导致函数没被调用」这两种**空执行**都会产出绿色结果。
下面 ``test_*`` 逐条覆盖任务书要求的五种终态。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "browser_evidence_e2e.py"


@pytest.fixture(scope="module")
def e2e():
    """把脚本作为模块导入（不执行 main，也不启动浏览器）。"""
    spec = importlib.util.spec_from_file_location("browser_evidence_e2e_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # dataclasses 需要模块已注册在 sys.modules 里才能解析类型注解。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _result(e2e, case: str = "u05"):
    return e2e.CaseResult(case=case, mode="real")


# ---------------------------------------------------------------------------
# 五种终态（任务书 P0-4 明列）
# ---------------------------------------------------------------------------
def test_genuine_pass_yields_passed_and_zero(e2e):
    """真实通过：执行过 + 至少一条通过断言。"""
    result = _result(e2e)
    result.executed = True
    result.check("匿名访问返回 401", True)
    result.finalize()

    assert result.status == "passed"
    assert e2e.exit_code([result]) == 0


def test_empty_case_never_becomes_passed(e2e):
    """空用例：函数被删空 → 既没有 executed 也没有断言 → incomplete、退出码非零。"""
    result = _result(e2e)
    result.finalize()

    assert result.status == "incomplete", "空执行不得收敛为 passed"
    assert e2e.exit_code([result]) == 2
    assert "未被调用" in result.reason


def test_executed_but_zero_assertions_is_incomplete(e2e):
    """调用了但仍然零断言：不可能"通过"。"""
    result = _result(e2e)
    result.executed = True
    result.finalize()

    assert result.status == "incomplete"
    assert e2e.exit_code([result]) == 2


def test_only_optional_skips_is_incomplete(e2e):
    """只有可选跳过、没有任何真实断言：不能算通过。

    可选跳过本身不影响退出码，但它**不能替代**断言——否则把某个用例的实现
    全换成 ``unimplemented()`` 就能拿到绿色。
    """
    result = _result(e2e)
    result.executed = True
    result.unimplemented("preview-zoom-controls", "组件未实现缩放控件")
    result.finalize()

    assert result.status == "incomplete"
    assert e2e.exit_code([result]) == 2


def test_optional_skip_alongside_real_pass_keeps_passed(e2e):
    """有真实断言时的可选跳过不影响结论（这是 U02 的常态）。"""
    result = _result(e2e)
    result.executed = True
    result.check("HH:MM:SS 时间格式", True, "01:00:01")
    result.unimplemented("preview-zoom-controls", "组件未实现缩放控件")
    result.finalize()

    assert result.status == "passed"
    assert e2e.exit_code([result]) == 0


def test_required_subcheck_missing_is_incomplete_and_nonzero(e2e):
    """R10 的核心：缺账号的必检项必须让结论变成「覆盖不完整」。"""
    result = _result(e2e)
    result.executed = True
    result.check("匿名访问返回 401", True)
    result.require("学生访问行为证据返回 403", "未提供 --student-user/--student-pass")
    result.finalize()

    assert result.status == "incomplete"
    assert result.required_skips, "必检缺失必须出现在 requiredSkips 里"
    assert e2e.exit_code([result]) != 0


def test_failed_assertion_wins_over_missing_required(e2e):
    """失败优先：有失败断言时状态是 failed，而不是被 incomplete 掩盖。"""
    result = _result(e2e)
    result.executed = True
    result.check("学生访问返回 403", False, "HTTP 200")
    result.require("另一教师访问返回 403", "未提供账号")
    result.finalize()

    assert result.status == "failed"
    assert e2e.exit_code([result]) == 1


def test_exception_mid_case_is_error_and_nonzero(e2e):
    """用例中途抛异常：分发处把 status 置 error，finalize 不能把它改成 passed。"""
    result = _result(e2e)
    result.executed = True
    result.check("打开报告抽屉", True)
    # 模拟分发处的 except 分支
    result.status = "error"
    result.reason = "PlaywrightError: Target page closed"
    result.finalize()

    assert result.status == "error"
    assert e2e.exit_code([result]) == 1


def test_explicit_skip_is_preserved(e2e):
    """用例自己声明缺前置条件：保留 skipped，退出码 2。"""
    result = _result(e2e)
    result.executed = True
    result.status = "skipped"
    result.reason = "当前报告没有证据帧"
    result.finalize()

    assert result.status == "skipped"
    assert e2e.exit_code([result]) == 2


def test_running_sentinel_never_becomes_passed(e2e):
    """未决哨兵：既没 executed 也没断言 → 不得收敛为 passed。"""
    result = _result(e2e)
    assert result.status == "running"
    result.finalize()

    assert result.status != "passed"
    assert e2e.exit_code([result]) == 2


# ---------------------------------------------------------------------------
# JSON 结构与静态契约
# ---------------------------------------------------------------------------
def test_required_and_optional_are_separated_in_json(e2e):
    result = _result(e2e)
    result.executed = True
    result.check("匿名访问返回 401", True)
    result.require("学生访问行为证据返回 403", "缺账号")
    result.unimplemented("history-version-switch", "UI 无切换控件")
    result.finalize()

    payload = result.to_dict()
    assert [item["name"] for item in payload["requiredSkips"]] == ["学生访问行为证据返回 403"]
    assert [item["name"] for item in payload["optionalSkips"]] == ["history-version-switch"]
    assert payload["incomplete"] is True


def test_summary_reports_coverage_and_assertion_counts(e2e):
    """汇总必须能读出 coverage / requiredSkips / optionalSkips / 断言计数。"""
    ok = _result(e2e, "u01")
    ok.executed = True
    ok.check("分页总数一致", True)
    ok.finalize()

    bad = _result(e2e, "u02")
    bad.executed = True
    bad.check("时间格式", False, "00:01")
    bad.require("同帧目标切换", "缺少第二目标")
    bad.unimplemented("zoom_in", "未实现")
    bad.finalize()

    summary = e2e.summarize_results([ok, bad])
    for key in ("passed", "failed", "skipped", "incomplete", "coverage",
                "assertions_pass", "assertions_fail", "assertions_skip",
                "required_skips", "required_skips_count",
                "optional_skips", "optional_skips_count"):
        assert key in summary, f"summary 缺少 {key}"
    assert summary["assertions_pass"] == 1
    assert summary["assertions_fail"] == 1
    assert summary["required_skips_count"] == 1
    assert summary["optional_skips_count"] == 1


def test_exit_code_rejects_passed_status_without_executed_flag(e2e):
    """status 是普通字符串，可以被任何路径直接赋值；exit_code 必须独立复核。

    这条是"绕过 finalize 直接写 status='passed'"的回归：即使 status 看起来是绿的，
    没有 executed 标记就必须按覆盖不完整处理。
    """
    result = _result(e2e)
    result.status = "passed"
    result.check("某条通过断言", True)
    # 故意不执行 finalize()，模拟绕过统一收敛的代码路径

    assert e2e.exit_code([result]) == 2


def test_exit_code_rejects_passed_status_without_any_pass_assertion(e2e):
    """自称 passed 但一条通过断言都没有：不能算通过。"""
    result = _result(e2e)
    result.executed = True
    result.status = "passed"

    assert e2e.exit_code([result]) == 2


def test_double_finalize_cannot_upgrade_incomplete_to_passed(e2e):
    """二次 finalize 把 incomplete 改写成 passed 是最隐蔽的假绿。"""
    result = _result(e2e)
    result.executed = True
    result.check("匿名访问返回 401", True)
    result.require("学生访问行为证据返回 403", "缺账号")
    result.finalize()
    assert result.status == "incomplete"

    result.required_skips.clear()   # 模拟别处清空了必检标记
    result.finalize()

    assert result.status == "incomplete", "已收敛的 incomplete 不得被二次 finalize 升级"


def test_summary_reports_running_and_produced_counts(e2e):
    """未决 running 必须有独立计数，否则读 JSON 的人只会看到 'failed: 0'。"""
    pending = _result(e2e, "u03")
    ok = _result(e2e, "u01")
    ok.executed = True
    ok.check("分页总数一致", True)
    ok.finalize()

    summary = e2e.summarize_results([ok, pending])
    assert summary["running"] == 1
    assert summary["cases_produced"] == 2
    assert summary["cases_passed"] == 1
    assert summary["coverage"] == "incomplete"


def test_requested_case_without_result_is_recorded_incomplete():
    """分发处漏调用某个用例时，必须自己补一条 incomplete，不能静默消失。

    静态断言：run_cases 里存在"按 cfg.cases 补齐缺失结果"的代码块，且补齐项
    走 require() + finalize()（因此进入 requiredSkips 并让退出码为 2）。
    """
    source = SCRIPT.read_text(encoding="utf-8")
    start = source.index("def run_cases(")
    end = source.index("def exit_code(")
    body = source[start:end]
    assert "for case in cfg.cases:" in body
    assert "已请求但没有任何 CaseResult 产出" in body
    assert "missing.require(" in body
    assert "missing.finalize()" in body


def test_u05_does_not_use_plain_skip_for_required_auth_checks():
    """静态契约：U05 的学生/另一教师 403 必须走 require，而不是可以被忽略的 skip。

    这两项是 review 点名的本轮验收目标；用 skip 会让「没测」看起来像「没失败」。
    """
    source = SCRIPT.read_text(encoding="utf-8")
    start = source.index("def case_u05(")
    u05 = source[start:source.index("\n# -- U04", start)]

    assert 'res.require("学生访问行为证据返回 403"' in u05
    assert 'res.require("另一教师访问返回 403"' in u05
    assert 'res.skip("学生访问行为证据返回 403"' not in u05
    assert 'res.skip("另一教师访问返回 403"' not in u05


def test_every_case_function_marks_executed():
    """静态契约：每个 case_* 函数体第一句必须置 ``executed``。

    这是「函数被删空」与「分发处漏调用」两类空执行的唯一证据来源；
    漏掉任何一个，该用例就会恒为 incomplete（宁可红，不可假绿），
    但更可能的情况是有人新增用例后忘了加标记——所以在这里锁死。
    """
    source = SCRIPT.read_text(encoding="utf-8")
    names = ("case_u01", "case_u02", "case_u03", "case_u04",
             "case_u05", "case_u06", "case_u07", "case_u08")
    for name in names:
        start = source.index(f"def {name}(")
        body = source[start:start + 1200]
        assert "res.executed = True" in body, f"{name} 缺少 res.executed = True"
    assert source.count("res.executed = True") == len(names)


def test_case_functions_do_not_hand_write_passed_status():
    """静态契约：不得再出现手写的 `res.status = ... else "passed"` 三目。

    手写的三目会绕过 finalize() 的两道守卫（executed + 至少一条通过断言），
    历史上有 5 处这样的写法。改回手写必须让本测试失败。

    注意只匹配**赋值语句形式**：``finalize()`` 的 docstring 里引用了这个写法
    做说明，直接搜字符串会把注释也算成违规。
    """
    source = SCRIPT.read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in source.splitlines()
        if line.lstrip().startswith("res.status") and 'else "passed"' in line
    ]
    assert not offenders, (
        "用例不得手写 `else \"passed\"`，必须调用 res.finalize() 走统一收敛:\n"
        + "\n".join(offenders)
    )
