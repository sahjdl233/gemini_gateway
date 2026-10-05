"""reCAPTCHA Enterprise anonymous token fetching.

Mirrors recaptcha.go (and the APS2api reference): anchor iframe GET -> parse
base token -> reload POST -> rresp.  The token is put into
variables.recaptchaToken for every batchGraphql request.

The release version is discovered dynamically from enterprise.js and cached;
a stale hardcoded version makes the upstream token assessment fail with
"Failed to verify action" (ANON-003, observed live).  Anchor / reload carry
the exact browser-context header sets the console uses: anchor is an iframe
NAVIGATION (build_anchor_headers), reload is a same-origin XHR against
www.google.com with the full fingerprint header set.  On failure the version
cache is invalidated and the whole flow retried.

This module exposes:
  - fetch_recaptcha_token()   the real anchor+reload flow (tests use a mock)
  - RecaptchaTokenProvider    the abstraction AnonymousVertexProvider depends on
  - FakeRecaptchaTokenProvider deterministic fake for tests

No bypass / cracking / bulk fetching is implemented (TASK-002-A section 17).
"""

from __future__ import annotations

import asyncio
import random
import re
import urllib.parse
from typing import Optional, Protocol, runtime_checkable

from transport.http import build_client
from transport.proxy import ProxyConfig, TransportConfig

RECAPTCHA_BASE = "https://www.google.com"
SITE_KEY = "6LdCjtspAAAAAMcV4TGdWLJqRTEk1TfpdLqEnKdj"
RECAPTCHA_CO = "aHR0cHM6Ly9jb25zb2xlLmNsb3VkLmdvb2dsZS5jb206NDQz"
RECAPTCHA_HL = "zh-CN"
RECAPTCHA_V_FALLBACK = "jdMmXeCQEkPbnFDy9T04NbgJ"
RECAPTCHA_VH = "6581054572"

_TOKEN_RE = re.compile(r'id="recaptcha-token"[^>]*value="([^"]+)"')
_RRESP_RE = re.compile(r'rresp","(.*?)"')
_VERSION_RE = re.compile(r"releases/([A-Za-z0-9_-]{20,})")

_CHARSET = "abcdefghijklmnopqrstuvwxyz0123456789"

#: Live retry budget per token fetch; each attempt re-resolves the version
#: after an invalidation (a rolling Google release is the primary failure
#: cause of "Failed to verify action").
_MAX_ATTEMPTS = 3


def _random_string(n: int) -> str:
    return "".join(random.choice(_CHARSET) for _ in range(n))


@runtime_checkable
class RecaptchaTokenProvider(Protocol):
    """Abstraction for producing a fresh reCAPTCHA Enterprise token.

    AnonymousVertexProvider depends on this interface (via token_fetcher),
    never on the concrete Google flow.  Tests inject FakeRecaptchaTokenProvider.
    """

    async def get_token(self) -> str:
        """Return a fresh reCAPTCHA Enterprise token."""
        ...


class FakeRecaptchaTokenProvider:
    """Deterministic token provider for tests (no network)."""

    def __init__(self, token: str = "recaptcha-token") -> None:
        self._token = token
        self.calls = 0

    async def get_token(self) -> str:
        self.calls += 1
        return self._token


# -- release-version discovery (dynamic; Google rolls it regularly) --

_version_lock = asyncio.Lock()
_cached_version: Optional[str] = None


def invalidate_cached_version() -> None:
    """Drop the cached release version after a failed fetch attempt."""
    global _cached_version
    _cached_version = None


async def fetch_release_version(client) -> Optional[str]:
    """Parse the current reCAPTCHA release version from enterprise.js."""
    from providers.anonymous_vertex.headers import build_xhr_headers

    resp = await client.get(
        f"{RECAPTCHA_BASE}/recaptcha/enterprise.js?render={SITE_KEY}",
        headers=build_xhr_headers(
            content_type="",
            accept="*/*",
            origin=RECAPTCHA_BASE,
            referer=RECAPTCHA_BASE,
            site="cross-site",
        ),
    )
    if resp.status_code != 200:
        return None
    match = _VERSION_RE.search(resp.text)
    return match.group(1) if match else None


async def _current_version(client) -> str:
    """Cached current release version; falls back to the constant."""
    global _cached_version
    if _cached_version:
        return _cached_version
    async with _version_lock:
        if _cached_version:
            return _cached_version
        try:
            version = await fetch_release_version(client)
        except Exception:  # noqa: BLE001 - version discovery is best-effort
            version = None
        if version:
            _cached_version = version
            return version
    return RECAPTCHA_V_FALLBACK


async def _fetch_once(client, version: str) -> str:
    """One anchor + reload round on the given (shared) HTTP client."""
    from providers.anonymous_vertex.headers import (
        build_anchor_headers,
        build_xhr_headers,
    )

    cb = _random_string(10)
    anchor_url = (
        f"{RECAPTCHA_BASE}/recaptcha/enterprise/anchor?ar=1&k={SITE_KEY}"
        f"&co={RECAPTCHA_CO}&hl={RECAPTCHA_HL}&v={version}&size=invisible"
        f"&anchor-ms=20000&execute-ms=15000&cb={cb}"
    )

    resp = await client.get(anchor_url, headers=build_anchor_headers())
    if resp.status_code != 200:
        raise RuntimeError(f"recaptcha anchor failed: HTTP {resp.status_code}")
    anchor_body = resp.text
    m = _TOKEN_RE.search(anchor_body)
    if m is None:
        raise RuntimeError("recaptcha anchor: recaptcha-token not found")
    base_token = m.group(1)

    form = urllib.parse.urlencode({
        "v": version,
        "reason": "q",
        "k": SITE_KEY,
        "c": base_token,
        "co": RECAPTCHA_CO,
        "hl": RECAPTCHA_HL,
        "size": "invisible",
        "vh": RECAPTCHA_VH,
        "chr": "",
        "bg": "",
    })
    reload_url = f"{RECAPTCHA_BASE}/recaptcha/enterprise/reload?k={SITE_KEY}"

    # The reload is a same-origin XHR from the anchor page on www.google.com
    # (origin www.google.com / referer anchor URL), carrying the full
    # browser fingerprint header set — NOT a console-origin request.
    resp = await client.post(
        reload_url,
        content=form,
        headers=build_xhr_headers(
            content_type="application/x-www-form-urlencoded;charset=UTF-8",
            accept="*/*",
            origin=RECAPTCHA_BASE,
            referer=anchor_url,
            site="same-origin",
        ),
    )
    if resp.status_code != 200:
        raise RuntimeError(f"recaptcha reload failed: HTTP {resp.status_code}")
    reload_body = resp.text
    rm = _RRESP_RE.search(reload_body)
    if rm is None:
        raise RuntimeError("recaptcha reload: rresp token not found")
    return rm.group(1)


async def fetch_recaptcha_token(
    client=None,
    proxy: Optional[ProxyConfig] = None,
    version: Optional[str] = None,
) -> str:
    """Fetch a reCAPTCHA Enterprise token via anchor + reload.

    Retries up to three times; each failed attempt invalidates the cached
    release version so the next attempt re-resolves it.  Raises RuntimeError
    (mapped to a retryable ProviderError by the Provider) when all attempts
    fail.

    client: an httpx.AsyncClient-compatible object (tests inject a mock).
    proxy: optional proxy config (only used when client is None).
    version: explicit release version (default: dynamic discovery).
    """
    if client is None:
        cfg = TransportConfig(proxy=proxy, timeout_seconds=15.0)
        client = build_client(cfg)

    last_error: Optional[Exception] = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            ver = version or await _current_version(client)
            return await _fetch_once(client, ver)
        except Exception as exc:  # noqa: BLE001 - retried below
            last_error = exc
            invalidate_cached_version()
            if attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(0.2 * (attempt + 1))
    raise last_error if last_error else RuntimeError("recaptcha fetch failed")
