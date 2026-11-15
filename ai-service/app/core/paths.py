"""Path safety helpers: keep local file access inside configured roots."""
from __future__ import annotations

import os
from pathlib import Path

from app.core.config import settings


def media_work_dir() -> Path:
    """Directory where downloaded media is written before analysis.

    ``MACT_AI_WORK_DIR`` (settings.work_dir) wins when set, so deployments
    can keep large analysis copies on the data disk instead of the system
    temp dir.

    The fallback is a **project-relative** ``.cache/tmp/ai-media`` — not the
    OS temp dir. Reason (2026-10-30): this project forbids writing anywhere
    outside ``E:/Personal_Design``; the OS temp dir is on C:. Container
    deployments always set ``MACT_AI_WORK_DIR`` (docker-compose.yml), so this
    fallback only matters for a bare local ``uvicorn`` run — and in that case
    writing to C: is exactly what must not happen.
    """
    configured = (settings.work_dir or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path(__file__).resolve().parents[3] / ".cache" / "tmp" / "ai-media").resolve()


def allowed_roots() -> list[Path]:
    roots: list[Path] = [
        Path("data").resolve(),
        (Path("data") / "uploads").resolve(),
        media_work_dir(),
    ]
    model_dir = Path(settings.model_dir).expanduser()
    if model_dir.exists():
        roots.append(model_dir.resolve())
    seen: set[str] = set()
    unique: list[Path] = []
    for r in roots:
        key = str(r)
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


def safe_basename(name: str | None) -> str | None:
    """Strip directories and reject traversal / empty names."""
    if not name:
        return None
    cleaned = str(name).replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not cleaned or cleaned in {".", ".."} or ".." in cleaned:
        return None
    if cleaned.startswith("/") or (len(cleaned) > 1 and cleaned[1] == ":"):
        return None
    return cleaned


def resolve_under_roots(path_like: str | None) -> Path | None:
    """Resolve a path and require it to sit under an allowed root.

    Non-existent paths under an allowed root are still returned so workers
    can answer FILE_NOT_FOUND / VIDEO_NOT_FOUND instead of an opaque 400.
    """
    if not path_like:
        return None
    try:
        candidate = Path(path_like).expanduser()
        if not candidate.is_absolute():
            candidate = (Path.cwd() / candidate).resolve()
        else:
            candidate = candidate.resolve()
    except (OSError, RuntimeError):
        return None
    for root in allowed_roots():
        try:
            candidate.relative_to(root)
            return candidate
        except ValueError:
            continue
    return None


def is_safe_local_path(path_like: str | None) -> bool:
    return resolve_under_roots(path_like) is not None
