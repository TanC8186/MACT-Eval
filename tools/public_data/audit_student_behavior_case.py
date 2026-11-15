"""Audit the supplied classroom case and prepare an isolated YOLO dataset.

Original files are read only. Images with any invalid label are excluded as
whole images, with reasons recorded; their boxes are never guessed or fixed.
Case class IDs remain independent of the deployed SCB5 taxonomy.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import xml.etree.ElementTree as ET

from PIL import Image


CASE_CLASSES = (
    "hand-raising", "reading", "writing", "using phone",
    "bowing the head", "leaning over the table",
)


def sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            value.update(block)
    return value.hexdigest()


def parse_labels(path: Path):
    rows, issues = [], []
    if not path.is_file():
        return rows, [{"kind": "missing_label"}]
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            fields = line.split()
            if len(fields) != 5:
                raise ValueError("expected five fields")
            class_id = int(fields[0])
            box = tuple(float(v) for v in fields[1:])
            if not 0 <= class_id < len(CASE_CLASSES):
                raise ValueError("class ID outside case taxonomy")
            if not all(math.isfinite(v) for v in box):
                raise ValueError("non-finite coordinate")
            x, y, width, height = box
            if not (0 <= x <= 1 and 0 <= y <= 1 and 0 < width <= 1 and 0 < height <= 1):
                raise ValueError("invalid center or non-positive size")
            if min(x - width / 2, y - height / 2) < -1e-6 or max(x + width / 2, y + height / 2) > 1 + 1e-6:
                raise ValueError("box extends outside image")
            rows.append((class_id, *box))
        except ValueError as exc:
            issues.append({"line": line_number, "kind": "invalid_label", "reason": str(exc), "row": line})
    return rows, issues


def xml_matches_labels(xml_path: Path, label_path: Path, width: int, height: int) -> bool:
    """Compare raw annotations, allowing XML integer rounding up to one pixel.

    This checks consistency, not validity. Matching inverted boxes still fail
    parse_labels and exclude their entire image from the prepared dataset.
    """
    root = ET.parse(xml_path).getroot()
    if (int(root.findtext("size/width")), int(root.findtext("size/height"))) != (width, height):
        return False
    boxes = []
    for line in label_path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            class_id, x, y, w, h = map(float, line.split())
        except ValueError:
            return False
        boxes.append((class_id, (x - w / 2) * width, (y - h / 2) * height,
                      (x + w / 2) * width, (y + h / 2) * height))
    objects = root.findall("object")
    if len(objects) != len(boxes):
        return False
    for obj in objects:
        name = obj.findtext("name")
        if name not in CASE_CLASSES:
            return False
        box = obj.find("bndbox")
        expected = [float(box.findtext(key)) for key in ("xmin", "ymin", "xmax", "ymax")]
        match = next((i for i, row in enumerate(boxes)
                      if row[0] == CASE_CLASSES.index(name)
                      and all(abs(a - b) <= 1.01 for a, b in zip(row[1:], expected))), None)
        if match is None:
            return False
        boxes.pop(match)
    return True


def audit(case_root: Path, scb_root: Path | None = None):
    data = case_root / "classes_dection"
    manifest, issues, counts, groups, xml_mismatches = [], [], {}, {}, []
    hashes = defaultdict(list)
    for split in ("train", "val"):
        histogram, prefix_counts = Counter(), Counter()
        images = sorted((data / "images" / split).glob("*.jpg"))
        if not images:
            raise ValueError(f"missing images for {split}")
        for image in images:
            label = data / "labels" / split / (image.stem + ".txt")
            rows, problems = parse_labels(label)
            for problem in problems:
                issues.append(dict(problem, image=image.name, split=split))
            with Image.open(image) as decoded:
                width, height = decoded.size
                decoded.verify()
            digest = sha256(image)
            hashes[digest].append({"split": split, "image": image.name})
            histogram.update(CASE_CLASSES[row[0]] for row in rows)
            prefix_counts[image.stem[:4]] += 1
            xml = data / "xml" / (image.stem + ".xml")
            if xml.is_file() and not xml_matches_labels(xml, label, width, height):
                xml_mismatches.append({"split": split, "image": image.name})
            manifest.append({"split": split, "image": image.name, "image_sha256": digest,
                             "label_sha256": sha256(label) if label.is_file() else None,
                             "width": width, "height": height, "valid_boxes": len(rows),
                             "eligible": not problems})
        expected_labels = {image.stem for image in images}
        orphan_labels = sorted(p.name for p in (data / "labels" / split).glob("*.txt")
                               if p.stem not in expected_labels)
        counts[split] = {"images": len(images), "valid_boxes": sum(histogram.values()),
                         "per_class_valid_boxes": dict(histogram), "orphan_labels": orphan_labels}
        groups[split] = dict(prefix_counts)
    overlap = []
    if scb_root is not None:
        # The prepared SCB5 filenames include the first eight SHA-256 digits.
        # Use them only as an index, then verify the complete content digest.
        for candidate in sorted((scb_root / "images").rglob("*.jpg")):
            prefix = candidate.stem.rsplit("_", 1)[-1]
            if any(digest.startswith(prefix) for digest in hashes) and len(prefix) == 8:
                digest = sha256(candidate)
                if digest in hashes:
                    overlap.append({"case_images": hashes[digest],
                                    "scb_image": candidate.relative_to(scb_root).as_posix()})
    metrics = []
    log = case_root / "train_log.txt"
    for line in log.read_text(encoding="utf-8").splitlines():
        match = re.search(r"\('AP', ([0-9.e+-]+)\), \('AP50', ([0-9.e+-]+)\)", line)
        if match:
            metrics.append({"timestamp": line[:23], "map50_95": float(match[1]), "map50": float(match[2])})
    result = {
        "schema": "classroom-case-audit/1.0", "verification_status": "UNVERIFIED",
        "scope": "Material inspection, not training or independent model evaluation",
        "case_classes": list(CASE_CLASSES), "counts": counts,
        "unique_images": len(hashes),
        "exact_duplicates": [files for files in hashes.values() if len(files) > 1],
        "train_val_exact_overlap": sum(len({f['split'] for f in files}) > 1 for files in hashes.values()),
        "issues": issues, "xml_files": len(list((data / 'xml').glob('*.xml'))),
        "xml_label_mismatches_with_one_pixel_tolerance": xml_mismatches,
        "filename_prefix_groups": groups, "scb5_exact_image_overlap": overlap,
        "log_metrics": {"historical_evaluations": len(metrics),
                        "best_ap": max(metrics, key=lambda m: m['map50_95']) if metrics else None,
                        "last": metrics[-1] if metrics else None},
        "source_hashes": {name: sha256(case_root / name) for name in ("last.pth", "train_log.txt")},
        "notebook_sha256": sha256(data / "data.ipynb"), "audit_script_sha256": sha256(Path(__file__)),
        "limitations": ["Exact hashes do not exclude same-classroom or adjacent-frame leakage.",
                        "Historical log metrics have not been independently reproduced.",
                        "Case class IDs 3 and 5 differ from the deployed SCB5 class IDs.",
                        "No real-classroom accuracy improvement is established."],
    }
    return result, manifest


def prepare(case_root: Path, out: Path, result, manifest):
    if result["train_val_exact_overlap"] or result["xml_label_mismatches_with_one_pixel_tolerance"]:
        raise ValueError("resolve split overlap or annotation inconsistency before preparing")
    if any(info["orphan_labels"] for info in result["counts"].values()):
        raise ValueError("resolve orphan labels before preparing")
    eligible_counts = Counter(entry["split"] for entry in manifest if entry["eligible"])
    if any(eligible_counts[split] == 0 for split in ("train", "val")):
        raise ValueError("no eligible images remain in a required split")
    # Re-read source files so changed data cannot silently reuse the audit.
    for entry in manifest:
        for kind in ("images", "labels"):
            source = case_root / "classes_dection" / kind / entry["split"] / entry["image"]
            source = source if kind == "images" else source.with_suffix(".txt")
            expected = entry["image_sha256" if kind == "images" else "label_sha256"]
            if expected is not None and sha256(source) != expected:
                raise ValueError(f"source changed since audit: {source}")
    out.mkdir(parents=True, exist_ok=False)
    kept, excluded, boxes = Counter(), [], Counter()
    for entry in manifest:
        if not entry["eligible"]:
            excluded.append({"split": entry["split"], "image": entry["image"]})
            continue
        split = entry["split"]
        for kind in ("images", "labels"):
            source = case_root / "classes_dection" / kind / split / entry["image"]
            target = out / kind / split / entry["image"]
            if kind == "labels":
                source, target = source.with_suffix(".txt"), target.with_suffix(".txt")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        kept[split] += 1
        boxes[split] += entry["valid_boxes"]
    (out / "data.yaml").write_text(
        "path: " + json.dumps(out.resolve().as_posix(), ensure_ascii=False) + "\n"
        "train: images/train\nval: images/val\nnames:\n"
        + "".join(f"  {i}: {name}\n" for i, name in enumerate(CASE_CLASSES)), encoding="utf-8")
    result["prepared"] = {"images": dict(kept), "boxes": dict(boxes),
                          "excluded_whole_images": excluded,
                          "class_ids": "original case taxonomy; not SCB5",
                          "split_policy": "Original split retained for diagnostics; not an independent classroom test."}
    for name, payload in (("audit.json", result), ("file-manifest.json", manifest)):
        (out / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-root", type=Path, required=True)
    parser.add_argument("--scb-root", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--prepare", type=Path, help="New directory for isolated copies, never overwrite")
    args = parser.parse_args()
    result, manifest = audit(args.case_root, args.scb_root)
    if args.prepare:
        prepare(args.case_root, args.prepare, result, manifest)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
