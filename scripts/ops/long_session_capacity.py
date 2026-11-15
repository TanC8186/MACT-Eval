#!/usr/bin/env python
"""Measure one real long-classroom analysis job end to end.

The script creates an isolated teacher/course/session in the capacity database,
uploads a real video or audio file to the isolated MinIO, triggers the normal
two-phase media upload flow, samples host/process/GPU resources, and records the
terminal job state plus database row counts.

Usage:
    python scripts/ops/long_session_capacity.py \
        --base http://127.0.0.1:18081/api \
        --video data/raw/public/classroom_videos/daqi_yuqiang.mp4 \
        --label 40min-real-480p --sample-interval 2 --window-duration 60
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from capacity_common import (  # noqa: E402
    Api, ISO_DIR, ResourceSampler, count_rows, job_row, make_chart,
    pending_callbacks, pending_summary, speech_window_count, stream_length,
    transcript_metrics, wait_job, write_json,
)


def media_duration(path: Path) -> float | None:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=60)
        return round(float(result.stdout.strip()), 3)
    except Exception:
        return None


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:18081/api")
    parser.add_argument("--video", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--media-type", choices=["video", "audio"], default="video")
    parser.add_argument("--sample-interval", type=int, default=2)
    parser.add_argument("--window-duration", type=int, default=60)
    parser.add_argument("--min-observations", type=int, default=5)
    parser.add_argument("--total-seats", type=int, default=30)
    parser.add_argument("--timeout", type=float, default=3600)
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--skip-sha256", action="store_true")
    parser.add_argument(
        "--expected-action-labels",
        default="举手,阅读,书写,讨论,低头,转头",
        help="Comma-separated checkpoint names; empty disables the action-model assertion")
    parser.add_argument(
        "--expected-action-digest",
        default="c356aac1fc8cab0cd478355118ece9bb16441b221b155ddd8b08327c7dd0b738",
        help="SHA-256 of the classroom action checkpoint; empty disables the digest assertion")
    args = parser.parse_args()

    media = Path(args.video)
    if not media.is_file():
        raise SystemExit(f"media not found: {media}")
    out = Path(args.out_dir) if args.out_dir else ISO_DIR / args.label
    out.mkdir(parents=True, exist_ok=True)
    suffix = uuid.uuid4().hex[:8]

    api = Api(args.base)
    api.login_or_register(f"capacity-teacher-{suffix}", "capacity123")
    course_id = api.create_course(suffix)
    lesson_id = api.create_session(course_id, f"容量课堂-{args.label}-{suffix}")

    metadata = {
        "label": args.label,
        "media": str(media),
        "mediaType": args.media_type,
        "mediaBytes": media.stat().st_size,
        "mediaDurationS": media_duration(media),
        "mediaSha256": None if args.skip_sha256 else sha256(media),
        "sampleInterval": args.sample_interval,
        "windowDuration": args.window_duration,
        "minObservations": args.min_observations,
        "totalSeats": args.total_seats,
        "lessonId": lesson_id,
        "courseId": course_id,
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "hardware": {
            "cpuLogical": __import__("psutil").cpu_count(logical=True),
            "memoryBytes": __import__("psutil").virtual_memory().total,
        },
    }
    sampler = ResourceSampler(out, interval_s=2.0).start()
    result = None
    upload = None
    try:
        upload = api.upload_media(
            lesson_id, media, args.media_type, args.total_seats,
            args.sample_interval, args.window_duration, args.min_observations)
        result = wait_job(api, upload["jobId"], timeout_s=args.timeout, poll_s=2.0)
    finally:
        resources = sampler.stop()

    job_id = upload["jobId"] if upload else ""
    db = job_row(job_id) if job_id else {}
    observed = count_rows("behavior_observations", f"job_id='{job_id}'")
    evidence = count_rows("behavior_evidence_frames", f"job_id='{job_id}'")
    windows = count_rows("lesson_window_features", f"lesson_session_id='{lesson_id}'")
    action_frames = count_rows(
        "behavior_evidence_frames",
        f"job_id='{job_id}' and metadata is not null "
        "and metadata::json->>'detectionSource'='behavior_model'")
    behavior_digest_frames = count_rows(
        "behavior_evidence_frames",
        f"job_id='{job_id}' and metadata is not null "
        "and metadata::json->>'detectionSource'='behavior_model' "
        f"and metadata::json->>'modelDigest'='{args.expected_action_digest}'"
        if args.expected_action_digest else "1=0")
    transcript = transcript_metrics(job_id)
    speech_windows = speech_window_count(lesson_id)
    expected_labels = [x.strip() for x in args.expected_action_labels.split(",") if x.strip()]
    action_model = {}
    result_meta = {}
    try:
        if result and result.get("job", {}).get("resultMetadata"):
            result_meta = json.loads(result["job"]["resultMetadata"])
            action_model = result_meta.get("action_evidence_model") or {}
    except (TypeError, ValueError, KeyError):
        pass
    supported = action_model.get("supportedActions") or []
    action_check = {
        "expected": expected_labels,
        "supportedActions": supported,
        "matches": not expected_labels or set(supported) == set(expected_labels),
        "modelDigest": action_model.get("modelDigest"),
        "expectedDigest": args.expected_action_digest or None,
        "digestMatches": (not args.expected_action_digest
                          or action_model.get("modelDigest") == args.expected_action_digest),
        "behaviorModelFrames": action_frames,
        "behaviorModelDigestFrames": behavior_digest_frames,
        "detectionMode": result_meta.get("detection_mode"),
        "visionModelDigest": result_meta.get("model_digest"),
        "note": "visionModelDigest is the shared face/person locator, not the action checkpoint",
    }
    expected_windows = None
    if metadata.get("mediaDurationS"):
        expected_windows = max(1, int(metadata["mediaDurationS"] // args.window_duration))
    checks = {
        "terminalSucceeded": db.get("status") == "SUCCEEDED",
        "videoProducedWindows": args.media_type != "video" or windows > 0,
        "videoProducedObservations": args.media_type != "video" or observed > 0,
        "videoWindowCoverage": args.media_type != "video" or expected_windows is None
        or windows >= expected_windows - 2,
        "actionModelMatchesCheckpoint": args.media_type != "video" or not expected_labels
        or action_check["matches"],
        "actionModelDigestMatches": args.media_type != "video"
        or not args.expected_action_digest or action_check["digestMatches"],
        "behaviorModelFramesPresent": args.media_type != "video" or action_frames > 0,
        "behaviorModelFramesCarryExpectedDigest": args.media_type != "video"
        or not args.expected_action_digest
        or (action_frames > 0 and behavior_digest_frames == action_frames),
        "audioTranscriptPresent": args.media_type != "audio"
        or (transcript is not None and transcript["segments"] > 0
            and transcript["chars"] > 0),
        "audioSpeechWindowsPresent": args.media_type != "audio" or speech_windows > 0,
    }
    make_chart(sampler, out / "resource-curve.png",
               f"Long classroom analysis: {args.label}")
    payload = {
        "metadata": metadata,
        "upload": upload,
        "wait": result,
        "jobDb": db,
        "transcript": transcript,
        "counts": {
            "behaviorObservations": observed,
            "evidenceFrames": evidence,
            "windowFeatures": windows,
            "behaviorModelFrames": action_frames,
            "behaviorModelDigestFrames": behavior_digest_frames,
            "expectedWindows": expected_windows,
            "speechWindows": speech_windows,
            "transcript": transcript,
        },
        "actionModelCheck": action_check,
        "checks": checks,
        "queue": {
            "streamLength": stream_length(),
            "pendingSummary": pending_summary(),
            "pendingCallbacks": pending_callbacks(),
        },
        "resources": resources,
        "notes": [
            "evidenceFrames is the number of selected images, not the number of action events or students.",
            "subjectId is frame-local; it is not a student name or a cross-frame identity.",
            "action labels come from the checkpoint names in the worker result.",
        ],
    }
    write_json(out / "summary.json", payload)
    print(json.dumps({
        "label": args.label,
        "jobId": job_id,
        "status": db.get("status"),
        "attempt": db.get("attempt"),
        "sampled": db.get("sampled_frame_count"),
        "effectiveInterval": db.get("effective_sample_interval_sec"),
        "windows": windows,
        "observations": observed,
        "evidenceFrames": evidence,
        "queueWaitS": result.get("history", [{}])[0].get("at_s") if result else None,
        "jobRunS": db.get("completed_at"),
        "elapsedS": result.get("wait_seconds") if result else None,
        "resource": resources,
        "checks": checks,
    }, ensure_ascii=True, indent=2))
    return 0 if all(checks.values()) else 2


if __name__ == "__main__":
    sys.exit(main())
