"""Draw a stratified Pri-MCCD sample and write a manual transcription template.

Usage::

    python tools/public_data/make_manual_transcripts.py \
        --manifest data/processed/pri_mccd/manifest.csv \
        --out data/processed/pri_mccd/manual_transcripts \
        [--per-class 30] [--seed 42] [--minutes-per-clip 5/60]

The sample is stratified by climate label and, when the manifest provides
``lesson_id``, by lesson within each label: every lesson receives one clip
first (as far as the per-class budget allows), and the remaining slots are
allocated proportionally to lesson size.  Sampling uses a seeded
``random.Random`` instance and is deterministic for a given manifest, budget
and seed.

The output directory receives:

* ``manual_transcripts.csv`` with columns ``clip_id``, ``audio_path``,
  ``label``, ``lesson_id`` and an intentionally empty ``transcript`` column;
* ``manual_transcripts_README.md`` with verbatim transcription instructions.

The ``transcript`` column is never prefilled: reference transcripts must be
produced independently of the ASR system under evaluation, otherwise the
in-domain CER measurement is circular.  Clips without an audio path are
excluded because they cannot be transcribed.
"""
from __future__ import annotations

import argparse
import math
import random
import sys
from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from public_data.labels import LABEL_NAMES
else:
    from .labels import LABEL_NAMES

OUTPUT_COLUMNS = ["clip_id", "audio_path", "label", "lesson_id", "transcript"]
REQUIRED_MANIFEST_COLUMNS = ("clip_id", "audio_path", "label")

TRANSCRIPT_CSV_NAME = "manual_transcripts.csv"
README_NAME = "manual_transcripts_README.md"


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value}")
    return parsed


def _minutes_per_clip(value: str) -> float:
    """Parse ``5/60`` or a plain float as minutes."""
    text = value.strip()
    if "/" in text:
        numerator, _, denominator = text.partition("/")
        try:
            result = float(numerator) / float(denominator)
        except (ValueError, ZeroDivisionError) as exc:
            raise argparse.ArgumentTypeError(
                f"invalid fraction for minutes per clip: {value!r}"
            ) from exc
    else:
        try:
            result = float(text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                f"invalid minutes per clip: {value!r}"
            ) from exc
    if result <= 0:
        raise argparse.ArgumentTypeError(
            f"minutes per clip must be positive, got {value}"
        )
    return result


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Stratified Pri-MCCD sampling for manual ASR spot-check "
            "transcripts. Writes an empty transcript template; it never "
            "prefills ASR output."
        )
    )
    parser.add_argument(
        "--manifest",
        required=True,
        type=Path,
        help="Manifest CSV produced by prepare_pri_mccd.py",
    )
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help=f"Output directory for {TRANSCRIPT_CSV_NAME} and {README_NAME}",
    )
    parser.add_argument(
        "--per-class",
        type=_positive_int,
        default=30,
        help="Maximum clips to sample per climate label (default: 30)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for reproducible sampling (default: 42)",
    )
    parser.add_argument(
        "--minutes-per-clip",
        type=_minutes_per_clip,
        default=5 / 60,
        help=(
            "Audio minutes per clip used for the workload estimate; accepts "
            "plain minutes or a fraction such as 5/60 (default: 5/60, i.e. "
            "five-second clips)"
        ),
    )
    return parser.parse_args(argv)


def load_manifest(path: Path) -> pd.DataFrame:
    """Read and validate a manifest CSV."""
    if not path.exists():
        raise FileNotFoundError(f"Manifest does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"Manifest path is not a file: {path}")

    manifest = pd.read_csv(path, dtype={"clip_id": str})
    missing = [
        column
        for column in REQUIRED_MANIFEST_COLUMNS
        if column not in manifest.columns
    ]
    if missing:
        raise ValueError(
            f"Manifest {path} is missing required column(s): {', '.join(missing)}"
        )
    if manifest.empty:
        raise ValueError(f"Manifest {path} contains no clips")

    if "lesson_id" not in manifest.columns:
        manifest["lesson_id"] = None

    try:
        manifest["label"] = pd.to_numeric(
            manifest["label"], errors="raise"
        ).astype(int)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Manifest {path} has non-integer climate labels"
        ) from exc

    unknown = sorted(set(manifest["label"]) - set(LABEL_NAMES))
    if unknown:
        raise ValueError(
            f"Manifest {path} has unknown climate label(s): {unknown}; "
            f"expected {sorted(LABEL_NAMES)}"
        )
    return manifest


def _lesson_key(value: object) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    if pd.isna(value):
        return ""
    return str(value)


def _allocate_quotas(n: int, sizes: Mapping[str, int]) -> dict[str, int]:
    """Allocate ``n`` samples across lesson strata of the given sizes.

    Every non-empty stratum gets one clip when the budget allows, so a small
    budget still spreads the sample across lessons instead of concentrating on
    the largest class.  Remaining slots are allocated proportionally with the
    largest-remainder method.  The returned quotas sum to ``min(n, total)``.
    """
    keys = sorted(sizes)
    total = sum(sizes.values())
    n = min(n, total)
    if n <= 0:
        return {key: 0 for key in keys}

    if n <= len(keys):
        ranked = sorted(keys, key=lambda key: (-sizes[key], key))
        chosen = set(ranked[:n])
        return {key: (1 if key in chosen else 0) for key in keys}

    quotas = {key: 1 for key in keys}
    remaining = n - len(keys)
    capacities = {key: sizes[key] - 1 for key in keys}
    capacity_total = sum(capacities.values())
    if remaining and capacity_total:
        exact = {
            key: remaining * capacities[key] / capacity_total for key in keys
        }
        floors = {key: math.floor(exact[key]) for key in keys}
        leftovers = remaining - sum(floors.values())
        ranked = sorted(
            keys, key=lambda key: (-(exact[key] - floors[key]), key)
        )
        for key in ranked[:leftovers]:
            floors[key] += 1
        for key in keys:
            quotas[key] += floors[key]
    return quotas


def _sample_label(
    candidates: pd.DataFrame, per_class: int, rng: random.Random
) -> pd.DataFrame:
    """Sample up to ``per_class`` rows, stratified by lesson when available."""
    if candidates.empty or per_class <= 0:
        return candidates.iloc[0:0]

    candidates = candidates.sort_values("clip_id", kind="stable")
    lesson_keys = candidates["lesson_id"].map(_lesson_key)
    if lesson_keys.eq("").all():
        take = min(per_class, len(candidates))
        indices = rng.sample(list(candidates.index), take)
        return candidates.loc[sorted(indices)]

    keyed = candidates.assign(_lesson_key=lesson_keys)
    sizes = {
        key: len(group)
        for key, group in keyed.groupby("_lesson_key", sort=True)
    }
    quotas = _allocate_quotas(per_class, sizes)

    chosen: list[int] = []
    for key in sorted(sizes):
        group = keyed[keyed["_lesson_key"] == key]
        take = min(quotas[key], len(group))
        if take:
            chosen.extend(rng.sample(list(group.index), take))
    return candidates.loc[sorted(chosen)]


def sample_manifest(
    manifest: pd.DataFrame, *, per_class: int, seed: int
) -> tuple[pd.DataFrame, int]:
    """Return ``(sampled_frame, excluded_missing_audio_count)``.

    Rows without an audio path are excluded: a manual transcript cannot be
    produced from missing audio, and substituting a placeholder would fake the
    evaluation input.
    """
    audio = manifest["audio_path"]
    has_audio = audio.notna() & (audio.astype(str).str.strip() != "")
    eligible = manifest[has_audio]
    excluded = int((~has_audio).sum())

    rng = random.Random(seed)
    selected_frames = [
        _sample_label(eligible[eligible["label"] == label], per_class, rng)
        for label in sorted(LABEL_NAMES)
    ]
    selected = pd.concat(selected_frames, ignore_index=False)
    if selected.empty:
        raise ValueError(
            "No clips with a non-empty audio_path are available for sampling"
        )
    selected = selected.copy()
    selected["transcript"] = ""
    selected["_lesson_sort"] = selected["lesson_id"].map(_lesson_key)
    selected = selected.sort_values(
        ["label", "_lesson_sort", "clip_id"], kind="stable"
    )
    selected = selected.drop(columns="_lesson_sort")
    return selected[OUTPUT_COLUMNS].reset_index(drop=True), excluded


def _render_readme(
    sampled: pd.DataFrame,
    *,
    per_class: int,
    seed: int,
    minutes_per_clip: float,
    excluded_missing_audio: int,
    manifest_name: str,
) -> str:
    label_counts = {
        label: int((sampled["label"] == label).sum())
        for label in sorted(LABEL_NAMES)
    }
    total = len(sampled)
    audio_minutes = total * minutes_per_clip
    seconds_per_clip = minutes_per_clip * 60

    lines = [
        "# Pri-MCCD manual transcription template",
        "",
        "This directory contains the in-domain ASR spot-check sample for the",
        "MACT-Eval public-data experiments. The `transcript` column is",
        "intentionally empty; fill it in by hand.",
        "",
        "## Sample",
        "",
        f"- Source manifest: `{manifest_name}`",
        f"- Requested clips per climate label: {per_class}",
        f"- Random seed: {seed}",
        f"- Selected clips: {total}",
        (
            "- Sampling: stratified by climate label, then by `lesson_id` "
            "within each label"
        ),
        (
            "  (one clip per lesson first where the budget allows, remaining "
            "slots proportional to lesson size)."
        ),
        f"- Clips excluded because `audio_path` is empty: {excluded_missing_audio}",
        (
            f"- Estimated audio to review: {audio_minutes:.2f} min "
            f"({total} clips x {seconds_per_clip:.2f} s per clip, from "
            "`--minutes-per-clip`)"
        ),
        "",
        "| Label | Selected |",
        "|---|---:|",
    ]
    for label in sorted(LABEL_NAMES):
        lines.append(f"| {LABEL_NAMES[label]} ({label}) | {label_counts[label]} |")
    lines.extend(
        [
            "",
            "## How to transcribe",
            "",
            "Edit `manual_transcripts.csv` in place and keep the header. Do not",
            "add, remove, reorder, or renumber rows, and keep the file in UTF-8.",
            "One row is one clip.",
            "",
            "1. Listen to the WAV file named in `audio_path` with headphones, if",
            "   available.",
            "2. Transcribe the speech **verbatim** in Simplified Chinese: write",
            "   exactly what is said, including filler words (such as 呃 and 啊),",
            "   repetitions, false starts, and colloquial forms.",
            "3. Do not correct grammar, complete unfinished sentences, summarize,",
            "   or paraphrase. If a word is unclear, write `[听不清]` instead of",
            "   guessing.",
            "4. Write numbers as Arabic numerals (for example `3个`, `2024年`) to",
            "   match the ASR normalization convention. Punctuation is optional;",
            "   it is stripped before CER is computed.",
            "5. Do not add speaker labels, role labels, timestamps, gesture",
            "   descriptions, or topic annotations. Speaker diarization is out of",
            "   scope for this evaluation.",
            "6. Leave `transcript` empty only if the clip has no intelligible",
            "   speech; otherwise fill every row.",
            "",
            "## No ASR prefill",
            "",
            "The `transcript` column is deliberately empty. Do **not** paste ASR",
            "output, machine transcription, or any model-generated text into it",
            "before listening. Reference transcripts must be independent of the",
            "ASR system under test; prefilling with ASR output would make the",
            "in-domain CER circular and invalid. Run ASR separately and store its",
            "output in a different file.",
            "",
            "## Privacy and data handling",
            "",
            "Pri-MCCD is used under its OSF data-use agreement: non-commercial",
            "research only, no re-identification. Keep this template and the",
            "completed transcripts inside the approved storage location; do not",
            "commit them to Git or share them outside the project.",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        manifest = load_manifest(args.manifest)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        sampled, excluded = sample_manifest(
            manifest, per_class=args.per_class, seed=args.seed
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if excluded:
        print(
            f"warning: excluded {excluded} clip(s) without an audio path",
            file=sys.stderr,
        )

    args.out.mkdir(parents=True, exist_ok=True)
    csv_path = args.out / TRANSCRIPT_CSV_NAME
    readme_path = args.out / README_NAME

    sampled.to_csv(csv_path, index=False, encoding="utf-8")
    readme_path.write_text(
        _render_readme(
            sampled,
            per_class=args.per_class,
            seed=args.seed,
            minutes_per_clip=args.minutes_per_clip,
            excluded_missing_audio=excluded,
            manifest_name=args.manifest.name,
        ),
        encoding="utf-8",
    )

    print(f"selected clips: {len(sampled)}")
    for label in sorted(LABEL_NAMES):
        count = int((sampled["label"] == label).sum())
        print(f"{LABEL_NAMES[label]}: {count}")
    print(f"excluded missing audio: {excluded}")
    print(f"transcripts: {csv_path}")
    print(f"instructions: {readme_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
