"""GeminiCliResource - one OAuth credential (account) as one Gateway Resource.

Each resource owns:
  - OAuth credential material (refresh flow)
  - cloudaicompanionProject (project id) + tier
  - per-resource cooldown/quota state (via the core Resource fields)

Multiple accounts => multiple resources => the existing ResourcePool handles
rotation/failover.  Credentials are never logged.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import Field

from core.resource import Resource


class GeminiCliResource(Resource):
    """Resource for the Gemini CLI (Code Assist) provider."""

    provider: str = "gemini_cli"

    # OAuth credential material (never log these).
    access_token: str = ""
    refresh_token: str = ""
    client_id: str = ""
    client_secret: str = ""
    token_expiry: Optional[float] = None  # epoch seconds

    # Code Assist project (filled by onboarding/loadCodeAssist).
    project_id: Optional[str] = None
    tier: str = "unknown"  # FREE / PRO / ULTRA / unknown

    # Optional: pin to a specific model.
    pinned_model: Optional[str] = None

    # Optional: per-resource HTTP/SOCKS5 proxy.
    proxy: Optional[str] = None

    # Onboarding knobs.
    ide_type: str = "GCLI"
    platform: str = "PLATFORM_UNSPECIFIED"
    plugin_type: str = "GEMINI"

    # Preview credentials may be pre-flighted at build/health time.
    preview: bool = Field(default=True)

    def redacted_dict(self) -> Dict[str, Any]:
        """Config-like view with all credential material masked."""
        data = self.model_dump(exclude={"access_token", "refresh_token"})
        data["access_token"] = "***" if self.access_token else ""
        data["refresh_token"] = "***" if self.refresh_token else ""
        return data
