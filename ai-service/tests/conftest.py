"""Shared fixtures: attach internal service token to all TestClient calls."""
import asyncio
import sys
from pathlib import Path

# Allow `python -m pytest ai-service/tests` from the repository root:
# ensure the ai-service directory (which contains the `app` package) is on sys.path.
AI_SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(AI_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(AI_SERVICE_ROOT))

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.core.config import settings
from app.main import app


@pytest.fixture(autouse=True)
def no_real_background_consumer(monkeypatch):
    """Keep every unit test away from a real Redis connection (L01).

    ``TestClient`` runs the app lifespan, and the lifespan starts the consumer
    supervisor. With the real ``consume_jobs`` every API test opened a Redis
    connection to the developer/CI host; when that connection was garbage
    collected after the test event loop had closed it raised
    ``PytestUnraisableExceptionWarning: Event loop is closed``.

    The replacement is an idle-but-healthy consumer: it never touches Redis or
    the backend but, like the real one, records "running" once it would have
    joined the group. That keeps /health and /ready assertions meaningful
    (they must not silently turn "degraded" just because the queue is faked).
    Only an explicit integration test that re-patches these seams itself may
    connect to isolated infrastructure.
    """
    async def _idle_consumer() -> None:
        main_module._set_consumer_state("running")
        await asyncio.sleep(3600)

    monkeypatch.setattr(main_module, "consume_jobs", _idle_consumer)
    yield


@pytest.fixture
def client() -> TestClient:
    token = (settings.service_token or "").strip()
    headers = {"X-Service-Token": token} if token else {}
    with TestClient(app, headers=headers) as c:
        yield c


@pytest.fixture
def bare_client() -> TestClient:
    """Client without service token — used for auth rejection tests."""
    with TestClient(app) as c:
        yield c
