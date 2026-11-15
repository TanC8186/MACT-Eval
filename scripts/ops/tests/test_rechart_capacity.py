#!/usr/bin/env python
"""Tests for the capacity chart restyle and the CSV-vs-summary peak check.

The tests use synthetic samples only: no measured run is read, so they are safe
to run while other agents work in the same checkout.
"""
from __future__ import annotations

import copy
import csv
import json
import sys
from pathlib import Path

import pytest

OPS_DIR = Path(__file__).resolve().parents[1]
if str(OPS_DIR) not in sys.path:
    sys.path.insert(0, str(OPS_DIR))

import rechart_capacity as rechart  # noqa: E402
from capacity_common import make_chart  # noqa: E402

FIELDS = ["elapsed_s", "timestamp", "host_cpu_pct", "host_mem_used_mb",
          "host_mem_pct", "backend_cpu_pct", "backend_rss_mb", "ai_cpu_pct",
          "ai_rss_mb", "gpu_util_pct", "gpu_mem_used_mb", "gpu_mem_total_mb",
          "gpu_temp_c", "gpu_power_w"]


def synthetic_rows(count: int = 6) -> list[dict]:
    rows = []
    for index in range(count):
        rows.append({
            "elapsed_s": float(index * 2),
            "timestamp": f"2026-10-26T02:47:{38 + index:02d}",
            "host_cpu_pct": 10.0 + index,
            "host_mem_used_mb": 15000.0 + index * 10,
            "host_mem_pct": 94.0,
            "backend_cpu_pct": 0.5 * index,
            "backend_rss_mb": 400.0 + index,
            "ai_cpu_pct": 100.0 + index * 10,
            "ai_rss_mb": 500.0 + index * 20,
            "gpu_util_pct": 1.0 + index,
            "gpu_mem_used_mb": 1500.0 + index * 10,
            "gpu_mem_total_mb": 8188.0,
            "gpu_temp_c": 40.0 + index,
            "gpu_power_w": 11.0 + index,
        })
    return rows


def write_run(run_dir: Path, rows: list[dict], *, summary_peaks: dict | None = None,
              samples: int | None = None) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "resource-samples.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    peaks = rechart.csv_peaks(rows) if summary_peaks is None else summary_peaks
    summary = {
        "metadata": {"label": run_dir.name},
        "resources": {
            "samples": len(rows) if samples is None else samples,
            "gpu_util_peak_pct": peaks["gpu_util_peak_pct"],
            "gpu_mem_peak_mb": peaks["gpu_mem_peak_mb"],
            "ai_cpu_peak_pct": peaks["ai_cpu_peak_pct"],
            "ai_rss_peak_mb": peaks["ai_rss_peak_mb"],
            "backend_rss_peak_mb": peaks["backend_rss_peak_mb"],
        },
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return run_dir


@pytest.fixture
def captured_figure(monkeypatch):
    """Keep the figure that make_chart would otherwise close."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figures = []
    monkeypatch.setattr(plt, "close", lambda fig=None: figures.append(fig))
    return figures


def axis_by_ylabel(figure, needle: str):
    matches = [ax for ax in figure.axes
               if needle.lower() in ax.get_ylabel().lower()]
    assert len(matches) == 1, [ax.get_ylabel() for ax in figure.axes]
    return matches[0]


def test_make_chart_does_not_modify_input(tmp_path, captured_figure):
    rows = synthetic_rows()
    before = copy.deepcopy(rows)
    out = tmp_path / "curve.png"

    make_chart(type("S", (), {"rows": rows})(), out,
               "synthetic run", run_mode="single")

    assert rows == before
    assert out.exists() and out.stat().st_size > 10_000
    assert out.with_suffix(".svg").exists()
    assert out.with_suffix(".svg").stat().st_size > 1_000


def test_gpu_panels_use_distinct_colour_and_linestyle(tmp_path, captured_figure):
    make_chart(type("S", (), {"rows": synthetic_rows()})(),
               tmp_path / "gpu.png", "synthetic gpu", run_mode="single")

    assert captured_figure, "make_chart did not produce a figure"
    figure = captured_figure[-1]
    gpu_ax = axis_by_ylabel(figure, "GPU utilisation")
    gpu_mem_ax = axis_by_ylabel(figure, "GPU memory MB")
    assert gpu_mem_ax.get_shared_x_axes().joined(gpu_ax, gpu_mem_ax)
    assert gpu_ax.get_ylim() == (0.0, 100.0)

    util_line = gpu_ax.get_lines()[0]
    mem_line = gpu_mem_ax.get_lines()[0]
    assert util_line.get_label() == "GPU util % (0-100, left axis)"
    assert mem_line.get_label() == "GPU memory MB (whole card, right axis)"
    assert util_line.get_color() != mem_line.get_color()
    assert util_line.get_linestyle() != mem_line.get_linestyle()
    assert util_line.get_linestyle() == "-"
    assert mem_line.get_linestyle() == "--"

    # axis colour follows its own curve, so the right-axis scale is not read as
    # a percentage of the left axis.
    assert gpu_ax.yaxis.label.get_color() == util_line.get_color()
    assert gpu_mem_ax.yaxis.label.get_color() == mem_line.get_color()

    texts = " ".join(text.get_text() for text in figure.texts).lower()
    assert "whole-card" in texts and "sum across threads" in texts
    assert "single consumer" in texts


def test_concurrent_run_annotation_states_missing_second_consumer(tmp_path,
                                                                  captured_figure):
    make_chart(type("S", (), {"rows": synthetic_rows()})(),
               tmp_path / "concurrent.png", "synthetic concurrent",
               run_mode="concurrent")

    texts = " ".join(text.get_text() for text in captured_figure[-1].texts).lower()
    assert "second consumer" in texts
    assert "not sampled" in texts


def test_memory_panels_use_distinct_colour_and_linestyle(tmp_path, captured_figure):
    make_chart(type("S", (), {"rows": synthetic_rows()})(),
               tmp_path / "mem.png", "synthetic mem", run_mode="single")

    mem_ax = axis_by_ylabel(captured_figure[-1], "Host used memory MB")
    rss_ax = axis_by_ylabel(captured_figure[-1], "Process RSS MB")
    host_line = mem_ax.get_lines()[0]
    backend_line, ai_line = rss_ax.get_lines()[:2]
    assert host_line.get_color() != backend_line.get_color()
    assert backend_line.get_color() != ai_line.get_color()
    assert backend_line.get_linestyle() != ai_line.get_linestyle()
    assert mem_ax.yaxis.label.get_color() == host_line.get_color()
    assert rss_ax.yaxis.label.get_color() == backend_line.get_color()


def write_all_default_runs(root: Path) -> None:
    for label in rechart.DEFAULT_LABELS:
        write_run(root / label, synthetic_rows())


def test_verify_never_rewrites_inputs(tmp_path, capsys):
    write_all_default_runs(tmp_path)
    run_dir = tmp_path / "final-40min-480p"
    csv_before = (run_dir / "resource-samples.csv").read_bytes()
    summary_before = (run_dir / "summary.json").read_bytes()

    code = rechart.main(["--verify", "--source-dir", str(tmp_path)])

    assert code == 0
    capsys.readouterr()
    assert (run_dir / "resource-samples.csv").read_bytes() == csv_before
    assert (run_dir / "summary.json").read_bytes() == summary_before


def test_default_labels_are_the_seven_final_runs():
    assert rechart.DEFAULT_LABELS == [
        "final-40min-480p", "final-80min-480p", "final-120min-480p",
        "final-41min-1080p", "final-40min-asr",
        "final-concurrent-a-40min", "final-concurrent-b-40min",
    ]


def test_default_label_set_reports_absent_runs_as_incomplete(tmp_path, capsys):
    """R09 语义变更：七个必需运行缺任何一个都不再算成功（原断言「只有 1 个运行也 exit 0」
    正是被复核判定的缺陷行为——`failed=[]` 被当成功、`skipped` 不参与判定，
    于是空目录也能输出 ok=true）。"""
    write_run(tmp_path / "final-40min-480p", synthetic_rows())

    code = rechart.main(["--verify", "--source-dir", str(tmp_path)])

    assert code != 0
    payload = json.loads(capsys.readouterr().out)
    assert [run["label"] for run in payload["runs"]] == ["final-40min-480p"]
    assert payload["skipped"] == rechart.DEFAULT_LABELS[1:]
    assert payload["missingRequired"] == rechart.DEFAULT_LABELS[1:]
    assert payload["complete"] is False
    assert payload["ok"] is False
    assert payload["selection"] == "required"
    assert payload["required"] == rechart.DEFAULT_LABELS


def test_verify_accepts_matching_peaks(tmp_path, capsys):
    rows = synthetic_rows()
    write_run(tmp_path / "run-a", rows)

    code = rechart.main(["--verify", "--source-dir", str(tmp_path),
                         "--labels", "run-a"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "verify"
    assert payload["ok"] is True
    assert payload["complete"] is True
    assert payload["missingRequired"] == []
    assert payload["runs"][0]["comparisons"]["gpu_util_peak_pct"]["delta"] == 0.0


def test_verify_fails_when_summary_peak_disagrees(tmp_path, capsys):
    rows = synthetic_rows()
    peaks = rechart.csv_peaks(rows)
    drifted = dict(peaks)
    drifted["gpu_util_peak_pct"] = peaks["gpu_util_peak_pct"] + 0.02
    write_run(tmp_path / "run-b", rows, summary_peaks=drifted)

    code = rechart.main(["--verify", "--source-dir", str(tmp_path),
                         "--labels", "run-b"])

    assert code != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["complete"] is False
    # 输入齐全但峰值不一致：missrequired 为空，判别必须靠 complete/ok 字段而不是 skip 列表。
    assert payload["missingRequired"] == []
    comparison = payload["runs"][0]["comparisons"]["gpu_util_peak_pct"]
    assert comparison["ok"] is False
    assert comparison["delta"] == pytest.approx(0.02)


def test_verify_is_tolerant_of_rounding(tmp_path):
    rows = synthetic_rows()
    peaks = rechart.csv_peaks(rows)
    rounded = {key: round(value, 2) for key, value in peaks.items()}
    run_dir = write_run(tmp_path / "run-c", rows, summary_peaks=rounded)

    report = rechart.verify_run(run_dir, tolerance=0.01)

    assert report["ok"] is True, report


def test_verify_reports_missing_inputs_without_crashing(tmp_path, capsys):
    (tmp_path / "no-csv").mkdir()
    (tmp_path / "no-csv" / "summary.json").write_text("{}", encoding="utf-8")
    write_run(tmp_path / "no-summary", synthetic_rows())
    (tmp_path / "no-summary" / "summary.json").unlink()

    code = rechart.main(["--verify", "--source-dir", str(tmp_path), "--labels"])

    assert code != 0
    payload = json.loads(capsys.readouterr().out)
    by_label = {run["label"]: run for run in payload["runs"]}
    assert by_label["no-csv"]["missing"] == ["resource-samples.csv"]
    assert by_label["no-summary"]["missing"] == ["summary.json"]
    # 目录存在但输入缺文件属于「已验证但不完整」：complete 为 False，且不是被当成漏测跳过。
    assert payload["complete"] is False
    assert payload["missingRequired"] == []


def test_unknown_label_is_reported_incomplete(tmp_path, capsys):
    """显式指定但目录不存在的标签不再静默通过（R09）。"""
    write_run(tmp_path / "run-d", synthetic_rows())

    code = rechart.main(["--verify", "--source-dir", str(tmp_path),
                         "--labels", "run-d", "absent-run"])

    assert code != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["skipped"] == ["absent-run"]
    assert payload["missingRequired"] == ["absent-run"]
    assert payload["complete"] is False
    assert [run["label"] for run in payload["runs"]] == ["run-d"]


# ---------------------------------------------------------------- R09 新增用例
def test_verify_empty_source_dir_is_incomplete_not_success(tmp_path, capsys):
    """空目录（本次复核的实际复现）：必须非零退出，且 JSON 明确 complete=false。"""
    code = rechart.main(["--verify", "--source-dir", str(tmp_path)])

    assert code != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["complete"] is False
    assert payload["runs"] == []
    assert payload["missingRequired"] == rechart.DEFAULT_LABELS
    assert "incomplete" in payload["error"]
    assert payload["selection"] == "required"


def test_verify_one_of_seven_required_runs_lists_the_rest(tmp_path, capsys):
    write_run(tmp_path / rechart.DEFAULT_LABELS[0], synthetic_rows())

    code = rechart.main(["--verify", "--source-dir", str(tmp_path)])

    assert code != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["complete"] is False
    assert payload["missingRequired"] == rechart.DEFAULT_LABELS[1:]
    assert len(payload["missingRequired"]) == 6


def test_verify_all_seven_consistent_runs_pass(tmp_path, capsys):
    write_all_default_runs(tmp_path)

    code = rechart.main(["--verify", "--source-dir", str(tmp_path)])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["complete"] is True
    assert payload["ok"] is True
    assert payload["missingRequired"] == []
    assert [run["label"] for run in payload["runs"]] == rechart.DEFAULT_LABELS
    assert all(run["ok"] for run in payload["runs"])


def test_verify_discovery_mode_without_any_input_is_incomplete(tmp_path, capsys):
    """显式 discovery（--labels 传空）找不到任何运行 → incomplete 且非零。"""
    code = rechart.main(["--verify", "--source-dir", str(tmp_path), "--labels"])

    assert code != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selection"] == "discovery"
    assert payload["required"] == []
    assert payload["runs"] == []
    assert payload["complete"] is False
    assert payload["ok"] is False
    assert "discovery" in payload["reason"]


def test_verify_discovery_mode_with_a_run_passes(tmp_path, capsys):
    write_run(tmp_path / "legacy-run", synthetic_rows())

    code = rechart.main(["--verify", "--source-dir", str(tmp_path), "--labels"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selection"] == "discovery"
    assert payload["complete"] is True
    assert [run["label"] for run in payload["runs"]] == ["legacy-run"]


def test_redraw_refuses_incomplete_coverage(tmp_path, capsys):
    """默认七组缺失时，重绘也必须拒绝，不能只重绘找到的那一组。"""
    write_run(tmp_path / "source" / "final-40min-480p", synthetic_rows())
    out_dir = tmp_path / "evidence"

    code = rechart.main(["--source-dir", str(tmp_path / "source"),
                         "--out-dir", str(out_dir)])

    assert code != 0
    assert not list(out_dir.glob("*.png")) if out_dir.exists() else True
    err = capsys.readouterr().err
    assert "coverage incomplete" in err
    assert "final-80min-480p" in err


def test_redraw_writes_evidence_named_files_and_keeps_csv(tmp_path):
    rows = synthetic_rows()
    run_dir = write_run(tmp_path / "source" / "final-demo-40min", rows)
    csv_before = (run_dir / "resource-samples.csv").read_bytes()
    out_dir = tmp_path / "evidence"

    code = rechart.main(["--source-dir", str(tmp_path / "source"),
                         "--out-dir", str(out_dir),
                         "--labels", "final-demo-40min"])

    assert code == 0
    png = out_dir / "2026-10-26-capacity-final-demo-40min.png"
    assert png.exists() and png.stat().st_size > 10_000
    assert png.with_suffix(".svg").exists()
    assert (run_dir / "resource-samples.csv").read_bytes() == csv_before


def test_redraw_refuses_when_peaks_disagree(tmp_path, capsys):
    rows = synthetic_rows()
    peaks = rechart.csv_peaks(rows)
    drifted = dict(peaks)
    drifted["ai_rss_peak_mb"] = peaks["ai_rss_peak_mb"] + 5.0
    write_run(tmp_path / "source" / "final-drift-40min", rows,
              summary_peaks=drifted)
    out_dir = tmp_path / "evidence"

    code = rechart.main(["--source-dir", str(tmp_path / "source"),
                         "--out-dir", str(out_dir),
                         "--labels", "final-drift-40min"])

    assert code != 0
    assert not list(out_dir.glob("*.png")) if out_dir.exists() else True
    assert "peak verification failed" in capsys.readouterr().err
