"""Tests for SCB-Dataset3-S preparation and the YOLO training CLI.

Everything here works on synthetic archives; ``ultralytics`` is never
imported, so the suite also runs in environments without the heavy training
dependencies.
"""
from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest
import yaml

from public_data.prepare_scb import main as prepare_main
from public_data.scb import (
    CLASS_NAMES,
    _replace_with_retry,
    prepare_scb,
    validate_layout,
    write_data_yaml,
)
from public_data.train_scb_yolo import build_parser as build_train_parser
from public_data.train_scb_yolo import main as train_main

PREFIX = "5k_HRW_yolo_Dataset_jpg"


def _write_zip(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return path


def _image_bytes(stem: str) -> bytes:
    # The preparer copies bytes without decoding them; JPEG validity is not
    # required to test layout normalization.
    return b"\xff\xd8\xff\xe0" + stem.encode("ascii")


def _separate_archives(tmp_path: Path) -> tuple[Path, Path]:
    images_zip = _write_zip(
        tmp_path / "images.zip",
        {
            f"{PREFIX}/images/train/img_a.jpg": _image_bytes("a"),
            f"{PREFIX}/images/val/img_b.jpg": _image_bytes("b"),
        },
    )
    labels_zip = _write_zip(
        tmp_path / "labels.zip",
        {
            f"{PREFIX}/labels/train/img_a.txt": (
                b"0 0.5 0.5 0.2 0.2\n1 0.3 0.3 0.1 0.1\n"
            ),
            f"{PREFIX}/labels/val/img_b.txt": b"2 0.4 0.4 0.1 0.1\n",
        },
    )
    return images_zip, labels_zip


def _combined_archive(tmp_path: Path, *, extra_val: bool = False) -> Path:
    entries = {
        f"{PREFIX}/images/train/img_a.jpg": _image_bytes("a"),
        f"{PREFIX}/images/val/img_b.jpg": _image_bytes("b"),
        f"{PREFIX}/labels/train/img_a.txt": b"0 0.5 0.5 0.2 0.2\n",
        f"{PREFIX}/labels/val/img_b.txt": b"2 0.4 0.4 0.1 0.1\n",
    }
    if extra_val:
        entries[f"{PREFIX}/images/val/img_c.jpg"] = _image_bytes("c")
        entries[f"{PREFIX}/labels/val/img_c.txt"] = b"1 0.6 0.6 0.2 0.2\n"
    return _write_zip(tmp_path / "scb-combined.zip", entries)


def test_prepare_separate_archives_normalizes_layout(tmp_path: Path) -> None:
    images_zip, labels_zip = _separate_archives(tmp_path)
    out = tmp_path / "scb"

    summary = prepare_scb(images_zip, labels_zip, out)

    assert summary["skipped"] is False
    assert summary["extracted"] is True
    assert (out / "images/train/img_a.jpg").read_bytes() == _image_bytes("a")
    assert (out / "images/val/img_b.jpg").read_bytes() == _image_bytes("b")
    assert (out / "labels/train/img_a.txt").read_text(encoding="utf-8") == (
        "0 0.5 0.5 0.2 0.2\n1 0.3 0.3 0.1 0.1\n"
    )

    train = summary["splits"]["train"]
    val = summary["splits"]["val"]
    assert (train["images"], train["labels"], train["boxes"]) == (1, 1, 2)
    assert (val["images"], val["labels"], val["boxes"]) == (1, 1, 1)
    assert train["class_counts"] == {
        "hand-raising": 1,
        "reading": 1,
        "writing": 0,
    }
    assert val["class_counts"] == {
        "hand-raising": 0,
        "reading": 0,
        "writing": 1,
    }
    assert summary["totals"]["images"] == 2
    assert summary["totals"]["boxes"] == 3


def test_prepare_combined_archive_reads_labels_from_images_zip(
    tmp_path: Path,
) -> None:
    archive = _combined_archive(tmp_path)
    out = tmp_path / "scb"

    summary = prepare_scb(images_zip=archive, labels_zip=None, out_root=out)

    assert summary["totals"]["images"] == 2
    assert (out / "labels/train/img_a.txt").is_file()
    assert summary["sources"]["images_zip"] == str(archive.resolve())
    assert summary["sources"]["labels_zip"] == str(archive.resolve())


def test_data_yaml_content_matches_ultralytics_layout(tmp_path: Path) -> None:
    images_zip, labels_zip = _separate_archives(tmp_path)
    out = tmp_path / "scb"
    prepare_scb(images_zip, labels_zip, out)

    document = yaml.safe_load((out / "data.yaml").read_text(encoding="utf-8"))

    assert document["path"] == out.resolve().as_posix()
    assert document["train"] == "images/train"
    assert document["val"] == "images/val"
    assert document["names"] == {
        0: "hand-raising",
        1: "reading",
        2: "writing",
    }
    assert set(document["names"]) == set(CLASS_NAMES)


def test_prepare_is_idempotent_and_force_rebuilds(tmp_path: Path) -> None:
    archive = _combined_archive(tmp_path)
    out = tmp_path / "scb"
    first = prepare_scb(archive, None, out)
    assert first["skipped"] is False

    notes = out / "notes.txt"
    notes.write_text("preserve me", encoding="utf-8")
    original = (out / "images/train/img_a.jpg").read_bytes()

    second = prepare_scb(archive, None, out)
    assert second["skipped"] is True
    assert second["extracted"] is False
    assert second["totals"]["images"] == 2
    assert (out / "images/train/img_a.jpg").read_bytes() == original

    larger = _combined_archive(tmp_path, extra_val=True)
    rebuilt = prepare_scb(larger, None, out, force=True)
    assert rebuilt["skipped"] is False
    assert rebuilt["totals"]["images"] == 3
    assert (out / "images/val/img_c.jpg").is_file()
    assert notes.read_text(encoding="utf-8") == "preserve me"


def test_prepare_rejects_class_id_outside_range(tmp_path: Path) -> None:
    images_zip, _ = _separate_archives(tmp_path)
    labels_zip = _write_zip(
        tmp_path / "bad-labels.zip",
        {
            f"{PREFIX}/labels/train/img_a.txt": b"3 0.5 0.5 0.2 0.2\n",
            f"{PREFIX}/labels/val/img_b.txt": b"2 0.4 0.4 0.1 0.1\n",
        },
    )
    out = tmp_path / "scb"

    with pytest.raises(ValueError, match="class id 3"):
        prepare_scb(images_zip, labels_zip, out)

    assert not (out / "images").exists()
    assert not (out / "labels").exists()


@pytest.mark.parametrize(
    ("label_bytes", "message"),
    [
        (b"0 0.5 0.5 0.2\n", "expected 5 fields"),
        (b"x 0.5 0.5 0.2 0.2\n", "not an integer"),
        (b"0 0.5 0.5 nan 0.2\n", "non-finite"),
        (b"\xff\xfe\x00", "not valid UTF-8"),
    ],
)
def test_prepare_rejects_malformed_labels(
    tmp_path: Path, label_bytes: bytes, message: str
) -> None:
    images_zip, _ = _separate_archives(tmp_path)
    labels_zip = _write_zip(
        tmp_path / "bad-labels.zip",
        {
            f"{PREFIX}/labels/train/img_a.txt": label_bytes,
            f"{PREFIX}/labels/val/img_b.txt": b"2 0.4 0.4 0.1 0.1\n",
        },
    )

    with pytest.raises(ValueError, match=message):
        prepare_scb(images_zip, labels_zip, tmp_path / "scb")


def test_prepare_rejects_image_label_count_mismatch(tmp_path: Path) -> None:
    images_zip, _ = _separate_archives(tmp_path)
    labels_zip = _write_zip(
        tmp_path / "missing-val-label.zip",
        {f"{PREFIX}/labels/train/img_a.txt": b"0 0.5 0.5 0.2 0.2\n"},
    )

    with pytest.raises(ValueError, match="without a label"):
        prepare_scb(images_zip, labels_zip, tmp_path / "scb")


@pytest.mark.parametrize("unsafe_name", ["../evil.jpg", "..\\evil.jpg"])
def test_prepare_rejects_path_traversal(
    tmp_path: Path, unsafe_name: str
) -> None:
    images_zip = _write_zip(
        tmp_path / "unsafe.zip",
        {
            unsafe_name: _image_bytes("evil"),
            f"{PREFIX}/images/train/img_a.jpg": _image_bytes("a"),
            f"{PREFIX}/labels/train/img_a.txt": b"0 0.5 0.5 0.2 0.2\n",
            f"{PREFIX}/images/val/img_b.jpg": _image_bytes("b"),
            f"{PREFIX}/labels/val/img_b.txt": b"2 0.4 0.4 0.1 0.1\n",
        },
    )

    with pytest.raises(ValueError, match="traversal"):
        prepare_scb(images_zip, None, tmp_path / "scb")

    assert not (tmp_path / "evil.jpg").exists()
    assert not (tmp_path.parent / "evil.jpg").exists()


def test_prepare_requires_force_for_incomplete_existing_output(
    tmp_path: Path,
) -> None:
    archive = _combined_archive(tmp_path)
    out = tmp_path / "scb"
    out.mkdir()
    keep = out / "random.txt"
    keep.write_text("keep", encoding="utf-8")

    with pytest.raises(FileExistsError, match="force=True"):
        prepare_scb(archive, None, out)

    summary = prepare_scb(archive, None, out, force=True)
    assert summary["skipped"] is False
    assert keep.read_text(encoding="utf-8") == "keep"
    assert (out / "images/train/img_a.jpg").is_file()


def test_validate_layout_detects_missing_label(tmp_path: Path) -> None:
    archive = _combined_archive(tmp_path)
    out = tmp_path / "scb"
    prepare_scb(archive, None, out)

    (out / "labels/val/img_b.txt").unlink()

    with pytest.raises(ValueError, match="without a label"):
        validate_layout(out)


def test_empty_label_file_counts_as_background(tmp_path: Path) -> None:
    images_zip, _ = _separate_archives(tmp_path)
    labels_zip = _write_zip(
        tmp_path / "empty-label.zip",
        {
            f"{PREFIX}/labels/train/img_a.txt": b"",
            f"{PREFIX}/labels/val/img_b.txt": b"2 0.4 0.4 0.1 0.1\n",
        },
    )

    summary = prepare_scb(images_zip, labels_zip, tmp_path / "scb")

    assert summary["splits"]["train"]["boxes"] == 0
    assert summary["splits"]["train"]["empty_labels"] == 1


def test_write_data_yaml_accepts_sequence(tmp_path: Path) -> None:
    path = write_data_yaml(tmp_path / "custom", ["alpha", "beta", "gamma"])

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert document["names"] == {0: "alpha", 1: "beta", 2: "gamma"}


def test_prepare_cli_reports_json_and_skips_on_second_run(
    tmp_path: Path, capsys
) -> None:
    images_zip, labels_zip = _separate_archives(tmp_path)
    out = tmp_path / "scb"

    assert (
        prepare_main(
            [
                "--images-zip",
                str(images_zip),
                "--labels-zip",
                str(labels_zip),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    first = json.loads(capsys.readouterr().out)
    assert first["skipped"] is False
    assert first["totals"]["images"] == 2

    assert (
        prepare_main(
            [
                "--images-zip",
                str(images_zip),
                "--labels-zip",
                str(labels_zip),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    second = json.loads(capsys.readouterr().out)
    assert second["skipped"] is True


def test_prepare_cli_returns_nonzero_for_bad_zip(
    tmp_path: Path, capsys
) -> None:
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")

    assert (
        prepare_main(
            ["--images-zip", str(bad), "--out", str(tmp_path / "scb")]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert "ERROR" in captured.err
    assert not (tmp_path / "scb/images").exists()


def test_train_cli_defaults_match_experiment_protocol() -> None:
    args = build_train_parser().parse_args([])

    assert args.data == Path("data/raw/public/scb/data.yaml")
    assert args.model == "yolov8n.pt"
    assert args.epochs == 100
    assert args.imgsz == 640
    assert args.batch == 16
    assert args.device == "auto"
    assert args.out == Path("docs/experiments/runs/scb-yolo")
    assert args.seed == 42


def test_train_cli_writes_failed_metrics_and_exits_nonzero(
    tmp_path: Path,
) -> None:
    out = tmp_path / "run"

    exit_code = train_main(
        ["--data", str(tmp_path / "missing.yaml"), "--out", str(out)]
    )

    assert exit_code != 0
    record = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    assert record["status"] == "FAILED"
    assert "error" in record
    assert "traceback" in record
    assert "metrics" not in record


def test_prepare_retries_transient_permission_error_on_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``WinError 5`` on the staging rename must not fail the prepare.

    Windows antivirus and the search indexer briefly hold handles on freshly
    written directories, which makes the staging rename fail intermittently.
    """
    archive = _combined_archive(tmp_path)
    out = tmp_path / "scb"
    real_replace = os.replace
    calls: list[tuple[str, str]] = []

    def flaky_replace(src, dst):
        calls.append((str(src), str(dst)))
        if len(calls) <= 2:
            raise PermissionError(13, "fake", str(src), str(dst), None)
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)

    summary = prepare_scb(archive, None, out)

    assert len(calls) > 2
    assert summary["skipped"] is False
    assert summary["totals"]["images"] == 2
    assert (out / "images/train/img_a.jpg").read_bytes() == _image_bytes("a")
    assert (out / "images/val/img_b.jpg").read_bytes() == _image_bytes("b")
    assert (out / "labels/train/img_a.txt").is_file()
    assert (out / "labels/val/img_b.txt").is_file()
    assert (out / "data.yaml").is_file()
    validate_layout(out)


def test_replace_with_retry_reraises_after_exhausting_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persistent sharing violation is a real failure and must surface."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    calls: list[tuple[str, str]] = []

    def always_denied(path_a, path_b):
        calls.append((str(path_a), str(path_b)))
        raise PermissionError(13, "fake", str(path_a), str(path_b), None)

    monkeypatch.setattr(os, "replace", always_denied)

    with pytest.raises(PermissionError) as excinfo:
        _replace_with_retry(src, dst, attempts=3, base_delay=0.001)

    assert len(calls) == 3
    message = str(excinfo.value)
    assert str(src) in message
    assert str(dst) in message
    assert "3 attempts" in message
    assert not dst.exists()
