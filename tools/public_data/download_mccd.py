#!/usr/bin/env python
"""Download MCCD clips described by ``manifest_download.csv``.

MCCD (https://osf.io/fw28j) ships one MP4 per 5-second clip under three class
folders.  The manifest is produced by enumerating the OSF file API, so the
download URLs are the authoritative ones; this script only adds resume,
retry and throughput reporting on top.

Files already present with the expected size are skipped, so an interrupted
run can simply be restarted.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable, Sequence

USER_AGENT = "Mozilla/5.0 (compatible; mact-eval-research/1.0)"


def read_manifest(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"clip_id", "label", "size", "url"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError(
                f"{path} missing columns: {sorted(required - set(reader.fieldnames or []))}"
            )
        return [dict(row) for row in reader]


def target_path(root: Path, row: dict[str, str]) -> Path:
    label = int(row["label"])
    folder = {0: "negative", 1: "neutral", 2: "positive"}[label]
    return root / folder / f"{row['clip_id']}.mp4"


def already_complete(path: Path, expected: int) -> bool:
    return path.is_file() and expected > 0 and path.stat().st_size == expected


def download_one(
    row: dict[str, str],
    root: Path,
    *,
    retries: int = 3,
    timeout: int = 120,
) -> tuple[str, str]:
    """Return ``(clip_id, status)``; status is one of ok/skip/fail:<reason>."""
    path = target_path(root, row)
    expected = int(row["size"])
    if already_complete(path, expected):
        return row["clip_id"], "skip"

    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".mp4.part")
    last_error = ""
    for attempt in range(retries):
        try:
            request = urllib.request.Request(row["url"], headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                with partial.open("wb") as handle:
                    while True:
                        chunk = response.read(1 << 20)
                        if not chunk:
                            break
                        handle.write(chunk)
            written = partial.stat().st_size
            if expected > 0 and written != expected:
                partial.unlink(missing_ok=True)
                last_error = f"size {written} != {expected}"
                continue
            partial.replace(path)
            return row["clip_id"], "ok"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            partial.unlink(missing_ok=True)
            # OSF returns 502 when too many connections are opened at once;
            # back off longer so the pool drains instead of retrying into it.
            sleep_s = 5 * (attempt + 1) if "502" in str(exc) else 2 * (attempt + 1)
            time.sleep(sleep_s)
    return row["clip_id"], f"fail:{last_error}"


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)

    rows = read_manifest(args.manifest)
    if args.limit is not None:
        rows = rows[: args.limit]
    total = len(rows)
    if total == 0:
        print("error: manifest is empty", file=sys.stderr)
        return 1

    started = time.time()
    done = 0
    failed: list[dict[str, str]] = []
    skipped = 0
    for row in rows:
        if already_complete(target_path(args.out, row), int(row["size"])):
            skipped += 1
    print(f"{skipped}/{total} already present", flush=True)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(download_one, row, args.out): row for row in rows}
        for future in as_completed(futures):
            row = futures[future]
            clip_id, status = future.result()
            done += 1
            if status.startswith("fail"):
                failed.append({"clip_id": clip_id, "status": status})
            if done % 25 == 0 or done == total:
                elapsed = time.time() - started
                rate = done / elapsed if elapsed > 0 else 0.0
                print(
                    f"[{done}/{total}] {rate:.1f} clips/s "
                    f"failed={len(failed)} elapsed={elapsed:.0f}s",
                    flush=True,
                )

    report = {
        "total": total,
        "already_present": skipped,
        "failed": failed,
        "out": str(args.out),
        "elapsed_s": round(time.time() - started, 1),
    }
    report_path = args.report or (args.out.parent / "download_report.json")
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in report.items() if k != "failed"},
                     ensure_ascii=False, indent=2))
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
