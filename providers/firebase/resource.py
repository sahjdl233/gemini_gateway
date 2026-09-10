"""FirebaseResource — one Firebase Project as one Gateway Resource.

Each resource holds the credentials needed to talk to Firebase AI Logic
for a specific project: api_key (Firebase Web API Key), project_id,
app_id, debug_token, and optional proxy.
"""
from __future__ import annotations

from typing import Optional

from pydantic import Field

from core.resource import Resource


class FirebaseResource(Resource):
    """Resource for the Firebase AI Logic provider."""

    provider: str = "firebase"

    # Firebase project credentials (required)
    project_id: str = ""
    api_key: str = ""
    app_id: str = ""
    debug_token: str = ""

    # Optional: per-resource HTTP/SOCKS5 proxy
    proxy: Optional[str] = None

    # Optional: pin to a specific model
    pinned_model: Optional[str] = None
