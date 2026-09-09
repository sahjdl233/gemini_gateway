"""reCAPTCHA Enterprise anonymous token fetching.

Mirrors recaptcha.go: anchor iframe GET -> parse base token -> reload POST -> rresp.
The token is put into variables.recaptchaToken for every batchGraphql request.

The live fetch hits google.com; tests use a mock transport instead of the network.
"""
from __future__ import annotations

import re
from typing import Optional

from transport.http import build_client, fetch_json
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

import random
import string


def _random_string(n: int) -> str:
    return "".join(random.choice(_CHARSET) for _ in range(n))


async def fetch_recaptcha_token(
    client=None,
    proxy: Optional[ProxyConfig] = None,
    version: Optional[str] = None,
) -> str:
    """Fetch a reCAPTCHA Enterprise token via anchor + reload.

    client: an httpx.AsyncClient-compatible object (tests inject a mock).
    proxy: optional proxy config.
    version: explicit release version (defaults to a fallback).
    """
    import httpx

    if client is None:
        cfg = TransportConfig(proxy=proxy, timeout_seconds=15.0)
        client = build_client(cfg)

    ver = version or RECAPTCHA_V_FALLBACK

    cb = _random_string(10)
    anchor_url = (
        f"{RECAPTCHA_BASE}/recaptcha/enterprise/anchor?ar=1&k={SITE_KEY}"
        f"&co={RECAPTCHA_CO}&hl={RECAPTCHA_HL}&v={ver}&size=invisible"
        f"&anchor-ms=20000&execute-ms=15000&cb={cb}"
    )

    resp = await client.get(anchor_url)
    if resp.status_code != 200:
        raise RuntimeError(f"recaptcha anchor failed: HTTP {resp.status_code}")
    anchor_body = resp.text
    m = _TOKEN_RE.search(anchor_body)
    if m is None:
        raise RuntimeError("recaptcha anchor: recaptcha-token not found")
    base_token = m.group(1)

    import urllib.parse

    form = urllib.parse.urlencode({
        "v": ver,
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

    resp = await client.post(
        reload_url,
        content=form,
        headers={"content-type": "application/x-www-form-urlencoded;charset=UTF-8"},
    )
    if resp.status_code != 200:
        raise RuntimeError(f"recaptcha reload failed: HTTP {resp.status_code}")
    reload_body = resp.text
    rm = _RRESP_RE.search(reload_body)
    if rm is None:
        raise RuntimeError("recaptcha reload: rresp token not found")
    return rm.group(1)
