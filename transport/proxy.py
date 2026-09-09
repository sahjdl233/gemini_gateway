"""Proxy / egress transport configuration (independent of providers)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ProxyConfig:
    scheme: str = "direct"  # direct | http | https | socks5
    host: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None

    @property
    def is_direct(self) -> bool:
        return self.scheme == "direct" or not self.host

    def as_url(self) -> Optional[str]:
        if self.is_direct:
            return None
        auth = ""
        if self.username:
            auth = f"{self.username}:{self.password or ''}@"
        return f"{self.scheme}://{auth}{self.host}:{self.port}"


@dataclass
class TransportConfig:
    proxy: Optional[ProxyConfig] = None
    timeout_seconds: float = 60.0
    verify_tls: bool = True  # MUST stay True; never disabled "for convenience"
    follow_redirects: bool = True
    extra_headers: dict = field(default_factory=dict)
