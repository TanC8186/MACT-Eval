"""Prepare SCB-Dataset3-S for Ultralytics YOLO training (E1).

Examples
--------
Official single archive (images + labels)::

    python tools/public_data/prepare_scb.py \
        --images-zip "data/SCB-Dataset3 yolo dataset/5k_HRW_yolo_Dataset_jpg.zip" \
        --out data/raw/public/scb

Separate image/label archives::

    python tools/public_data/prepare_scb.py \
        --images-zip images.zip --labels-zip labels.zip --out data/raw/public/scb

The command is idempotent: an existing valid normalized tree is left
untouched.  Pass ``--force`` to rebuild ``images/``, ``labels/`` and
``data.yaml`` from the archives.
"""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

try:  # imported as part of the public_data package
    from .scb import prepare_scb
except ImportError:  # executed directly as a script
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from public_data.scb import prepare_scb


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Safely extract and normalize SCB-Dataset3-S into "
            "images/{train,val}, labels/{train,val} and data.yaml."
        )
    )
    parser.add_argument(
        "--images-zip",
        required=True,
        type=Path,
        help="Zip containing images/{train,val} (may also contain labels/).",
    )
    parser.add_argument(
        "--labels-zip",
        type=Path,
        default=None,
        help="Optional separate labels zip; defaults to --images-zip.",
    )
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="Output dataset root (e.g. data/raw/public/scb).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Rebuild an existing output even when it already looks valid. "
            "Only images/, labels/ and data.yaml are replaced."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = prepare_scb(
            args.images_zip,
            args.labels_zip,
            args.out,
            force=args.force,
        )
    except (
        FileNotFoundError,
        FileExistsError,
        ValueError,
        zipfile.BadZipFile,
        OSError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
