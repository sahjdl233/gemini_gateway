"""Chrome 150 browser-fingerprint headers for Anonymous Vertex requests.

Mirrors transport/headers.go XHRHeaders(). Note: TLS ClientHello fingerprint is
NOT reproducible with plain httpx; a TLS-fingerprint-capable transport is required
for production. This module provides the HTTP headers.
"""
from __future__ import annotations

import base64
import hashlib
from typing import Dict

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
CH_UA = '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"'
CH_UA_FULL_VERSION_LIST = '"Not;A=Brand";v="8.0.0.0", "Chromium";v="150.0.7871.13", "Google Chrome";v="150.0.7871.13"'

_FIXED = {
    "sec-ch-ua": CH_UA,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-arch": '"x86"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version": '"150.0.7871.13"',
    "sec-ch-ua-full-version-list": CH_UA_FULL_VERSION_LIST,
    "sec-ch-ua-platform-version": '"19.0.0"',
    "sec-ch-ua-model": '""',
    "sec-ch-ua-wow64": "?0",
    "sec-ch-ua-form-factors": '"Desktop"',
    "user-agent": USER_AGENT,
    "sec-fetch-mode": "cors",
    "sec-fetch-dest": "empty",
    "accept-encoding": "gzip, deflate, br",
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
    "priority": "u=1, i",
    "x-goog-authuser": "0",
    "x-browser-channel": "stable",
    "x-browser-copyright": "Copyright 2026 Google LLC. All Rights Reserved.",
    "x-browser-year": "2026",
    "x-goog-ext-353267353-jspb": "[null,null,null,194274]",
}

# Public Google key used to derive x-browser-validation (not a user credential).
_BROWSER_VALIDATION_KEYS = {
    "windows": "AIzaSyA2KlwBX3mkFo30om9LUFYQhpqLoa_BNhE",
    "linux": "AIzaSyBqJZh-7pA44blAaAkH6490hUFOwX0KCYM",
    "mac": "AIzaSyDr2UxVnv_U85AbhhY8XSHSIavUW0DC-sY",
}


def generate_x_browser_validation(ua: str = USER_AGENT) -> str:
    ua_lower = ua.lower()
    if "linux" in ua_lower:
        key = _BROWSER_VALIDATION_KEYS["linux"]
    elif "macintosh" in ua_lower or "mac os x" in ua_lower:
        key = _BROWSER_VALIDATION_KEYS["mac"]
    else:
        key = _BROWSER_VALIDATION_KEYS["windows"]
    digest = hashlib.sha1((key + ua).encode()).digest()
    return base64.b64encode(digest).decode()


def build_xhr_headers(
    content_type: str = "application/json",
    accept: str = "*/*",
    origin: str = "https://console.cloud.google.com",
    referer: str = "https://console.cloud.google.com/",
    site: str = "cross-site",
) -> Dict[str, str]:
    headers = dict(_FIXED)
    headers["accept"] = accept
    headers["origin"] = origin
    headers["referer"] = referer
    headers["sec-fetch-site"] = site
    headers["x-browser-validation"] = generate_x_browser_validation(USER_AGENT)
    if content_type:
        headers["content-type"] = content_type
    return headers
