from __future__ import annotations

"""Antigravity Core Resource contract implementation (TASK-ANTIGRAVITY-001).

Must inherit from core.resource.Resource and keep Antigravity-specific
credential fields.
"""

from typing import Any

from core.resource import Resource


class AntigravityResource(Resource):
    """Gateway Resource for the Antigravity provider."""

    provider: str = "antigravity"

    # Antigravity-specific credentials (do not remove)
    access_token: str | None = None
    refresh_token: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    token_expiry: Any | None = None
    project_id: str | None = None
    ide_type: str = "ANTIGRAVITY"

