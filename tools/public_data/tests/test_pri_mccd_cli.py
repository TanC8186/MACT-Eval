"""CLI tests for the Pri-MCCD manifest and manual-transcription tools."""
from __future__ import annotations

import subprocess
import sys
import wave
from pathlib import Path

import pandas as pd
import pytest

from public_data.labels import LABEL_NAMES
from public_data.make_manual_transcripts import (
    OUTPUT_COLUMNS,
    README_NAME,
    TRANSCRIPT_CSV_NAME,
    main as make_main,
)
from public_data.manifest import MANIFEST_COLUMNS
from public_data.prepare_pri_mccd import main as prepare_main

PUBLIC_DATA_DIR = Path(__file__).resolve().parents[1]


def _touch(path: Path, payload: bytes = b"") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _write_wav(path: Path, seconds: float = 0.5, rate: int = 8000) -> None:
    """Write a silent PCM WAV so duration reporting can be exercised."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))


def _make_pri_tree(root: Path) -> None:
    """Two complete clips plus one clip whose WAV is missing."""
    for rel, seconds in (
        ("negative/neg0001_0", 0.5),
        ("neutral/neu0002_1", 0.5),
        ("positive/pos0003_2", None),
    ):
        _touch(root / f"{rel}.mp4")
        if seconds is not None:
            _write_wav(root / f"{rel}.wav", seconds)


def _write_manifest(
    path: Path,
    *,
    per_lesson: int = 2,
    lessons: tuple[str, ...] = ("L1", "L2", "L3"),
    missing_audio: frozenset[str] = frozenset(),
) -> Path:
    """Write a synthetic manifest CSV with every required output column."""
    rows: list[dict[str, object]] = []
    serial = 0
    for label in sorted(LABEL_NAMES):
        for lesson in lessons:
            for _ in range(per_lesson):
                serial += 1
                clip_id = f"{LABEL_NAMES[label][:3]}{serial:04d}"
                rows.append(
                    {
                        "clip_id": clip_id,
                        "label": label,
                        "label_name": LABEL_NAMES[label],
                        "video_path": str(path.parent / "video" / f"{clip_id}.mp4"),
                        "audio_path": (
                            ""
                            if clip_id in missing_audio
                            else str(path.parent / "audio" / f"{clip_id}.wav")
                        ),
                        "lesson_id": lesson,
                        "split": "unspecified",
                    }
                )
    pd.DataFrame(rows, columns=MANIFEST_COLUMNS).to_csv(path, index=False)
    return path


# ---------------------------------------------------------------------------
# prepare_pri_mccd.py
# ---------------------------------------------------------------------------


def test_prepare_cli_writes_manifest_and_reports_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "pri_mccd"
    _make_pri_tree(root)
    out = tmp_path / "processed" / "manifest.csv"

    with pytest.warns(RuntimeWarning, match="missing"):
        exit_code = prepare_main(["--root", str(root), "--out", str(out)])

    assert exit_code == 0
    stdout = capsys.readouterr().out
    assert "clips: 3" in stdout
    assert "negative: 1" in stdout
    assert "neutral: 1" in stdout
    assert "positive: 1" in stdout
    assert "missing audio: 1" in stdout
    assert "total audio duration: 1.00 s (2/3 clips readable)" in stdout

    manifest = pd.read_csv(out)
    assert list(manifest.columns) == MANIFEST_COLUMNS
    assert manifest["clip_id"].tolist() == ["neg0001", "neu0002", "pos0003"]
    assert manifest["label"].tolist() == [0, 1, 2]
    assert pd.isna(manifest.set_index("clip_id").loc["pos0003", "audio_path"])


def test_prepare_cli_uses_metadata_mapping(tmp_path: Path) -> None:
    root = tmp_path / "pri_mccd"
    _make_pri_tree(root)
    metadata = tmp_path / "mapping.csv"
    metadata.write_text(
        "clip_id,lesson_id,split\n"
        "neg0001,lesson01,train\n"
        "neu0002,lesson02,validation\n"
        "pos0003,lesson03,test\n",
        encoding="utf-8",
    )
    out = tmp_path / "manifest.csv"

    with pytest.warns(RuntimeWarning, match="missing"):
        exit_code = prepare_main(
            [
                "--root",
                str(root),
                "--out",
                str(out),
                "--metadata",
                str(metadata),
            ]
        )

    assert exit_code == 0
    rows = pd.read_csv(out).set_index("clip_id")
    assert rows.loc["neg0001", "lesson_id"] == "lesson01"
    assert rows.loc["neg0001", "split"] == "train"
    assert rows.loc["neu0002", "lesson_id"] == "lesson02"
    assert rows.loc["neu0002", "split"] == "validation"
    assert rows.loc["pos0003", "lesson_id"] == "lesson03"
    assert rows.loc["pos0003", "split"] == "test"


def test_prepare_cli_reports_unavailable_duration_for_fake_wavs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "fake_wavs"
    _touch(root / "neutral" / "neu0001_1.mp4")
    _touch(root / "neutral" / "neu0001_1.wav")
    out = tmp_path / "manifest.csv"

    assert prepare_main(["--root", str(root), "--out", str(out)]) == 0
    assert "total audio duration: unavailable" in capsys.readouterr().out


def test_prepare_cli_missing_root_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "manifest.csv"

    exit_code = prepare_main(
        ["--root", str(tmp_path / "does-not-exist"), "--out", str(out)]
    )

    assert exit_code != 0
    assert "does not exist" in capsys.readouterr().err
    assert not out.exists()


def test_prepare_cli_empty_root_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    out = tmp_path / "manifest.csv"

    exit_code = prepare_main(["--root", str(root), "--out", str(out)])

    assert exit_code != 0
    assert "No .mp4" in capsys.readouterr().err
    assert not out.exists()


def test_prepare_cli_runs_as_a_direct_script(tmp_path: Path) -> None:
    root = tmp_path / "pri_mccd"
    _make_pri_tree(root)
    out = tmp_path / "manifest.csv"

    result = subprocess.run(
        [
            sys.executable,
            str(PUBLIC_DATA_DIR / "prepare_pri_mccd.py"),
            "--root",
            str(root),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert out.exists()


# ---------------------------------------------------------------------------
# make_manual_transcripts.py
# ---------------------------------------------------------------------------


def test_make_cli_samples_per_class_and_leaves_transcripts_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = _write_manifest(tmp_path / "manifest.csv")
    out = tmp_path / "manual"

    exit_code = make_main(
        [
            "--manifest",
            str(manifest),
            "--out",
            str(out),
            "--per-class",
            "4",
            "--seed",
            "42",
        ]
    )

    assert exit_code == 0
    assert "selected clips: 12" in capsys.readouterr().out

    sampled = pd.read_csv(out / TRANSCRIPT_CSV_NAME, keep_default_na=False)
    assert list(sampled.columns) == OUTPUT_COLUMNS
    assert sampled.groupby("label").size().to_dict() == {0: 4, 1: 4, 2: 4}
    assert (sampled["transcript"] == "").all()
    assert (out / README_NAME).exists()


def test_make_cli_stratifies_by_lesson_within_label(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path / "manifest.csv")
    out = tmp_path / "manual"

    assert (
        make_main(
            [
                "--manifest",
                str(manifest),
                "--out",
                str(out),
                "--per-class",
                "3",
                "--seed",
                "42",
            ]
        )
        == 0
    )

    sampled = pd.read_csv(out / TRANSCRIPT_CSV_NAME, keep_default_na=False)
    for label in sorted(LABEL_NAMES):
        counts = (
            sampled[sampled["label"] == label]
            .groupby("lesson_id")
            .size()
            .to_dict()
        )
        assert counts == {"L1": 1, "L2": 1, "L3": 1}


def test_make_cli_is_deterministic_for_a_fixed_seed(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path / "manifest.csv")
    first = tmp_path / "first"
    second = tmp_path / "second"
    args = ["--manifest", str(manifest), "--per-class", "5", "--seed", "42"]

    assert make_main([*args, "--out", str(first)]) == 0
    assert make_main([*args, "--out", str(second)]) == 0

    first_frame = pd.read_csv(first / TRANSCRIPT_CSV_NAME, keep_default_na=False)
    second_frame = pd.read_csv(second / TRANSCRIPT_CSV_NAME, keep_default_na=False)
    assert first_frame.equals(second_frame)
    assert (first / README_NAME).read_text(
        encoding="utf-8"
    ) == (second / README_NAME).read_text(encoding="utf-8")


def test_make_cli_different_seed_changes_the_sample(tmp_path: Path) -> None:
    manifest = _write_manifest(
        tmp_path / "manifest.csv", per_lesson=5, lessons=("L1", "L2", "L3", "L4")
    )
    first = tmp_path / "seed42"
    second = tmp_path / "seed7"

    assert (
        make_main(
            [
                "--manifest",
                str(manifest),
                "--out",
                str(first),
                "--per-class",
                "5",
                "--seed",
                "42",
            ]
        )
        == 0
    )
    assert (
        make_main(
            [
                "--manifest",
                str(manifest),
                "--out",
                str(second),
                "--per-class",
                "5",
                "--seed",
                "7",
            ]
        )
        == 0
    )

    first_ids = pd.read_csv(first / TRANSCRIPT_CSV_NAME, keep_default_na=False)[
        "clip_id"
    ].tolist()
    second_ids = pd.read_csv(second / TRANSCRIPT_CSV_NAME, keep_default_na=False)[
        "clip_id"
    ].tolist()
    assert first_ids != second_ids


def test_make_cli_excludes_clips_without_audio(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = "neg0001"
    manifest = _write_manifest(
        tmp_path / "manifest.csv",
        lessons=("L1",),
        missing_audio=frozenset({missing}),
    )
    out = tmp_path / "manual"

    assert (
        make_main(
            [
                "--manifest",
                str(manifest),
                "--out",
                str(out),
                "--per-class",
                "10",
                "--seed",
                "42",
            ]
        )
        == 0
    )

    assert "excluded 1 clip(s) without an audio path" in capsys.readouterr().err
    sampled = pd.read_csv(out / TRANSCRIPT_CSV_NAME, keep_default_na=False)
    assert missing not in sampled["clip_id"].tolist()
    assert sampled.groupby("label").size().to_dict() == {0: 1, 1: 2, 2: 2}


def test_make_cli_readme_contains_verbatim_instructions_and_no_asr_prefill(
    tmp_path: Path,
) -> None:
    manifest = _write_manifest(tmp_path / "manifest.csv")
    out = tmp_path / "manual"

    assert (
        make_main(
            [
                "--manifest",
                str(manifest),
                "--out",
                str(out),
                "--per-class",
                "4",
                "--seed",
                "42",
                "--minutes-per-clip",
                "5/60",
            ]
        )
        == 0
    )

    readme = (out / README_NAME).read_text(encoding="utf-8")
    lowered = readme.lower()
    assert "verbatim" in lowered
    assert "no asr prefill" in lowered
    assert "do **not** paste asr" in lowered
    assert TRANSCRIPT_CSV_NAME in readme
    assert "Random seed: 42" in readme
    assert "Estimated audio to review: 1.00 min" in readme
    assert "[听不清]" in readme


def test_make_cli_missing_manifest_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "manual"

    exit_code = make_main(
        ["--manifest", str(tmp_path / "missing.csv"), "--out", str(out)]
    )

    assert exit_code != 0
    assert "does not exist" in capsys.readouterr().err
    assert not (out / TRANSCRIPT_CSV_NAME).exists()


def test_make_cli_rejects_empty_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = tmp_path / "manifest.csv"
    pd.DataFrame(
        columns=["clip_id", "audio_path", "label", "lesson_id"]
    ).to_csv(manifest, index=False)
    out = tmp_path / "manual"

    exit_code = make_main(["--manifest", str(manifest), "--out", str(out)])

    assert exit_code != 0
    assert "contains no clips" in capsys.readouterr().err


def test_make_cli_fails_when_no_clip_has_audio(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = tmp_path / "manifest.csv"
    pd.DataFrame(
        [
            {
                "clip_id": "neg0001",
                "label": 0,
                "audio_path": "",
                "lesson_id": "L1",
            },
            {
                "clip_id": "neu0002",
                "label": 1,
                "audio_path": "",
                "lesson_id": "L1",
            },
        ]
    ).to_csv(manifest, index=False)
    out = tmp_path / "manual"

    exit_code = make_main(["--manifest", str(manifest), "--out", str(out)])

    assert exit_code != 0
    assert "No clips with a non-empty audio_path" in capsys.readouterr().err
    assert not (out / TRANSCRIPT_CSV_NAME).exists()
