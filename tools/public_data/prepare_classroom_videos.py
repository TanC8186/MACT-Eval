#!/usr/bin/env python
"""Prepare public classroom videos for window-level annotation.

The input is a CSV with one row per public classroom recording::

    video_id,video_path,subject,grade,teacher,source_page,license_note

For each recording the script:

* probes duration with ``ffprobe``;
* cuts non-overlapping 60 s windows (the thesis's unified window);
* writes a 360p/10 fps MP4 and a 16 kHz mono WAV per window;
* writes ``window_manifest.csv`` and two annotation sheets (A/B);
* writes a self-contained ``annotation_A.html`` / ``annotation_B.html``
  browser tool (localStorage + CSV export) so annotators can label without a
  server.

No raw video is copied into Git; all outputs stay under ``--out-dir``.
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, Sequence

MANIFEST_COLUMNS = [
    "clip_id",
    "lesson_id",
    "video_id",
    "subject",
    "grade",
    "teacher",
    "source_page",
    "license_note",
    "start_sec",
    "end_sec",
    "duration_sec",
    "window_path",
    "audio_path",
]

ANNOTATION_COLUMNS = [
    "clip_id",
    "lesson_id",
    "subject",
    "grade",
    "teacher",
    "start_sec",
    "end_sec",
    "window_path",
    "label",
    "confidence",
    "notes",
]


@dataclass(frozen=True)
class SourceVideo:
    video_id: str
    video_path: Path
    subject: str
    grade: str
    teacher: str
    source_page: str
    license_note: str


@dataclass(frozen=True)
class Window:
    clip_id: str
    lesson_id: str
    video_id: str
    subject: str
    grade: str
    teacher: str
    source_page: str
    license_note: str
    start_sec: float
    end_sec: float
    duration_sec: float
    window_path: Path
    audio_path: Path


def _run(command: Sequence[str]) -> None:
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(
            "command failed: "
            + " ".join(command)
            + "\n"
            + (completed.stderr or completed.stdout)[-2000:]
        )


def probe_duration(path: Path) -> float:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {completed.stderr.strip()}")
    try:
        return float(completed.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(f"cannot parse duration for {path}") from exc


def read_sources(path: Path) -> list[SourceVideo]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"video_id", "video_path", "subject", "grade", "teacher", "source_page", "license_note"}
        if reader.fieldnames is None or not required.issubset(set(reader.fieldnames)):
            raise ValueError(f"sources CSV missing columns: {sorted(required - set(reader.fieldnames or []))}")
        rows: list[SourceVideo] = []
        for row in reader:
            video = SourceVideo(
                video_id=row["video_id"].strip(),
                video_path=Path(row["video_path"].strip()),
                subject=row["subject"].strip(),
                grade=row["grade"].strip(),
                teacher=row["teacher"].strip(),
                source_page=row["source_page"].strip(),
                license_note=row["license_note"].strip(),
            )
            if not video.video_path.is_file():
                raise FileNotFoundError(f"video not found: {video.video_path}")
            rows.append(video)
    if not rows:
        raise ValueError("sources CSV is empty")
    return rows


def cut_window(
    source: Path,
    start_sec: float,
    duration_sec: float,
    video_out: Path,
    audio_out: Path,
    *,
    height: int = 360,
    fps: int = 10,
) -> None:
    video_out.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{start_sec:.3f}",
            "-t",
            f"{duration_sec:.3f}",
            "-i",
            str(source),
            "-vf",
            f"scale=-2:{height},fps={fps}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "30",
            "-c:a",
            "aac",
            "-b:a",
            "64k",
            "-movflags",
            "+faststart",
            str(video_out),
        ]
    )
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{start_sec:.3f}",
            "-t",
            f"{duration_sec:.3f}",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(audio_out),
        ]
    )


def build_windows(
    sources: Iterable[SourceVideo],
    out_dir: Path,
    *,
    window_seconds: float = 60.0,
    height: int = 360,
    fps: int = 10,
    limit_windows: int | None = None,
) -> tuple[list[Window], list[dict[str, object]]]:
    windows: list[Window] = []
    skipped: list[dict[str, object]] = []
    for source in sources:
        duration = probe_duration(source.video_path)
        full_windows = int(duration // window_seconds)
        if full_windows == 0:
            skipped.append({"video_id": source.video_id, "reason": "shorter than one window"})
            continue
        remainder = duration - full_windows * window_seconds
        if remainder > 1.0:
            skipped.append(
                {
                    "video_id": source.video_id,
                    "reason": f"incomplete tail dropped: {remainder:.1f}s",
                }
            )
        for index in range(full_windows):
            if limit_windows is not None and len(windows) >= limit_windows:
                return windows, skipped
            start = index * window_seconds
            end = min(duration, start + window_seconds)
            clip_id = f"{source.video_id}_w{index:03d}"
            window_dir = out_dir / source.video_id
            window_path = window_dir / f"{clip_id}.mp4"
            audio_path = window_dir / f"{clip_id}.wav"
            if not window_path.is_file() or not audio_path.is_file():
                cut_window(
                    source.video_path,
                    start,
                    end - start,
                    window_path,
                    audio_path,
                    height=height,
                    fps=fps,
                )
            windows.append(
                Window(
                    clip_id=clip_id,
                    lesson_id=source.video_id,
                    video_id=source.video_id,
                    subject=source.subject,
                    grade=source.grade,
                    teacher=source.teacher,
                    source_page=source.source_page,
                    license_note=source.license_note,
                    start_sec=start,
                    end_sec=end,
                    duration_sec=end - start,
                    window_path=window_path,
                    audio_path=audio_path,
                )
            )
    return windows, skipped


def write_manifest(windows: Sequence[Window], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_COLUMNS)
        writer.writeheader()
        for window in windows:
            row = asdict(window)
            row["window_path"] = str(window.window_path)
            row["audio_path"] = str(window.audio_path)
            writer.writerow(row)


def write_annotation_sheet(windows: Sequence[Window], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ANNOTATION_COLUMNS)
        writer.writeheader()
        for window in windows:
            writer.writerow(
                {
                    "clip_id": window.clip_id,
                    "lesson_id": window.lesson_id,
                    "subject": window.subject,
                    "grade": window.grade,
                    "teacher": window.teacher,
                    "start_sec": f"{window.start_sec:.3f}",
                    "end_sec": f"{window.end_sec:.3f}",
                    "window_path": str(window.window_path),
                    "label": "",
                    "confidence": "",
                    "notes": "",
                }
            )


def _html(windows: Sequence[Window], annotator: str) -> str:
    rows = [
        {
            "clip_id": w.clip_id,
            "lesson_id": w.lesson_id,
            "subject": w.subject,
            "grade": w.grade,
            "teacher": w.teacher,
            "start": w.start_sec,
            "end": w.end_sec,
            "src": w.window_path.name,
            "dir": w.video_id,
        }
        for w in windows
    ]
    payload = json.dumps(rows, ensure_ascii=False)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>课堂氛围标注 - {annotator}</title>
<style>
body{{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;margin:20px;background:#f6f7f9;color:#1f2937}}
h1{{font-size:20px}} .meta{{color:#667085;font-size:13px;margin-bottom:16px}}
.row{{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:12px;margin:12px 0;display:grid;grid-template-columns:320px 1fr;gap:16px}}
video{{width:320px;max-height:240px;background:#000;border-radius:8px}}
.title{{font-weight:600;margin-bottom:6px}} .small{{color:#667085;font-size:12px}}
label{{margin-right:12px}} input[type=text]{{width:100%;padding:6px;border:1px solid #d0d5dd;border-radius:6px}}
button{{padding:10px 16px;border:0;border-radius:8px;background:#2563eb;color:#fff;font-weight:600;cursor:pointer}}
.bar{{position:sticky;top:0;background:#f6f7f9;padding:10px 0;z-index:2;display:flex;gap:12px;align-items:center}}
</style></head><body>
<h1>课堂氛围标注（{annotator}）</h1>
<div class="meta">0 = Negative（冷淡/混乱/脱节）；1 = Neutral（常规/有限互动）；2 = Positive（温暖/投入/有效互动）。每个窗口 60 秒，独立标注，不要参考他人。</div>
<div class="bar"><button onclick="exportCsv()">导出 CSV</button><span id="progress" class="small"></span></div>
<div id="rows"></div>
<script>
const rows = {payload};
const key = "mact_classroom_annotation_{annotator}";
const saved = JSON.parse(localStorage.getItem(key) || "{{}}");
const container = document.getElementById("rows");
function save(){{ localStorage.setItem(key, JSON.stringify(saved)); updateProgress(); }}
function updateProgress(){{
  const done = Object.values(saved).filter(v => v.label !== "" && v.label !== undefined).length;
  document.getElementById("progress").textContent = `已完成 ${{done}} / ${{rows.length}}`;
}}
rows.forEach((r, i) => {{
  const cur = saved[r.clip_id] || {{}};
  const div = document.createElement("div"); div.className = "row";
  div.innerHTML = `
    <div><video preload="none" controls src="${{r.dir}}/${{r.src}}"></video></div>
    <div>
      <div class="title">${{r.clip_id}} · ${{r.subject}} · ${{r.grade}}</div>
      <div class="small">${{r.teacher}} · ${{Math.round(r.start)}}s–${{Math.round(r.end)}}s</div>
      <div style="margin:10px 0">
        <label><input type="radio" name="l${{i}}" value="0" ${{cur.label==="0"?"checked":""}}> 0 Negative</label>
        <label><input type="radio" name="l${{i}}" value="1" ${{cur.label==="1"?"checked":""}}> 1 Neutral</label>
        <label><input type="radio" name="l${{i}}" value="2" ${{cur.label==="2"?"checked":""}}> 2 Positive</label>
      </div>
      <div style="margin:10px 0">置信度
        <select id="c${{i}}"><option value="">-</option><option value="1" ${{cur.confidence==="1"?"selected":""}}>1 低</option><option value="2" ${{cur.confidence==="2"?"selected":""}}>2 中</option><option value="3" ${{cur.confidence==="3"?"selected":""}}>3 高</option></select>
      </div>
      <input type="text" id="n${{i}}" placeholder="备注（可选）" value="${{(cur.notes||"").replace(/"/g,"&quot;")}}">
    </div>`;
  div.querySelectorAll(`input[name=l${{i}}]`).forEach(el => el.addEventListener("change", e => {{
    saved[r.clip_id] = {{...(saved[r.clip_id]||{{}}), label: e.target.value}}; save();
  }}));
  div.querySelector(`#c${{i}}`).addEventListener("change", e => {{
    saved[r.clip_id] = {{...(saved[r.clip_id]||{{}}), confidence: e.target.value}}; save();
  }});
  div.querySelector(`#n${{i}}`).addEventListener("input", e => {{
    saved[r.clip_id] = {{...(saved[r.clip_id]||{{}}), notes: e.target.value}}; save();
  }});
  container.appendChild(div);
}});
function exportCsv(){{
  const header = ["clip_id","lesson_id","subject","grade","teacher","start_sec","end_sec","window_path","label","confidence","notes"];
  const lines = [header.join(",")];
  rows.forEach(r => {{
    const v = saved[r.clip_id] || {{}};
    const vals = [r.clip_id,r.lesson_id,r.subject,r.grade,r.teacher,r.start,r.end,`${{r.dir}}/${{r.src}}`,v.label||"",v.confidence||"",v.notes||""];
    lines.push(vals.map(x => `"${{String(x).replace(/"/g,'""')}}"`).join(","));
  }});
  const blob = new Blob([lines.join("\\n")], {{type:"text/csv;charset=utf-8"}});
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob);
  a.download = "annotation_{annotator}.csv"; a.click(); URL.revokeObjectURL(a.href);
}}
updateProgress();
</script></body></html>"""


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--window-seconds", type=float, default=60.0)
    parser.add_argument("--height", type=int, default=360)
    parser.add_argument("--fps", type=int, default=10)
    parser.add_argument("--limit-windows", type=int, default=None)
    args = parser.parse_args(argv)

    if args.window_seconds <= 0:
        parser.error("--window-seconds must be positive")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        parser.error("ffmpeg and ffprobe are required")

    sources = read_sources(args.sources)
    windows, skipped = build_windows(
        sources,
        args.out_dir,
        window_seconds=args.window_seconds,
        height=args.height,
        fps=args.fps,
        limit_windows=args.limit_windows,
    )
    if not windows:
        print("error: no full windows produced", file=sys.stderr)
        return 1

    manifest = args.out_dir / "window_manifest.csv"
    write_manifest(windows, manifest)
    for annotator in ("A", "B"):
        write_annotation_sheet(windows, args.out_dir / f"annotation_{annotator}.csv")
        (args.out_dir / f"annotation_{annotator}.html").write_text(
            _html(windows, annotator), encoding="utf-8"
        )
    (args.out_dir / "annotation_guide.md").write_text(
        "# 课堂氛围标注指南\n\n"
        "- 0 = Negative：冷淡、混乱或脱节；教师负面/控制性强；学生明显走神、违纪或几乎无有效互动。\n"
        "- 1 = Neutral：常规、守纪但互动有限；以教师讲授为主，学生基本在任务中但参与被动。\n"
        "- 2 = Positive：温暖、投入、互动有效；教师鼓励/回应，学生主动参与，整体氛围积极。\n\n"
        "标注单位是 60 秒窗口；先完整看一遍，再给整体印象，不因单个瞬间改变标签。\n"
        "两位标注者独立完成 A/B 表；不要互相讨论。完成后用 merge_annotations.py 计算一致性。\n"
        "若窗口内出现明显事件但整体氛围不典型，在 notes 中说明，不要强行归入极端类。\n\n"
        "## 与 Pri-MCCD 对齐的判定标准（改编自其 Annotation Manual）\n\n"
        "先按 7 分制给整段一个整体印象分，再映射为三档：\n\n"
        "- 1–2 分 → 0 Negative\n"
        "- 3–5 分 → 1 Neutral\n"
        "- 6–7 分 → 2 Positive\n\n"
        "**Positive（需多个一致线索）**：教师语气温暖、鼓励或支持性反馈；学生可见投入（微笑、协同注意、主动参与）；互动提问或回应性对话；过渡顺畅、结构清晰；整体明显超出常规教学。\n\n"
        "**Neutral（常规/程序性互动）**：教师语气平静中性；学生专注但情感表达有限；事实性提问或照本宣科；轻微、非破坏性走神；稳定任务导向，无强正负信号。\n\n"
        "**Negative（明显脱离或紧张）**：训斥、冷语气或轻蔑互动；学生明显脱离（走神、违纪）；过渡混乱或指令不清；长时间沉默显示紧张或互动中断。\n\n"
        "**歧义处理优先级**：\n\n"
        "1. 互动影响 > 孤立手势；\n"
        "2. 持续行为基调 > 短暂微表情；\n"
        "3. 集体反应 > 单个学生行为。\n\n"
        "示例：教师用温和语气纠正学生、姿态支持且反馈建设性 → Neutral，除非有明显学生脱离或情绪紧张。\n"
        "示例：批评语气尖锐且学生明显不适 → Negative，即使教学指令清晰。\n\n"
        "Pri-MCCD 的原始标注单位是 5 秒；本研究用 60 秒窗口。不要把 60 秒内的单个瞬间当成整段标签，先看完整段，再按整体主导氛围和上述优先级判断。\n\n"
        "> 来源：Pri-MCCD Supplementary Annotation Manual（OSF `osf.io/jw6kt` 公开文件）。\n\n"
        "## 标注环境\n\n"
        "`python -m http.server` 不支持 HTTP Range，视频无法拖动进度。\n"
        "请在项目根目录启动支持 Range 的静态服务器：\n\n"
        "```bash\n"
        "npx --yes http-server data/processed/classroom_windows -p 8765 -a 127.0.0.1 -c-1\n"
        "```\n\n"
        "浏览器打开 `http://127.0.0.1:8765/annotation_A.html`（B 表把 A 换成 B）。\n"
        "标注进度保存在浏览器 localStorage，与地址（含端口）绑定；\n"
        "不要更换端口，也不要在 `localhost` 与 `127.0.0.1` 之间切换。\n\n"
        "## 导出与合并\n\n"
        "每页点“导出 CSV”，浏览器会下载 `annotation_A.csv` / `annotation_B.csv`。\n"
        "把两个文件分别保存为：\n\n"
        "- `data/processed/classroom_annotations/annotation_A.csv`\n"
        "- `data/processed/classroom_annotations/annotation_B.csv`\n\n"
        "然后运行 `merge_annotations.py` 计算 Cohen's κ 并生成 `adjudication_needed.csv`。\n",
        encoding="utf-8",
    )
    summary = {
        "videos": len(sources),
        "windows": len(windows),
        "total_window_seconds": sum(w.duration_sec for w in windows),
        "manifest": str(manifest),
        "skipped": skipped,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
