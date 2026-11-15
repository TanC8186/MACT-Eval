"""SCB-Dataset3-S preparation: safe extraction, normalization and validation.

The official archive (``5k_HRW_yolo_Dataset_jpg.zip``) contains one prefix
directory with both images and labels::

    <prefix>/
        images/train/*.jpg
        images/val/*.jpg
        labels/train/*.txt
        labels/val/*.txt

Some mirrors distribute images and labels in separate archives; both layouts
are accepted.  This module never extracts member paths verbatim.  Every archive
member is checked for absolute paths, ``..`` traversal and symlinks, and only
the three expected components ``images|labels / train|val / <filename>`` are
copied to a normalized output tree.

Normalized output::

    <out_root>/
        images/train/*.jpg
        images/val/*.jpg
        labels/train/*.txt
        labels/val/*.txt
        data.yaml

Validation checks that every split has the same set of image/label stems and
that every label line has five fields with a class id in ``0..2``.  Coordinate
ranges are deliberately *not* enforced: the official SCB release contains
boxes whose normalized x coordinate exceeds 1 (max ≈ 1.146), and rejecting
them would reject valid official data.

``prepare_scb`` is idempotent: if ``out_root`` already holds a complete valid
layout it is left untouched (only a missing/incorrect ``data.yaml`` is
rewritten).  Rebuilding requires ``force=True`` and only removes
``images/``, ``labels/`` and ``data.yaml`` — unrelated files are preserved.
"""
from __future__ import annotations

import math
import os
import re
import shutil
import stat
import time
import uuid
import zipfile
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import yaml

#: Official SCB-Dataset3-S class names, indexed by YOLO class id.
CLASS_NAMES: dict[int, str] = {
    0: "hand-raising",
    1: "reading",
    2: "writing",
}

SPLITS: tuple[str, ...] = ("train", "val")

DATA_YAML_NAME = "data.yaml"

IMAGE_SUFFIXES: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
)

_DRIVE_RE = re.compile(r"^[A-Za-z]:")

#: Windows error codes that mean "someone else holds a handle to this path".
#: Antivirus scanners, the search indexer and Explorer previews open freshly
#: written files and directories for a few milliseconds after they appear, so
#: an otherwise valid rename fails with ERROR_ACCESS_DENIED (5) or
#: ERROR_SHARING_VIOLATION (32) and succeeds when retried shortly after.
_TRANSIENT_WINERRORS: frozenset[int] = frozenset({5, 32})


def _replace_with_retry(
    src: str | Path,
    dst: str | Path,
    *,
    attempts: int = 8,
    base_delay: float = 0.05,
) -> None:
    """``os.replace`` that tolerates transient Windows sharing violations.

    The retry only covers errors that go away on their own (a scanner or
    indexer holding a handle on ``src``/``dst``).  Every other ``OSError`` is
    raised immediately, and a persistent sharing violation is re-raised after
    ``attempts`` tries with both paths in the message so the failure is
    diagnosable.
    """
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1, got {attempts!r}")

    delay = base_delay
    for attempt in range(1, attempts + 1):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)
            transient = isinstance(exc, PermissionError) or (
                winerror is not None and winerror in _TRANSIENT_WINERRORS
            )
            if not transient or attempt == attempts:
                if transient:
                    raise PermissionError(
                        exc.errno,
                        f"{exc.strerror} (gave up after {attempts} attempts "
                        f"renaming {src!s} -> {dst!s})",
                        str(src),
                        str(dst),
                    ) from exc
                raise
            time.sleep(delay)
            delay *= 2


def prepare_scb(
    images_zip: str | Path,
    labels_zip: str | Path | None = None,
    out_root: str | Path | None = None,
    *,
    force: bool = False,
) -> dict:
    """Normalize an SCB-Dataset3-S archive pair into ``out_root``.

    Args:
        images_zip: Archive containing ``images/{train,val}``.  It may also
            contain ``labels/{train,val}`` (the official layout).
        labels_zip: Optional separate labels archive.  When ``None`` labels
            are read from ``images_zip``.
        out_root: Destination directory.  Required.
        force: Rebuild ``images/``, ``labels/`` and ``data.yaml`` even when a
            valid layout already exists.

    Returns:
        A JSON-serializable summary with per-split image/label/box counts,
        per-class instance counts and the ``data.yaml`` path.

    Raises:
        FileNotFoundError: an input archive or output directory is missing.
        FileExistsError: ``out_root`` exists but is not a complete layout and
            ``force`` is false.
        ValueError: an archive is unsafe/malformed, counts do not match, or a
            label class id is outside ``0..2``.
        zipfile.BadZipFile: an input is not a valid zip archive.
    """
    if out_root is None:
        raise ValueError("out_root is required")

    images_zip = Path(images_zip)
    labels_zip_path = images_zip if labels_zip is None else Path(labels_zip)
    out = Path(out_root)

    _require_zip(images_zip)
    _require_zip(labels_zip_path)

    if out.exists() and not out.is_dir():
        raise ValueError(f"SCB output path is not a directory: {out}")

    if out.exists() and not force:
        has_content = any(out.iterdir())
        if has_content:
            try:
                summary = validate_layout(out)
            except (FileNotFoundError, ValueError) as exc:
                raise FileExistsError(
                    f"Output {out} exists but is not a complete SCB layout "
                    f"({exc}); pass force=True to rebuild"
                ) from exc

            data_yaml_path = out / DATA_YAML_NAME
            data_yaml_written = False
            if not _data_yaml_matches(data_yaml_path, out):
                data_yaml_path = write_data_yaml(out)
                data_yaml_written = True

            summary.update(
                {
                    "out_root": str(out.resolve()),
                    "data_yaml": str(data_yaml_path.resolve()),
                    "skipped": True,
                    "extracted": False,
                    "data_yaml_written": data_yaml_written,
                    "sources": _sources(images_zip, labels_zip_path),
                }
            )
            return summary

    image_members = _scan_archive(images_zip, roles=frozenset({"images"}))
    labels_from_images = labels_zip_path == images_zip
    label_members = _scan_archive(
        images_zip if labels_from_images else labels_zip_path,
        roles=frozenset({"labels"}),
    )

    if not image_members["images"]:
        raise ValueError(
            f"No SCB images found in {images_zip}; expected members under "
            "images/train/ and images/val/"
        )
    if not label_members["labels"]:
        raise ValueError(
            f"No SCB labels found in {labels_zip_path}; expected members "
            "under labels/train/ and labels/val/ (pass --labels-zip if labels "
            "are distributed separately)"
        )

    stats = _validate_dataset(
        image_members=image_members["images"],
        label_members=label_members["labels"],
        labels_archive=images_zip if labels_from_images else labels_zip_path,
    )

    _stage_and_install(
        out,
        image_members=image_members["images"],
        label_members=label_members["labels"],
        images_archive=images_zip,
        labels_archive=images_zip if labels_from_images else labels_zip_path,
    )

    data_yaml_path = write_data_yaml(out)

    return _summary(
        out=out,
        splits=stats,
        data_yaml_path=data_yaml_path,
        sources=_sources(images_zip, labels_zip_path),
        skipped=False,
        extracted=True,
        data_yaml_written=True,
    )


def validate_layout(out_root: str | Path) -> dict:
    """Validate an already-normalized SCB tree and return its counts.

    Raises:
        FileNotFoundError: a required split directory is missing.
        ValueError: image/label stems differ, a duplicate stem exists, or a
            label file is malformed.
    """
    out = Path(out_root)
    if not out.exists():
        raise FileNotFoundError(f"SCB output directory does not exist: {out}")

    splits: dict[str, dict] = {}
    for split in SPLITS:
        images_dir = out / "images" / split
        labels_dir = out / "labels" / split
        if not images_dir.is_dir():
            raise FileNotFoundError(f"Missing SCB image directory: {images_dir}")
        if not labels_dir.is_dir():
            raise FileNotFoundError(f"Missing SCB label directory: {labels_dir}")

        images = _index_files(images_dir, IMAGE_SUFFIXES)
        labels = _index_files(labels_dir, frozenset({".txt"}))

        missing_labels = sorted(set(images) - set(labels))
        missing_images = sorted(set(labels) - set(images))
        if missing_labels or missing_images:
            raise ValueError(
                f"SCB split {split!r} image/label mismatch: "
                f"{len(missing_labels)} image(s) without a label"
                f"{_preview(missing_labels)}, "
                f"{len(missing_images)} label(s) without an image"
                f"{_preview(missing_images)}"
            )
        if not images:
            raise ValueError(f"SCB split {split!r} contains no images")

        split_stats = _empty_split_stats(len(images), len(labels))
        for label_path in labels.values():
            data = label_path.read_bytes()
            boxes = _validate_label_file(str(label_path), data)
            split_stats["boxes"] += boxes
            if boxes == 0:
                split_stats["empty_labels"] += 1
            _count_classes(
                split_stats["class_counts"],
                data.decode("utf-8-sig").splitlines(),
            )
        splits[split] = split_stats

    return _summary(
        out=out,
        splits=splits,
        data_yaml_path=out / DATA_YAML_NAME,
        sources={},
        skipped=True,
        extracted=False,
        data_yaml_written=False,
    )


def write_data_yaml(
    out_root: str | Path,
    class_names: Mapping[int, str] | Sequence[str] = CLASS_NAMES,
) -> Path:
    """Write the Ultralytics ``data.yaml`` for a normalized SCB tree.

    ``path`` is stored as an absolute POSIX-style path so the same file works
    on Windows and Linux; ``train``/``val`` stay relative to it.
    """
    out = Path(out_root)
    out.mkdir(parents=True, exist_ok=True)
    names = _normalize_class_names(class_names)

    document = {
        "path": out.resolve().as_posix(),
        "train": "images/train",
        "val": "images/val",
        "names": names,
    }
    text = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)

    path = out / DATA_YAML_NAME
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8", newline="\n")
        _replace_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def _require_zip(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"SCB archive does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"SCB archive is not a file: {path}")
    if not zipfile.is_zipfile(path):
        raise zipfile.BadZipFile(f"Not a valid zip archive: {path}")


def _scan_archive(path: Path, *, roles: frozenset[str]) -> dict[str, dict]:
    """Index safe ``images|labels/<split>/<file>`` members by split and stem.

    Every member name in the archive is safety-checked, even when its role is
    not requested, so an archive containing traversal entries is rejected
    outright instead of silently ignored.
    """
    indexed: dict[str, dict[tuple[str, str], tuple[str, str]]] = {
        "images": {},
        "labels": {},
    }

    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            parts = _safe_member_parts(info)
            classified = _classify_parts(parts)
            if classified is None:
                continue
            role, split, filename = classified
            if role not in roles:
                continue

            suffix = Path(filename).suffix.lower()
            if role == "images" and suffix not in IMAGE_SUFFIXES:
                continue
            if role == "labels" and suffix != ".txt":
                continue

            stem = Path(filename).stem
            key = (split, stem)
            if key in indexed[role]:
                previous = indexed[role][key][0]
                raise ValueError(
                    f"Duplicate {role[:-1]} for split {split!r} and stem "
                    f"{stem!r} in {path}: {previous!r} and {filename!r}"
                )
            indexed[role][key] = (filename, info.filename)

    return indexed


def _safe_member_parts(info: zipfile.ZipInfo) -> tuple[str, ...]:
    """Return normalized path components or raise for an unsafe member."""
    name = info.filename
    if "\x00" in name:
        raise ValueError(f"Unsafe archive member (NUL byte): {name!r}")

    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or _DRIVE_RE.match(normalized):
        raise ValueError(f"Unsafe archive member (absolute path): {name!r}")

    parts = tuple(part for part in normalized.split("/") if part not in ("", "."))
    if any(part == ".." for part in parts):
        raise ValueError(f"Unsafe archive member (path traversal): {name!r}")

    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise ValueError(f"Unsafe archive member (symbolic link): {name!r}")

    return parts


def _classify_parts(parts: tuple[str, ...]) -> tuple[str, str, str] | None:
    """Return ``(role, split, filename)`` for a dataset member, else ``None``."""
    if len(parts) < 3:
        return None
    role, split, filename = parts[-3], parts[-2], parts[-1]
    if role not in ("images", "labels") or split not in SPLITS or not filename:
        return None
    return role, split, filename


def _validate_dataset(
    *,
    image_members: Mapping[tuple[str, str], tuple[str, str]],
    label_members: Mapping[tuple[str, str], tuple[str, str]],
    labels_archive: Path,
) -> dict[str, dict]:
    """Check stem/count agreement and validate every label file."""
    splits: dict[str, dict] = {}

    for split in SPLITS:
        images = {stem for (member_split, stem) in image_members if member_split == split}
        labels = {stem for (member_split, stem) in label_members if member_split == split}

        missing_labels = sorted(images - labels)
        missing_images = sorted(labels - images)
        if missing_labels or missing_images:
            raise ValueError(
                f"SCB split {split!r} image/label mismatch in archives: "
                f"{len(missing_labels)} image(s) without a label"
                f"{_preview(missing_labels)}, "
                f"{len(missing_images)} label(s) without an image"
                f"{_preview(missing_images)}"
            )
        if not images:
            raise ValueError(
                f"SCB split {split!r} contains no images; expected "
                f"images/{split}/ members"
            )

        splits[split] = _empty_split_stats(len(images), len(labels))

    with zipfile.ZipFile(labels_archive) as archive:
        for (split, _stem), (_filename, member_name) in label_members.items():
            data = archive.read(member_name)
            boxes = _validate_label_file(member_name, data)
            stats = splits[split]
            stats["boxes"] += boxes
            if boxes == 0:
                stats["empty_labels"] += 1
            _count_classes(
                stats["class_counts"],
                data.decode("utf-8-sig").splitlines(),
            )

    return splits


def _validate_label_file(name: str, data: bytes) -> int:
    """Validate one YOLO label file and return its box count."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Label file {name} is not valid UTF-8: {exc}") from exc

    boxes = 0
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        fields = stripped.split()
        if len(fields) != 5:
            raise ValueError(
                f"{name}:{line_number}: expected 5 fields "
                f"(class x y w h), found {len(fields)}"
            )
        try:
            class_id = int(fields[0])
        except ValueError as exc:
            raise ValueError(
                f"{name}:{line_number}: class id {fields[0]!r} is not an integer"
            ) from exc
        if class_id not in CLASS_NAMES:
            raise ValueError(
                f"{name}:{line_number}: class id {class_id} outside the "
                f"supported range 0..2 ({CLASS_NAMES})"
            )
        try:
            coordinates = [float(value) for value in fields[1:]]
        except ValueError as exc:
            raise ValueError(
                f"{name}:{line_number}: non-numeric coordinates {fields[1:]!r}"
            ) from exc
        if not all(math.isfinite(value) for value in coordinates):
            raise ValueError(
                f"{name}:{line_number}: non-finite coordinates {coordinates!r}"
            )
        boxes += 1
    return boxes


def _count_classes(
    counts: dict[str, int], lines: Iterable[str]
) -> None:
    for line in lines:
        fields = line.split()
        if not fields:
            continue
        counts[CLASS_NAMES[int(fields[0])]] += 1


def _index_files(directory: Path, suffixes: frozenset[str]) -> dict[str, Path]:
    index: dict[str, Path] = {}
    for path in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        stem = path.stem
        if stem in index:
            raise ValueError(
                f"Duplicate stem {stem!r} in {directory}: "
                f"{index[stem].name!r} and {path.name!r}"
            )
        index[stem] = path
    return index


def _empty_split_stats(image_count: int, label_count: int) -> dict:
    return {
        "images": image_count,
        "labels": label_count,
        "boxes": 0,
        "empty_labels": 0,
        "class_counts": {name: 0 for name in CLASS_NAMES.values()},
    }


def _stage_and_install(
    out: Path,
    *,
    image_members: Mapping[tuple[str, str], tuple[str, str]],
    label_members: Mapping[tuple[str, str], tuple[str, str]],
    images_archive: Path,
    labels_archive: Path,
) -> None:
    """Extract into a staging directory, then swap the dataset directories in.

    Validation happens before this function is called, so a malformed input
    never destroys an existing valid layout.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = out.parent / f".{out.name}.prepare-{uuid.uuid4().hex}"

    try:
        _extract_members(images_archive, image_members, staging / "images")
        _extract_members(labels_archive, label_members, staging / "labels")

        if out.exists():
            _remove_dataset_dirs(out)
        else:
            out.mkdir(parents=True)

        _replace_with_retry(staging / "images", out / "images")
        _replace_with_retry(staging / "labels", out / "labels")
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _extract_members(
    archive_path: Path,
    members: Mapping[tuple[str, str], tuple[str, str]],
    destination: Path,
) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        for (split, _stem), (filename, member_name) in members.items():
            target = destination / split / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member_name) as source, target.open("wb") as sink:
                shutil.copyfileobj(source, sink)


def _remove_dataset_dirs(out: Path) -> None:
    """Remove only the managed dataset paths; leave unrelated files alone."""
    for name in ("images", "labels"):
        target = out / name
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    data_yaml = out / DATA_YAML_NAME
    if data_yaml.exists():
        data_yaml.unlink()


def _data_yaml_matches(path: Path, out: Path) -> bool:
    if not path.is_file():
        return False
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return False
    if not isinstance(document, dict):
        return False
    if document.get("train") != "images/train":
        return False
    if document.get("val") != "images/val":
        return False
    try:
        if Path(str(document.get("path"))).resolve() != out.resolve():
            return False
    except (OSError, ValueError):
        return False
    try:
        names = _normalize_class_names(document.get("names", {}))
    except (TypeError, ValueError):
        return False
    return names == CLASS_NAMES


def _normalize_class_names(
    class_names: Mapping[int, str] | Sequence[str],
) -> dict[int, str]:
    if isinstance(class_names, Mapping):
        normalized = {int(key): str(value) for key, value in class_names.items()}
    else:
        normalized = {
            index: str(value) for index, value in enumerate(class_names)
        }
    if not normalized:
        raise ValueError("class_names must not be empty")
    return normalized


def _sources(images_zip: Path, labels_zip: Path) -> dict[str, str]:
    return {
        "images_zip": str(images_zip.resolve()),
        "labels_zip": str(labels_zip.resolve()),
    }


def _summary(
    *,
    out: Path,
    splits: Mapping[str, Mapping[str, object]],
    data_yaml_path: Path,
    sources: Mapping[str, str],
    skipped: bool,
    extracted: bool,
    data_yaml_written: bool,
) -> dict:
    totals = {
        "images": sum(int(stats["images"]) for stats in splits.values()),
        "labels": sum(int(stats["labels"]) for stats in splits.values()),
        "boxes": sum(int(stats["boxes"]) for stats in splits.values()),
        "empty_labels": sum(
            int(stats["empty_labels"]) for stats in splits.values()
        ),
        "class_counts": {
            name: sum(int(stats["class_counts"][name]) for stats in splits.values())
            for name in CLASS_NAMES.values()
        },
    }
    return {
        "out_root": str(out.resolve()),
        "data_yaml": str(data_yaml_path.resolve()),
        "skipped": skipped,
        "extracted": extracted,
        "data_yaml_written": data_yaml_written,
        "splits": {split: dict(stats) for split, stats in splits.items()},
        "totals": totals,
        "sources": dict(sources),
    }


def _preview(items: Sequence[str], limit: int = 5) -> str:
    if not items:
        return ""
    shown = ", ".join(items[:limit])
    if len(items) > limit:
        shown += ", ..."
    return f" ({shown})"


__all__ = [
    "CLASS_NAMES",
    "DATA_YAML_NAME",
    "IMAGE_SUFFIXES",
    "SPLITS",
    "prepare_scb",
    "validate_layout",
    "write_data_yaml",
]
