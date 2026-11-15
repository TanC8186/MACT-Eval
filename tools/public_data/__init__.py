"""Shared utilities for the public-data classroom-climate experiments.

Modules in this package implement the observable video/speech rule baseline and
evaluation helpers described in
``docs/superpowers/specs/2026-10-18-scope-narrowing-public-data-design.md``.

Importing this package must not require heavy optional dependencies (YOLO,
faster-whisper, ...).  Those are imported lazily by their dedicated modules.
"""
