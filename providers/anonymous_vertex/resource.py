"""AnonymousVertexResource — the resource model for Anonymous Vertex.

Each resource represents a single recaptcha-capable endpoint identity.
In practice, a resource just holds the configuration needed to make requests
(proxy, recaptcha parameters). The provider handles per-request token fetching.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from core.resource import Resource


class AnonymousVertexResource(Resource):
    """Resource for Anonymous Vertex provider.

    Currently anonymous; no persistent credentials are stored. The resource
    identity is just an ID. Future extensions could add:
    - specific proxy assignment
    - custom recaptcha parameters
    - API key overrides (not recommended for anonymous)
    """

    # Override provider to be fixed
    provider: str = "anonymous_vertex"

    # Optional: per-resource proxy configuration (uses core proxy config)
    proxy_scheme: str = "direct"
    proxy_host: Optional[str] = None
    proxy_port: Optional[int] = None
    proxy_username: Optional[str] = None
    proxy_password: Optional[str] = None

    # Model this resource is pinned to (optional; if None, serves all models)
    pinned_model: Optional[str] = None

    def get_proxy_config(self) -> dict:
        """Return proxy configuration as a dict for transport layer."""
        if self.proxy_scheme == "direct" or not self.proxy_host:
            return {"scheme": "direct"}
        return {
            "scheme": self.proxy_scheme,
            "host": self.proxy_host,
            "port": self.proxy_port,
            "username": self.proxy_username,
            "password": self.proxy_password,
        }
