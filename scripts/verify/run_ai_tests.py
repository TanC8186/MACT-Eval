#!/usr/bin/env python
"""Run ai-service tests in an isolated, reproducible environment.

The developer machine may have two conflicting OpenCV wheels (a user-site
headless 5.x without ``CascadeClassifier`` shadowing a working conda 4.x).
Creating a project-local venv with ``--system-site-packages`` and installing
the pinned headless wheel there fixes the environment **without modifying the
global Python installation**.
"""
from __future__ import annotations

import os
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AI_SERVICE = ROOT / "ai-service"
VENV_DIR = AI_SERVICE / ".venv-test"
PINNED_OPENCV = "opencv-python-headless==4.11.0.86"
REQUIREMENTS = AI_SERVICE / "requirements-test.txt"
MARKER = VENV_DIR / ".test-deps-installed"


def venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def ensure_venv() -> Path:
    python = venv_python()
    if not python.exists():
        print(f"[ai-tests] creating isolated venv at {VENV_DIR}")
        venv.EnvBuilder(with_pip=True, system_site_packages=False).create(VENV_DIR)
    if not MARKER.exists():
        print("[ai-tests] installing requirements-test.txt into project venv")
        subprocess.run([str(python), "-m", "pip", "install", "--quiet",
                        "--disable-pip-version-check", "-r", str(REQUIREMENTS)], check=True)
        MARKER.write_text("ok", encoding="utf-8")
    check = subprocess.run(
        [str(python), "-c", "import cv2; print(hasattr(cv2, 'CascadeClassifier'))"],
        capture_output=True, text=True,
    )
    if check.stdout.strip() != "True":
        print(f"[ai-tests] installing pinned {PINNED_OPENCV} into project venv")
        subprocess.run([str(python), "-m", "pip", "install", "--quiet",
                        "--disable-pip-version-check", PINNED_OPENCV], check=True)
        check = subprocess.run(
            [str(python), "-c", "import cv2; print(hasattr(cv2, 'CascadeClassifier'))"],
            capture_output=True, text=True,
        )
        if check.stdout.strip() != "True":
            raise SystemExit("[ai-tests] OpenCV still lacks CascadeClassifier after install")
    return python


def main() -> int:
    python = ensure_venv()
    cmd = [str(python), "-m", "pytest", "ai-service/tests", "-q", "-p", "no:cacheprovider"]
    print("[ai-tests]", " ".join(cmd))
    return subprocess.run(cmd, cwd=ROOT).returncode


if __name__ == "__main__":
    sys.exit(main())
