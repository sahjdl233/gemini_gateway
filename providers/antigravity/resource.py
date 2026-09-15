from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class AntigravityResource:
    id: str
    provider: str = "antigravity"
    access_token: str | None = None
    refresh_token: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    token_expiry: Any | None = None
    project_id: str | None = None
    ide_type: str = "ANTIGRAVITY"
