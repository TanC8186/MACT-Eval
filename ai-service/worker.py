"""Standalone Redis Stream worker for the MACT-Eval AI service.

The FastAPI app starts the same consumer inside its lifespan, so a single
``uvicorn app.main:app`` is enough for normal deployments. This entrypoint is
for running inference in its own process instead:

    python worker.py

Use it when the HTTP API and the job consumer should be restartable
independently, or when a long job must not share a process with request
handling. It consumes and acknowledges the same ``analysis:jobs`` stream.
"""
from __future__ import annotations

import asyncio
import logging

from app.workers.base import consume_jobs


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(consume_jobs())
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Worker interrupted; exiting")


if __name__ == "__main__":
    main()
