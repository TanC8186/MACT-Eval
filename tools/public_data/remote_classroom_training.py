"""Confined SCB classroom-model experiment runner (Python 3.8+).

Run artifacts, downloads, caches, checkpoints and dataset caches stay under
/data/tjx_upload/MACT. Existing runs are never overwritten. No real-classroom
accuracy claim is made without a separately annotated classroom test set.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import time
from pathlib import Path

ROOT = Path('/data/tjx_upload/MACT')


def confined(path):
    path = Path(path).resolve()
    path.relative_to(ROOT.resolve())
    return path


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def write_json(path, value):
    temporary = Path(str(path) + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def summarize_validation(results):
    per_class = []
    for index, class_id in enumerate(results.box.ap_class_index):
        p, r, ap50, ap = results.box.class_result(index)
        per_class.append({'id': int(class_id), 'name': results.names[int(class_id)],
                          'precision': float(p), 'recall': float(r),
                          'f1': float(2*p*r/(p+r)) if p+r else 0.,
                          'map50': float(ap50), 'map50_95': float(ap)})
    metrics = {key: float(value) for key, value in results.results_dict.items()}
    import math
    values = list(metrics.values()) + [value for row in per_class for key, value in row.items()
                                      if key not in {'id', 'name'}]
    if not all(math.isfinite(value) for value in values):
        raise ValueError('Non-finite validation result')
    return {'metrics': metrics, 'per_class': per_class}


def validate_with_count(model, **kwargs):
    """Count images from the public validator callback, including skipped units."""
    counts = []

    def completed(validator):
        expected = len(validator.dataloader.dataset)
        if int(validator.seen) != expected:
            raise ValueError('Validation denominator mismatch: {} of {}'.format(validator.seen, expected))
        counts.append(expected)

    model.add_callback('on_val_end', completed)
    try:
        results = model.val(**kwargs)
    finally:
        model.callbacks['on_val_end'].remove(completed)
    if len(counts) != 1:
        raise ValueError('Expected one completed validation callback')
    return results, counts[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['smoke', 'baseline', 'train', 'evaluate'], required=True)
    parser.add_argument('--attempt', required=True)
    parser.add_argument('--model', default=str(ROOT / 'models/baseline-scb5.pt'))
    parser.add_argument('--data', default=str(ROOT / 'datasets/scb5/data.yaml'))
    parser.add_argument('--extra-val-data', nargs='*', default=[])
    parser.add_argument('--smoke-fraction', type=float, default=.02)
    parser.add_argument('--imgsz', type=int, default=960)
    parser.add_argument('--epochs', type=int, default=20)
    parser.add_argument('--batch', type=int, default=4)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--device', default='0')
    args = parser.parse_args()
    if not 0 < args.smoke_fraction <= 1:
        raise ValueError('smoke-fraction must be in (0,1]')
    os.chdir(confined(ROOT))
    for key, relative in {
        'TMPDIR': '.cache/tmp', 'TMP': '.cache/tmp', 'TEMP': '.cache/tmp',
        'XDG_CACHE_HOME': '.cache', 'XDG_CONFIG_HOME': '.cache/config',
        'YOLO_CONFIG_DIR': '.cache/ultralytics', 'TORCH_HOME': '.cache/torch',
        'HF_HOME': '.cache/huggingface', 'MPLCONFIGDIR': '.cache/matplotlib',
        'PIP_CACHE_DIR': '.cache/pip',
    }.items():
        directory = confined(ROOT / relative)
        directory.mkdir(parents=True, exist_ok=True)
        os.environ[key] = str(directory)
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    import numpy as np
    import torch
    import ultralytics
    from ultralytics import YOLO, settings

    settings.update({'datasets_dir': str(ROOT / 'datasets'),
                     'weights_dir': str(ROOT / 'models'), 'runs_dir': str(ROOT / 'runs')})
    if Path(args.attempt).name != args.attempt:
        raise ValueError('attempt must be a single directory name')
    out = confined(ROOT / 'runs' / args.attempt)
    out.mkdir(exist_ok=False)
    model_path = confined(args.model)
    data = confined(args.data)
    extra_data = [confined(path) for path in args.extra_val_data]
    contract = {
        'status': 'RUNNING', 'evidence_status': 'UNVERIFIED',
        'started_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'command': sys.argv, 'cwd': str(ROOT), 'args': vars(args),
        'model_sha256': digest(model_path),
        'dataset_manifest_sha256': digest(data.parent / 'file-manifest.json'),
        'data_yaml_sha256': digest(data),
        'extra_validation': [{'data': str(path), 'yaml_sha256': digest(path),
                              'manifest_sha256': digest(path.parent / 'file-manifest.json')}
                             for path in extra_data],
        'script_sha256': digest(__file__),
        'environment': {'python': platform.python_version(), 'torch': torch.__version__,
                        'ultralytics': ultralytics.__version__, 'numpy': np.__version__,
                        'cuda': torch.version.cuda,
                        'gpu': torch.cuda.get_device_name(0) if args.device != 'cpu' else None},
        'limits': 'Source dataset validation only; no independent human-labeled deployment-classroom test set available',
    }
    write_json(out / 'contract.json', contract)
    start = time.monotonic()
    try:
        model = YOLO(str(model_path))
        if args.mode in {'smoke', 'train'}:
            model.train(
                data=str(data), epochs=1 if args.mode == 'smoke' else args.epochs,
                fraction=args.smoke_fraction if args.mode == 'smoke' else 1., imgsz=args.imgsz,
                batch=args.batch, device=args.device, workers=2, cache=False,
                project=str(out), name='train', exist_ok=False,
                seed=args.seed, deterministic=True, optimizer='AdamW', lr0=.001,
                lrf=.01, patience=args.epochs, close_mosaic=5 if args.mode == 'train' else 0,
                amp=True, plots=True, save=True, val=True,
            )
            model_path = confined(Path(model.trainer.save_dir) / 'weights/best.pt')
            trained_images = len(model.trainer.train_loader.dataset)
            model = YOLO(str(model_path))
        else:
            trained_images = None
        results, validation_images = validate_with_count(model, data=str(data), split='val', imgsz=args.imgsz,
                            batch=args.batch, device=args.device, workers=2, plots=True,
                            project=str(out), name='val', exist_ok=False)
        main_validation = summarize_validation(results)
        separate_validation = []
        for index, extra in enumerate(extra_data):
            evaluated, evaluated_count = validate_with_count(model, data=str(extra), split='val', imgsz=args.imgsz,
                                  batch=args.batch, device=args.device, workers=2, plots=True,
                                  project=str(out), name='val-domain-{}'.format(index), exist_ok=False)
            separate_validation.append(dict(summarize_validation(evaluated), data=str(extra),
                                            evaluated_images=evaluated_count))
        summary = dict(contract, status='SUCCEEDED', elapsed_seconds=time.monotonic()-start,
                       checkpoint=str(model_path), checkpoint_sha256=digest(model_path),
                       trained_images=trained_images, evaluated_images=validation_images,
                       separate_validation=separate_validation, **main_validation)
        write_json(out / 'result.json', summary)
        print(json.dumps(summary), flush=True)
    except Exception as exc:
        write_json(out / 'result.json', dict(contract, status='FAILED',
                   elapsed_seconds=time.monotonic()-start, error=repr(exc)))
        raise


if __name__ == '__main__':
    main()
