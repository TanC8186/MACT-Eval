#!/usr/bin/env python
"""Verify the ai-service Dockerfile install chain fails fast (OPS02).

We cannot rebuild the 16GB CUDA image on every verification run, so this test
executes the exact RUN command with a fake ``python`` that fails at the
``requirements.txt`` step. Because the installs are joined with ``&&`` (and the
only tolerated failure is the parenthesised OpenCV uninstall), the chain must
return non-zero and never reach the final dependency check.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "ai-service" / "Dockerfile"


def extract_install_command(text: str) -> str:
    match = re.search(r"RUN python -m pip install --upgrade pip.*?(?=\n\nCOPY|\Z)", text, re.S)
    if not match:
        raise SystemExit("could not locate the install RUN block in ai-service/Dockerfile")
    block = match.group(0)[len("RUN "):]
    return block.replace("\\\n", " ")


def bash_executable() -> str:
    """Prefer Git Bash; Windows PATH may resolve ``bash`` to WSL."""
    candidates = [
        os.environ.get("GIT_BASH"),
        "C:/Program Files/Git/bin/bash.exe",
        "C:/Program Files/Git/usr/bin/bash.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    found = shutil.which("bash")
    if not found:
        raise SystemExit("bash is required to verify the Docker install chain")
    return found


def main() -> int:
    text = DOCKERFILE.read_text(encoding="utf-8")
    command = extract_install_command(text)

    # The only `|| true` must be inside the parenthesised uninstall.
    for line in text.splitlines():
        if "|| true" in line and "(python -m pip uninstall" not in line:
            raise SystemExit(f"unbounded `|| true` found in Dockerfile: {line}")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        wrapped = (
            "python() {\n"
            "  for arg in \"$@\"; do\n"
            "    if [[ \"$arg\" == \"requirements.txt\" ]]; then\n"
            "      echo 'injected requirements failure' >&2\n"
            "      return 9\n"
            "    fi\n"
            "  done\n"
            "  return 0\n"
            "}\n"
            "export -f python\n"
            + command
        )
        proc = subprocess.run(
            [bash_executable(), "-c", wrapped],
            capture_output=True, text=True, errors="replace", cwd=tmp,
        )
        if proc.returncode == 0:
            raise SystemExit("install chain unexpectedly succeeded after requirements failure")
        if "dependency check ok" in proc.stdout:
            raise SystemExit("install chain continued to the final check after a failure")
        if proc.returncode == 127:
            raise SystemExit("fake python was not invoked (exit 127)")
        print(f"docker fail-fast check ok (exit={proc.returncode})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
