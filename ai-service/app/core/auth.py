"""Shared-secret auth for internal AI service endpoints."""
from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from app.core.config import settings

HEADER_NAME = "X-Service-Token"


async def require_service_token(
    x_service_token: str | None = Header(default=None, alias=HEADER_NAME),
) -> None:
    """Reject requests without a valid internal service token.

    The token is shared with the Spring Boot backend (mact.service-token).
    Health/status endpoints stay open for probes.
    """
    expected = (settings.service_token or "").strip()
    if not expected:
        # Misconfiguration: refuse rather than run open on the network.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MACT_AI_SERVICE_TOKEN is not configured",
        )
    if not x_service_token or not secrets.compare_digest(x_service_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Service-Token",
        )
