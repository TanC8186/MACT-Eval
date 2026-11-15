"""Evaluate faster-whisper transcripts with corpus-level CER (experiment E2).

Two input modes are supported:

* ``--thchs-root``: a THCHS-30 extraction.  The script looks for
  ``<root>/data/*.wav``, ``<root>/data_thchs30/data/*.wav`` or
  ``<root>/*.wav``; each ``<name>.wav`` must have an adjacent
  ``<name>.wav.trn`` whose first line is the Chinese transcript (following
  lines are pinyin and are ignored).
* ``--manifest``: a CSV with ``audio_path,transcript`` columns.  Relative
  audio paths are resolved against the manifest directory.

For each file the transcript is compared against the reference with
:func:`public_data.asr.cer_pair` after normalisation.  Outputs are written to
``--out``:

    per_file.csv    one row per audio file (reference/hypothesis/edits/CER)
    summary.json    aggregate corpus CER and run metadata
    summary.md      human-readable summary

If faster-whisper is unavailable or the model cannot be loaded, the CLI exits
nonzero before writing any result files.  It never writes placeholder CER.
Per-file transcription failures are kept as ``FAILED`` rows and never counted
as zero-error transcripts; the process then exits with code 2.
"""
from __future__ import annotations

import argparse
import csv
import importlib.metadata
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

if __package__ in (None, ""):
    # Allows ``python tools/public_data/evaluate_asr.py`` from any directory.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from public_data.asr import (
        cer_pair,
        corpus_cer,
        load_model,
        normalization_description,
        transcribe_audio,
    )
else:
    from .asr import (
        cer_pair,
        corpus_cer,
        load_model,
        normalization_description,
        transcribe_audio,
    )

_REPO_ROOT = Path(__file__).resolve().parents[2]

CSV_FIELDS = [
    "item_id",
    "audio_path",
    "reference",
    "hypothesis",
    "edits",
    "ref_chars",
    "cer",
    "status",
    "error",
    "avg_logprob",
    "language",
]


@dataclass(frozen=True)
class AsrItem:
    """One audio file paired with its reference transcript."""

    item_id: str
    audio_path: Path
    reference: str


def _read_text(path: Path) -> str:
    """Read a Chinese text file as UTF-8, falling back to GB18030."""
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return path.read_text(encoding="gb18030")


def read_trn_transcript(
    path: str | Path, _seen: set[Path] | None = None
) -> str:
    """Return the first line of a THCHS-30 ``.trn`` file.

    THCHS-30 ``.trn`` files contain the Chinese transcript on the first line
    and the pinyin sequence on the following line(s).  Some archive layouts
    use pointer files under ``test/`` whose first line is a relative path to
    the real transcript under ``data/``; those pointers are resolved here.
    An empty first line is a malformed transcript and raises ``ValueError``.
    """
    trn_path = Path(path).resolve()
    if not trn_path.is_file():
        raise FileNotFoundError(f"Transcript file not found: {trn_path}")
    seen = set() if _seen is None else set(_seen)
    if trn_path in seen:
        raise ValueError(f"Transcript pointer cycle at {trn_path}")
    seen.add(trn_path)
    lines = _read_text(trn_path).splitlines()
    if not lines or not lines[0].strip():
        raise ValueError(f"Empty transcript on the first line of {trn_path}")
    first = lines[0].strip()
    if first.endswith(".wav.trn"):
        target = (trn_path.parent / first).resolve()
        if not target.is_file():
            # Some THCHS releases use dangling ../data pointers for clips
            # whose real transcript was packaged under train/dev/test.
            base = Path(first).name
            root = trn_path.parent.parent
            for subdir in ("data", "train", "dev", "test"):
                candidate = (root / subdir / base).resolve()
                if candidate.is_file():
                    target = candidate
                    break
        if not target.is_file():
            raise FileNotFoundError(
                f"Transcript pointer target not found: {target} "
                f"(pointer: {trn_path})"
            )
        return read_trn_transcript(target, seen)
    return first


def _resolve_thchs_wav_dir(root: Path) -> Path:
    candidates = [
        root / "data",
        root / "data_thchs30" / "data",
        root,
    ]
    for candidate in candidates:
        if candidate.is_dir() and any(candidate.glob("*.wav")):
            return candidate
    raise FileNotFoundError(
        "No THCHS-30 .wav files found under "
        f"{root}; expected {root / 'data'}/*.wav "
        "(or pass the directory that directly contains the .wav files)"
    )


def collect_thchs_items(root: str | Path) -> list[AsrItem]:
    """Discover THCHS-30 wav/trn pairs under ``root``.

    Raises:
        FileNotFoundError: if the root or a transcript is missing, or no wav
            files are present.
        ValueError: if a ``.trn`` first line is empty.
    """
    root_path = Path(root)
    if not root_path.is_dir():
        raise FileNotFoundError(f"THCHS-30 root not found: {root_path}")

    wav_dir = _resolve_thchs_wav_dir(root_path)
    wav_files = sorted(wav_dir.glob("*.wav"))
    if not wav_files:
        raise FileNotFoundError(f"No .wav files found in {wav_dir}")

    items: list[AsrItem] = []
    for wav_path in wav_files:
        trn_path = wav_path.with_name(wav_path.name + ".trn")
        if not trn_path.is_file():
            raise FileNotFoundError(
                f"Missing THCHS transcript for {wav_path}: expected {trn_path}"
            )
        items.append(
            AsrItem(
                item_id=wav_path.stem,
                audio_path=wav_path,
                reference=read_trn_transcript(trn_path),
            )
        )
    return items


def read_manifest_items(path: str | Path) -> list[AsrItem]:
    """Read a manifest CSV with ``audio_path,transcript`` columns.

    Column names are matched case-insensitively.  Relative audio paths are
    resolved against the manifest directory.  A missing file or column raises
    instead of producing an empty transcript.
    """
    manifest_path = Path(path)
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    try:
        handle = manifest_path.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise OSError(f"Cannot read manifest {manifest_path}: {exc}") from exc

    with handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        lookup = {name.strip().lower(): name for name in fieldnames}
        missing = [
            column
            for column in ("audio_path", "transcript")
            if column not in lookup
        ]
        if missing:
            raise ValueError(
                f"{manifest_path}: missing required manifest column(s) "
                f"{missing}; found {fieldnames}"
            )

        items: list[AsrItem] = []
        for line_number, row in enumerate(reader, start=2):
            audio_value = (row.get(lookup["audio_path"]) or "").strip()
            if not audio_value:
                raise ValueError(
                    f"{manifest_path}:{line_number}: empty audio_path"
                )
            transcript = row.get(lookup["transcript"]) or ""

            audio_path = Path(audio_value)
            if not audio_path.is_absolute():
                audio_path = manifest_path.parent / audio_path
            if not audio_path.is_file():
                raise FileNotFoundError(
                    f"{manifest_path}:{line_number}: audio file not found: "
                    f"{audio_path}"
                )
            items.append(
                AsrItem(
                    item_id=audio_path.stem,
                    audio_path=audio_path,
                    reference=transcript,
                )
            )

    if not items:
        raise ValueError(f"{manifest_path}: manifest contains no data rows")
    return items


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Transcribe audio with faster-whisper and compute CER against "
            "THCHS-30 or a custom manifest."
        )
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--thchs-root",
        type=Path,
        help=(
            "THCHS-30 extraction containing data/*.wav and *.wav.trn "
            "(the data directory itself is also accepted)"
        ),
    )
    source.add_argument(
        "--manifest",
        type=Path,
        help="CSV with audio_path,transcript columns",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "output directory (default: "
            "docs/experiments/runs/<YYYYMMDD-HHMMSS-asr>)"
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="evaluate at most N files (smoke tests)",
    )
    parser.add_argument(
        "--model-size",
        default="small",
        help="faster-whisper model size or local path (default: small)",
    )
    parser.add_argument(
        "--device",
        default="auto",
        help="faster-whisper device: auto, cpu or cuda (default: auto)",
    )
    parser.add_argument(
        "--compute-type",
        default="default",
        help="faster-whisper compute type (default: default)",
    )
    parser.add_argument(
        "--language",
        default="zh",
        help="Whisper language code (default: zh)",
    )
    parser.add_argument(
        "--beam-size",
        type=int,
        default=5,
        help="decoding beam size (default: 5)",
    )
    return parser


def evaluate_items(
    items: Sequence[AsrItem],
    *,
    model: Any,
    language: str,
    beam_size: int,
) -> list[dict[str, Any]]:
    """Transcribe and score every item, keeping per-file failures visible."""
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        print(f"[{index}/{len(items)}] {item.audio_path}", file=sys.stderr)
        try:
            result = transcribe_audio(
                item.audio_path,
                model=model,
                language=language,
                beam_size=beam_size,
            )
        except Exception as exc:
            rows.append(
                {
                    "item_id": item.item_id,
                    "audio_path": str(item.audio_path),
                    "reference": item.reference,
                    "hypothesis": "",
                    "edits": "",
                    "ref_chars": "",
                    "cer": "",
                    "status": "FAILED",
                    "error": f"{type(exc).__name__}: {exc}",
                    "avg_logprob": "",
                    "language": "",
                }
            )
            continue

        hypothesis = result["text"]
        edits, ref_chars = cer_pair(item.reference, hypothesis)
        rows.append(
            {
                "item_id": item.item_id,
                "audio_path": str(item.audio_path),
                "reference": item.reference,
                "hypothesis": hypothesis,
                "edits": edits,
                "ref_chars": ref_chars,
                "cer": edits / ref_chars if ref_chars > 0 else None,
                "status": "ok",
                "error": "",
                "avg_logprob": result.get("avg_logprob"),
                "language": result.get("language"),
            }
        )
    return rows


def _aggregate(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate CER over successfully transcribed rows only."""
    pairs = [
        (row["reference"], row["hypothesis"])
        for row in rows
        if row["status"] == "ok"
    ]
    if not pairs:
        return {
            "corpus_cer": None,
            "total_edits": 0,
            "total_ref_chars": 0,
            "sentence_cer_mean": None,
            "n": 0,
        }
    return corpus_cer(pairs)


def _faster_whisper_version() -> str | None:
    try:
        return importlib.metadata.version("faster-whisper")
    except importlib.metadata.PackageNotFoundError:
        return None


def _dataset_info(args: argparse.Namespace) -> dict[str, Any]:
    if args.thchs_root is not None:
        return {
            "name": "thchs30",
            "root": str(Path(args.thchs_root).resolve()),
        }
    return {
        "name": "manifest",
        "path": str(Path(args.manifest).resolve()),
    }


def _write_per_file_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_summary_json(path: Path, summary: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _format_metric(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


def _write_summary_markdown(
    path: Path,
    summary: dict[str, Any],
    rows: Sequence[dict[str, Any]],
) -> None:
    model = summary["model"]
    lines = [
        "# ASR CER Summary",
        "",
        f"- Run ID: `{summary['run_id']}`",
        f"- Created (UTC): {summary['created_at']}",
        f"- Dataset: `{summary['dataset']['name']}`",
        f"- Audio files: {summary['n']} "
        f"({summary['n_success']} succeeded, {summary['n_failed']} failed)",
        f"- Model: faster-whisper `{model['model_size']}` "
        f"(device={model['device']}, compute_type={model['compute_type']}, "
        f"language={model['language']}, beam_size={model['beam_size']})",
        "- Normalisation: NFKC, whitespace and punctuation removed, "
        "Latin lower-cased, "
        f"traditional→simplified: `{summary['normalization']['traditional_to_simplified']}`",
        "",
        "## Metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Corpus CER | {_format_metric(summary['corpus_cer'])} |",
        f"| Total edits | {summary['total_edits']} |",
        f"| Total reference characters | {summary['total_ref_chars']} |",
        f"| Mean sentence CER | {_format_metric(summary['sentence_cer_mean'])} |",
        f"| Succeeded | {summary['n_success']} |",
        f"| Failed | {summary['n_failed']} |",
        "",
    ]

    failed = [row for row in rows if row["status"] != "ok"]
    lines.append("## Failures")
    lines.append("")
    if not failed:
        lines.append("None.")
    else:
        lines.append("| Item | Audio | Error |")
        lines.append("|---|---|---|")
        for row in failed:
            lines.append(
                f"| {row['item_id']} | `{row['audio_path']}` | "
                f"{row['error']} |"
            )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _build_summary(
    *,
    run_id: str,
    args: argparse.Namespace,
    rows: Sequence[dict[str, Any]],
    aggregate: dict[str, Any],
) -> dict[str, Any]:
    n_success = sum(1 for row in rows if row["status"] == "ok")
    return {
        "run_id": run_id,
        "stage": "asr",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataset": _dataset_info(args),
        "model": {
            "backend": "faster-whisper",
            "version": _faster_whisper_version(),
            "model_size": args.model_size,
            "device": args.device,
            "compute_type": args.compute_type,
            "language": args.language,
            "beam_size": args.beam_size,
        },
        "normalization": normalization_description(),
        "input": {
            "limit": args.limit,
            "n_items": len(rows),
        },
        "n": len(rows),
        "n_success": n_success,
        "n_failed": len(rows) - n_success,
        "corpus_cer": aggregate["corpus_cer"],
        "total_edits": aggregate["total_edits"],
        "total_ref_chars": aggregate["total_ref_chars"],
        "sentence_cer_mean": aggregate["sentence_cer_mean"],
        "n_scored": aggregate["n"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Run the evaluation CLI and return a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be a positive integer")

    try:
        if args.thchs_root is not None:
            items = collect_thchs_items(args.thchs_root)
        else:
            items = read_manifest_items(args.manifest)
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.limit is not None:
        items = items[: args.limit]
    if not items:
        print("error: no audio items to evaluate", file=sys.stderr)
        return 1

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-asr"
    out_dir = (
        Path(args.out)
        if args.out is not None
        else _REPO_ROOT / "docs" / "experiments" / "runs" / run_id
    )

    # Load the model before creating output files: a missing dependency or
    # unavailable model must fail without leaving placeholder results.
    try:
        model = load_model(
            model_size=args.model_size,
            device=args.device,
            compute_type=args.compute_type,
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # model download/device errors from faster-whisper
        print(
            f"error: failed to load faster-whisper model "
            f"{args.model_size!r}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    rows = evaluate_items(
        items,
        model=model,
        language=args.language,
        beam_size=args.beam_size,
    )
    aggregate = _aggregate(rows)
    summary = _build_summary(
        run_id=run_id, args=args, rows=rows, aggregate=aggregate
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_per_file_csv(out_dir / "per_file.csv", rows)
    _write_summary_json(out_dir / "summary.json", summary)
    _write_summary_markdown(out_dir / "summary.md", summary, rows)

    print(
        f"wrote {out_dir / 'per_file.csv'}, {out_dir / 'summary.json'}, "
        f"{out_dir / 'summary.md'}",
        file=sys.stderr,
    )
    print(
        f"corpus CER: {_format_metric(summary['corpus_cer'])} "
        f"({summary['n_success']} ok, {summary['n_failed']} failed)",
        file=sys.stderr,
    )
    return 2 if summary["n_failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
