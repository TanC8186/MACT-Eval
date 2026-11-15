"""Combine SCB5 with the user's classroom case using an explicit eight-class map.

Python 3.8+. Original files are never edited. Invalid-label images are excluded
with a reason, not silently repaired. Case filename group 4003 is a temporal
holdout within the same classroom; it is not an independent classroom test.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
import os
from pathlib import Path
import shutil


CLASSES = ("hand-raising", "reading", "writing", "discussing", "bow-head",
           "turn-head", "using phone", "leaning over the table")
CASE_TO_UNIFIED = {0: 0, 1: 1, 2: 2, 3: 6, 4: 4, 5: 7}
CASE_GROUPS = {"4001": "train", "4002": "train", "4003": "val"}


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            result.update(block)
    return result.hexdigest()


def read_rows(path, class_map):
    rows, errors, edge_rows = [], [], []
    if not path.is_file():
        return [], [{"kind": "missing_label"}], []
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        fields = line.split()
        try:
            if len(fields) != 5:
                raise ValueError("expected five fields")
            source_id = int(fields[0])
            if source_id not in class_map:
                raise ValueError("unknown source class")
            values = [float(v) for v in fields[1:]]
            x, y, width, height = values
            if not all(math.isfinite(v) for v in values):
                raise ValueError("non-finite box")
            if not (0 <= x <= 1 and 0 <= y <= 1 and 0 < width <= 1 and 0 < height <= 1):
                raise ValueError("invalid center or non-positive size")
            if min(x - width / 2, y - height / 2) < -1e-6 or max(x + width / 2, y + height / 2) > 1 + 1e-6:
                # Retain the source box verbatim. Ultralytics image transforms
                # clip visible bounds; we do not invent a new annotation here.
                edge_rows.append(number)
            rows.append(str(class_map[source_id]) + " " + " ".join(fields[1:]))
        except ValueError as exc:
            errors.append({"line": number, "reason": str(exc), "row": line})
    return rows, errors, edge_rows


def link_or_copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(str(source), str(target))
        return "hardlink"
    except OSError:
        shutil.copyfile(str(source), str(target))
        return "copy"


def write_config(path, root, train, val):
    path.write_text("path: " + json.dumps(root.resolve().as_posix(), ensure_ascii=False)
                    + "\ntrain: " + train + "\nval: " + val + "\nnames:\n"
                    + "".join("  {}: {}\n".format(i, name) for i, name in enumerate(CLASSES)),
                    encoding="utf-8")


def prepare(scb_root, case_root, out):
    scb_root, case_root, out = (Path(p).resolve() for p in (scb_root, case_root, out))
    if out.exists():
        raise FileExistsError(str(out))
    for source in (scb_root, case_root):
        if out == source or source in out.parents or out in source.parents:
            raise ValueError("output and source directories must be separate")
    case_manifest = json.loads((case_root / "file-manifest.json").read_text(encoding="utf-8"))
    expected_case = {(row["split"], row["image"]): row for row in case_manifest if row["eligible"]}
    records, excluded, edge_warnings = [], [], []
    for source, root, mapping in (("scb5", scb_root, dict(enumerate(range(6)))),
                                  ("case11", case_root, CASE_TO_UNIFIED)):
        found = set()
        for original_split in ("train", "val"):
            images = sorted((root / "images" / original_split).glob("*.jpg"))
            if not images:
                raise ValueError("empty source split: {}/{}".format(source, original_split))
            image_stems = {p.stem for p in images}
            orphan_labels = [p.name for p in (root / "labels" / original_split).glob("*.txt")
                             if p.stem not in image_stems]
            if orphan_labels:
                raise ValueError("orphan labels: " + str(orphan_labels[:3]))
            for image in images:
                label = root / "labels" / original_split / (image.stem + ".txt")
                rows, errors, edges = read_rows(label, mapping)
                image_hash = digest(image)
                label_hash = digest(label) if label.is_file() else None
                key = (original_split, image.name)
                if source == "case11":
                    expected = expected_case.get(key)
                    if expected is None or image_hash != expected["image_sha256"] or label_hash != expected["label_sha256"]:
                        raise ValueError("case file differs from audited material: " + image.name)
                    found.add(key)
                    group = image.stem[:4]
                    if group not in CASE_GROUPS:
                        raise ValueError("unknown case group: " + image.stem)
                    split = CASE_GROUPS[group]
                else:
                    group, split = None, original_split
                provenance = {"source": source, "original_split": original_split,
                              "image": image.name, "split": split, "group": group,
                              "image_sha256": image_hash, "source_label_sha256": label_hash}
                if errors:
                    excluded.append(dict(provenance, errors=errors))
                    continue
                if edges:
                    edge_warnings.append(dict(provenance, rows=edges,
                                               treatment="source coordinates retained unchanged"))
                records.append(dict(provenance, image_path=image, label_path=label, rows=rows))
        if source == "case11" and found != set(expected_case):
            raise ValueError("audited case images are missing")
    # Fail closed on duplicates across sources or splits. Annotation union would
    # need its own explicit conflict policy rather than whichever source wins.
    seen = {}
    for record in records:
        old = seen.get(record["image_sha256"])
        if old:
            raise ValueError("duplicate image across inputs: {} and {}".format(old, record["image"]))
        seen[record["image_sha256"]] = record["image"]
    for split in ("train", "val"):
        if not any(r["split"] == split for r in records):
            raise ValueError("no eligible images in " + split)
    out.mkdir(parents=True, exist_ok=False)
    manifest, files, class_counts = [], [], defaultdict(Counter)
    counts, materialization = Counter(), Counter()
    lists = defaultdict(list)
    for record in records:
        source, split = record["source"], record["split"]
        stem = source + "_" + Path(record["image"]).stem
        image = out / "images" / split / (stem + ".jpg")
        label = out / "labels" / split / (stem + ".txt")
        materialization[link_or_copy(record["image_path"], image)] += 1
        label.parent.mkdir(parents=True, exist_ok=True)
        label.write_text("\n".join(record["rows"]) + ("\n" if record["rows"] else ""), encoding="utf-8")
        counts[source + "/" + split] += 1
        class_counts[source + "/" + split].update(CLASSES[int(row.split()[0])] for row in record["rows"])
        rel = image.relative_to(out).as_posix()
        lists[split].append(rel)
        lists[source + "_" + split].append(rel)
        manifest.append({k: v for k, v in record.items() if k not in ("image_path", "label_path", "rows")})
        for path in (image, label):
            files.append({"path": path.relative_to(out).as_posix(), "bytes": path.stat().st_size,
                          "sha256": record["image_sha256"] if path == image else digest(path)})
    for name, entries in lists.items():
        # Relative to the dataset root, so a transfer does not retain Windows
        # paths. Build the lists again on the remote host rather than edit them.
        (out / (name + ".txt")).write_text("\n".join(str((out / p).resolve()) for p in entries) + "\n", encoding="utf-8")
    write_config(out / "data.yaml", out, "train.txt", "val.txt")
    write_config(out / "scb5-val.yaml", out, "scb5_train.txt", "scb5_val.txt")
    write_config(out / "case11-val.yaml", out, "case11_train.txt", "case11_val.txt")
    for path in sorted(out.glob("*.txt")) + sorted(out.glob("*.yaml")):
        files.append({"path": path.name, "bytes": path.stat().st_size, "sha256": digest(path)})
    report = {
        "schema": "classroom-case8/1.0", "evidence_status": "UNVERIFIED",
        "classes": list(CLASSES), "case_class_mapping": CASE_TO_UNIFIED,
        "case_groups": CASE_GROUPS, "images_by_source_split": dict(counts),
        "class_boxes_by_source_split": {k: dict(v) for k, v in class_counts.items()},
        "excluded_whole_images": excluded,
        "preexisting_case_exclusions": json.loads((case_root / "audit.json").read_text(encoding="utf-8"))["prepared"]["excluded_whole_images"],
        "edge_coordinate_warnings": edge_warnings,
        "unique_images": len(records), "exact_image_duplicates": 0,
        "materialization": dict(materialization), "preparation_script_sha256": digest(__file__),
        "source_case_audit_sha256": digest(case_root / "audit.json"),
        "limitations": ["Case 4003 is a later-time holdout from the same classroom, not an independent classroom.",
                        "SCB5 original split retained; perceptual duplicates or scene overlap are not ruled out.",
                        "Annotations may be incomplete for classes absent from each source taxonomy.",
                        "Case bowing the head mapped to visual bow-head; no attention score inferred.",
                        "Historical DAMO weights and burned-in demo predictions are not ground truth."],
    }
    for name, payload in (("file-manifest.json", files), ("provenance.json", manifest), ("merge-report.json", report)):
        (out / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scb-root", type=Path, required=True)
    parser.add_argument("--case-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = prepare(args.scb_root, args.case_root, args.out)
    print(json.dumps({k: report[k] for k in ("classes", "images_by_source_split", "class_boxes_by_source_split", "unique_images")}, ensure_ascii=True))


if __name__ == "__main__":
    main()
