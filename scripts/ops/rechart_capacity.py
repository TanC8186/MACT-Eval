#!/usr/bin/env python
"""Regenerate resource curves from saved capacity CSV samples.

The sampler writes raw samples incrementally, so charts can be restyled without
re-running a long video. This is deliberately separate from the measurement
driver: a chart is presentation, never the source of a performance number.

Two modes:

``--verify``
    Recompute each run's peaks from the saved ``resource-samples.csv`` and compare
    them with the peaks recorded in ``summary.json`` (``resources``). A difference
    above ``--tolerance`` (default 0.01) is an inconsistency and returns non-zero.
    Nothing is plotted.

default (redraw)
    Verify first, then redraw every requested label into the evidence folder.
    The CSV is opened read-only and its SHA-256 is checked before and after the
    redraw; any change aborts with non-zero. A JSON manifest (redraw command,
    ``git rev-parse HEAD``, per-run source CSV SHA-256 and peak snapshot) is
    printed to stdout.

Coverage is part of the verdict, not a side note. With no ``--labels`` the seven
``DEFAULT_LABELS`` runs are *required*: if any is absent from ``--source-dir`` the
result is ``"complete": false`` with ``missingRequired`` listing it, and the exit
code is non-zero. An explicit empty ``--labels`` (discovery mode) has no fixed
requirement, but finding no run folder at all is still an absence of evidence and
fails the same way. Only a run that was actually read can be "verified", so an
empty directory must never produce a green result.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from capacity_common import ISO_DIR, ROOT, make_chart  # noqa: E402

EVIDENCE = ROOT / "docs" / "reviews" / "evidence"
DEFAULT_LABELS = [
    "final-40min-480p",
    "final-80min-480p",
    "final-120min-480p",
    "final-41min-1080p",
    "final-40min-asr",
    "final-concurrent-a-40min",
    "final-concurrent-b-40min",
]
DEFAULT_TOLERANCE = 0.01

NUMERIC = {
    "elapsed_s", "host_cpu_pct", "host_mem_used_mb", "host_mem_pct",
    "backend_cpu_pct", "backend_rss_mb", "ai_cpu_pct", "ai_rss_mb",
    "gpu_util_pct", "gpu_mem_used_mb", "gpu_mem_total_mb", "gpu_temp_c",
    "gpu_power_w",
}

# summary.json key -> raw CSV column
PEAK_KEYS = {
    "gpu_util_peak_pct": "gpu_util_pct",
    "gpu_mem_peak_mb": "gpu_mem_used_mb",
    "ai_cpu_peak_pct": "ai_cpu_pct",
    "ai_rss_peak_mb": "ai_rss_mb",
    "backend_rss_peak_mb": "backend_rss_mb",
}


def load_rows(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            converted = {}
            for key, value in row.items():
                if key in NUMERIC:
                    try:
                        converted[key] = float(value) if value not in ("", "None") else None
                    except ValueError:
                        converted[key] = None
                else:
                    converted[key] = value
            rows.append(converted)
    return rows


def csv_peaks(rows: list[dict], keys: dict[str, str] | None = None) -> dict[str, float | None]:
    peaks: dict[str, float | None] = {}
    for summary_key, column in (keys or PEAK_KEYS).items():
        values = [r[column] for r in rows if isinstance(r.get(column), (int, float))]
        peaks[summary_key] = max(values) if values else None
    return peaks


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_head() -> str:
    """Read-only code version. Never mutates the repository."""
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"unavailable: {exc}"
    if result.returncode != 0:
        return f"unavailable: {result.stderr.strip()[:200]}"
    return result.stdout.strip()


def verify_run(run_dir: Path, tolerance: float = DEFAULT_TOLERANCE) -> dict:
    """Compare CSV-derived peaks with the recorded summary peaks."""
    label = run_dir.name
    csv_path = run_dir / "resource-samples.csv"
    summary_path = run_dir / "summary.json"
    report: dict = {
        "label": label,
        "sourceCsv": str(csv_path),
        "summary": str(summary_path),
        "ok": False,
        "comparisons": {},
        "missing": [],
    }
    if not csv_path.exists():
        report["missing"].append("resource-samples.csv")
        report["error"] = "source CSV missing"
        return report
    if not summary_path.exists():
        report["missing"].append("summary.json")
        report["error"] = "summary.json missing"
        return report
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        report["error"] = f"summary.json unreadable: {exc}"
        return report
    recorded = summary.get("resources")
    if not isinstance(recorded, dict):
        report["error"] = "summary.json has no resources section"
        return report
    rows = load_rows(csv_path)
    report["csvRows"] = len(rows)
    report["summarySamples"] = recorded.get("samples")
    peaks = csv_peaks(rows)
    ok = True
    for key in PEAK_KEYS:
        csv_value = peaks[key]
        summary_value = recorded.get(key)
        if isinstance(summary_value, (int, float)):
            summary_value = float(summary_value)
        else:
            summary_value = None
        if csv_value is None or summary_value is None:
            match = csv_value is None and summary_value is None
            delta = None
        else:
            delta = abs(csv_value - summary_value)
            match = delta <= tolerance
        report["comparisons"][key] = {
            "csv": csv_value,
            "summary": summary_value,
            "delta": delta,
            "ok": match,
        }
        ok = ok and match
    report["ok"] = ok
    return report


def _run_mode(label: str) -> str:
    return "concurrent" if "concurrent" in label.lower() else "single"


def _display_path(path: Path) -> str:
    """Prefer a repo-relative path so the recorded command is copy-pasteable."""
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def redraw_run(run_dir: Path, out_dir: Path, label: str,
               source_dir: Path | None = None) -> dict:
    csv_path = run_dir / "resource-samples.csv"
    before = sha256_file(csv_path)
    rows = load_rows(csv_path)
    out_path = out_dir / f"2026-10-26-capacity-{label}.png"
    title = f"Long classroom analysis: {label}"
    make_chart(SimpleNamespace(rows=rows), out_path, title, run_mode=_run_mode(label))
    after = sha256_file(csv_path)
    if before != after:
        raise RuntimeError(f"source CSV changed during redraw: {csv_path}")
    source = source_dir or ISO_DIR
    verify_command = (
        f'python scripts/ops/rechart_capacity.py --verify '
        f'--source-dir "{_display_path(source)}" --labels "{label}"')
    command = (
        f'python scripts/ops/rechart_capacity.py --labels "{label}" '
        f'--source-dir "{_display_path(source)}" --out-dir "{_display_path(out_dir)}"'
    )
    return {
        "label": label,
        "sourceCsv": str(csv_path),
        "csvSha256": before,
        "csvRows": len(rows),
        "peaks": csv_peaks(rows),
        "outputs": [str(out_path), str(out_path.with_suffix(".svg"))],
        "verifyCommand": verify_command,
        "command": command,
        "runMode": _run_mode(label),
    }


def _discovery_dirs(source_dir: Path) -> list[Path]:
    """Every folder holding a CSV or a summary (the legacy ``smoke-90s`` smoke run excepted).

    A folder with only one of the two inputs is still a run that should be reported
    as incomplete, not silently ignored.
    """
    candidates = {
        p.parent.name for p in source_dir.glob("*/resource-samples.csv")}
    candidates |= {p.parent.name for p in source_dir.glob("*/summary.json")}
    return [source_dir / label for label in sorted(candidates) if label != "smoke-90s"]


def _select_dirs(source_dir: Path, labels: list[str] | None) -> tuple[list[Path], list[str]]:
    """Resolve run folders; return (folders to check, labels absent from ``source_dir``).

    With an explicit non-empty ``labels``, a folder that exists but lacks data is
    *not* silently dropped: ``verify_run`` reports the missing file and fails the
    run, so the difference between "measured but incomplete" and "never measured"
    stays visible. An empty ``labels`` list means discovery: every folder holding
    a CSV or a summary is checked.
    """
    found: list[Path] = []
    missing: list[str] = []
    if labels:
        for label in labels:
            run_dir = source_dir / label
            if run_dir.is_dir():
                found.append(run_dir)
            else:
                missing.append(label)
        return found, missing
    return _discovery_dirs(source_dir), missing


def resolve_selection(source_dir: Path, labels: list[str] | None) -> dict:
    """Decide which runs the invocation *requires* and which are merely discovered.

    ``required`` mode (no ``--labels``, or explicit labels): every requested label
    must exist. A missing one makes the whole check incomplete — an absent run can
    never be "verified", so it must not be folded into a green result (this is the
    empty-directory false pass reported in the 2026-10-27 review, R09).

    ``discovery`` mode (explicit empty ``--labels``): there is no fixed requirement,
    but finding *nothing* is still an absence of evidence, never a pass.
    """
    if labels:
        run_dirs, missing = _select_dirs(source_dir, labels)
        return {"selection": "required", "required": list(labels),
                "runDirs": run_dirs, "missingRequired": missing}
    return {"selection": "discovery", "required": [],
            "runDirs": _discovery_dirs(source_dir), "missingRequired": []}


def summarize_selection(sel: dict) -> tuple[bool, str]:
    """Return (complete, reason). ``complete`` is False when nothing proved the runs exist."""
    if sel["selection"] == "required":
        if sel["missingRequired"]:
            return False, "required runs absent from source-dir"
        return True, ""
    if not sel["runDirs"]:
        return False, "discovery found no run folders (no resource-samples.csv / summary.json)"
    return True, ""


def coverage_fields(sel: dict, complete: bool, reason: str = "") -> dict:
    """Explicit coverage fields so "nothing was verified" is never only implied by a skip list."""
    fields = {
        "selection": sel["selection"],
        "required": sel["required"],
        "missingRequired": sel["missingRequired"],
        "found": [path.name for path in sel["runDirs"]],
        "complete": complete,
    }
    if not complete:
        fields["error"] = "incomplete coverage: not every required run could be verified"
        fields["reason"] = reason
    return fields


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true",
                        help="compare CSV peaks with summary.json and exit")
    parser.add_argument("--labels", nargs="*", default=None,
                        help="run labels to process (default: the seven final runs, all "
                             "required; pass --labels with no values to discover every saved "
                             "run — discovering nothing is still an incomplete failure)")
    parser.add_argument("--source-dir", default=str(ISO_DIR),
                        help="directory holding the saved run folders")
    parser.add_argument("--out-dir", default=str(EVIDENCE),
                        help="where the redrawn PNG/SVG are written")
    parser.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE,
                        help="maximum allowed CSV-vs-summary peak difference")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source_dir = Path(args.source_dir)
    out_dir = Path(args.out_dir)
    # No --labels at all -> the seven final runs (required). An explicit empty
    # --labels means "discover every saved run" (legacy/defect runs included).
    labels = DEFAULT_LABELS if args.labels is None else args.labels
    selection = resolve_selection(source_dir, labels)
    run_dirs = selection["runDirs"]
    complete, incomplete_reason = summarize_selection(selection)

    verifications = [verify_run(run_dir, args.tolerance) for run_dir in run_dirs]
    if args.verify:
        failed = [v for v in verifications if not v["ok"]]
        payload = {
            "mode": "verify",
            "tolerance": args.tolerance,
            "gitHead": git_head(),
        }
        payload.update(coverage_fields(selection, complete, incomplete_reason))
        payload.update({
            "runs": verifications,
            "skipped": selection["missingRequired"],
            "complete": complete and not failed,
            "ok": complete and not failed,
        })
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        # 2 = 覆盖不完整（缺必需运行 / discovery 无输入）或峰值不一致；两者都不得算成功。
        return 0 if (complete and not failed) else 2

    failed = [v for v in verifications if not v["ok"]]
    if failed or not complete:
        payload = {"mode": "redraw"}
        payload.update(coverage_fields(
            selection, False,
            incomplete_reason or "peak verification failed; refusing to redraw"))
        payload.update({
            "error": ("coverage incomplete; refusing to redraw" if not complete
                      else "peak verification failed; refusing to redraw"),
            "failures": failed,
        })
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    manifests = []
    for run_dir in run_dirs:
        try:
            manifests.append(redraw_run(run_dir, out_dir, run_dir.name, source_dir))
        except Exception as exc:  # report, do not fabricate success
            print(f"{run_dir}: redraw failed: {exc}", file=sys.stderr)
            return 3
    payload = {
        "mode": "redraw",
        "gitHead": git_head(),
        "tolerance": args.tolerance,
        "outputDir": str(out_dir),
    }
    payload.update(coverage_fields(selection, True))
    payload.update({"runs": manifests, "skipped": selection["missingRequired"]})
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
