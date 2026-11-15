"""Regression test for the Windows cuDNN loader collision.

ctranslate2 ships its own cuDNN loader (``ctranslate2/cudnn64_9.dll``). When
that copy is the first one loaded, the first YOLO inference that selects CUDA
terminates the whole process with exit code 127 and the message

    Could not load symbol cudnnGetLibConfig. Error code 127

(no Python exception, so no try/except and no asyncio done-callback fires —
the uvicorn process simply disappears and port 8001 stops listening).

``app.workers.asr`` imports torch before faster-whisper on Windows so the
loader binds to ``torch/lib/cudnn64_9.dll`` instead. This test pins the
import order in a fresh interpreter, because the ordering only matters the
first time each DLL enters a process.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

AI_SERVICE_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DLL-name collision only")
def test_asr_imports_torch_before_ctranslate2():
    pytest.importorskip("torch")
    code = textwrap.dedent(
        """
        import sys
        import app.workers.asr  # noqa: F401
        order = list(sys.modules)
        assert "torch" in order, "app.workers.asr did not import torch"
        assert "ctranslate2" in order, "app.workers.asr did not import ctranslate2"
        print(order.index("torch") < order.index("ctranslate2"))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(AI_SERVICE_ROOT),
        capture_output=True,
        text=True,
        timeout=600,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "True"
