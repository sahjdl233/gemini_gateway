"""NDJSON streaming parser for Anonymous Vertex upstream.

The upstream response is NOT SSE. It is a raw stream of concatenated JSON objects
(one per network chunk, JSON-brace delimited). This module implements the
brace-counting scanner from stream_scanner.go, adapted to an async iterator of bytes.
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator, List, Optional

from providers.anonymous_vertex.errors import AnonymousVertexParseError


class StreamParseError(AnonymousVertexParseError):
    """Raised when the upstream stream is malformed or ends prematurely."""

    default_status = 502


FINISH_REASON_UNSPECIFIED = "FINISH_REASON_UNSPECIFIED"


class StreamingObjectScanner:
    """Incremental brace-counting JSON object scanner."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._scan_pos = 0

    def feed(self, data: bytes) -> List[bytes]:
        self._buffer.extend(data)
        objects: List[bytes] = []
        while True:
            obj = self._next_object()
            if obj is None:
                break
            objects.append(obj)
        return objects

    def _next_object(self) -> Optional[bytes]:
        if self._scan_pos >= len(self._buffer):
            self._scan_pos = 0
            self._buffer.clear()
            return None

        start = self._buffer.find(b"{", self._scan_pos)
        if start == -1:
            self._scan_pos = 0
            self._buffer.clear()
            return None

        brace_count = 0
        in_string = False
        escape = False
        for i in range(start, len(self._buffer)):
            ch = self._buffer[i]
            if escape:
                escape = False
                continue
            if ch == 0x5C:
                escape = True
                continue
            if ch == 0x22:
                in_string = not in_string
                continue
            if not in_string:
                if ch == 0x7B:
                    brace_count += 1
                elif ch == 0x7D:
                    brace_count -= 1
                    if brace_count == 0:
                        obj = bytes(self._buffer[start : i + 1])
                        del self._buffer[: i + 1]
                        self._scan_pos = 0
                        return obj

        self._scan_pos = start
        return None


def extract_chunk_from_frame(frame: bytes) -> Any:
    """Extract the Gemini chunk payload from a single upstream frame.

    Returns None if no usable chunk. Raises StreamParseError on malformed JSON
    or a classified upstream error.
    """
    from providers.anonymous_vertex.errors import (
        UpstreamVertexError,
        _parse_error_obj,
        classify_upstream_error,
    )

    try:
        env = json.loads(frame)
    except json.JSONDecodeError:
        raise StreamParseError(
            "malformed JSON object from upstream (protocol error)",
            provider="anonymous_vertex",
        )

    if not isinstance(env, dict):
        raise StreamParseError("malformed upstream frame", provider="anonymous_vertex")

    results = env.get("results")
    if not isinstance(results, list):
        return None

    for result in results:
        if not isinstance(result, dict):
            continue
        errors = result.get("errors")
        if errors:
            if isinstance(errors, list) and errors:
                first = errors[0]
                msg = first.get("message", "") if isinstance(first, dict) else str(first)
                if "Failed to verify action" in msg or "The caller does not have permission" in msg:
                    err = UpstreamVertexError(msg, status_code=502, kind="auth")
                    raise classify_upstream_error(err)
                parsed = _parse_error_obj({"errors": errors}, 500)
                if parsed is not None:
                    raise classify_upstream_error(parsed)
            continue

        data = result.get("data")
        if not isinstance(data, dict):
            continue

        ui = data.get("ui")
        payload = data
        if isinstance(ui, dict):
            inner = ui.get("streamGenerateContentAnonymous")
            if isinstance(inner, dict):
                payload = inner
            elif isinstance(inner, list):
                return inner
        return payload

    return None


async def iter_chunks(
    frame_iter: AsyncIterator[bytes],
    max_buffer: int = 64 * 1024 * 1024,
) -> AsyncIterator[Any]:
    """Iterate Gemini chunks from an async iterator of raw bytes."""
    scanner = StreamingObjectScanner()
    async for data in frame_iter:
        if not data:
            continue
        for obj in scanner.feed(data):
            if len(obj) > max_buffer:
                raise StreamParseError("upstream frame exceeds buffer limit", provider="anonymous_vertex")
            chunk = extract_chunk_from_frame(obj)
            if chunk is not None:
                yield chunk
    if scanner._buffer.strip():
        raise StreamParseError("stream ended with incomplete frame", provider="anonymous_vertex")


def normalize_chunk(chunk: Any) -> Any:
    """Normalize a raw chunk payload into a clean Gemini chunk dict."""
    if isinstance(chunk, list):
        normalized = []
        for item in chunk:
            n = _normalize_single(item)
            if n is not None:
                normalized.append(n)
        return normalized if normalized else None
    return _normalize_single(chunk)


def _normalize_single(chunk: Any) -> Optional[dict]:
    if not isinstance(chunk, dict):
        return None
    out = dict(chunk)

    cands = out.get("candidates")
    if isinstance(cands, list):
        cleaned = []
        for cand in cands:
            if not isinstance(cand, dict):
                continue
            c = dict(cand)
            content = c.get("content")
            if isinstance(content, dict):
                parts = content.get("parts", [])
                if isinstance(parts, list):
                    clean_parts = [_clean_part(p) for p in parts]
                    clean_parts = [p for p in clean_parts if p is not None]
                    cc = dict(content)
                    cc["role"] = content.get("role") or "model"
                    cc["parts"] = clean_parts
                    c["content"] = cc
            cleaned.append(c)
        out["candidates"] = cleaned

    for key in ("usageMetadata", "promptFeedback"):
        if key in out and not _truthy(out[key]):
            out.pop(key)

    if not _chunk_has_content(out):
        return None
    return out


def _clean_part(p: Any) -> Optional[dict]:
    if not isinstance(p, dict):
        return None
    out = dict(p)
    out.pop("data", None)

    if out.get("fileData") and not out["fileData"].get("fileUri") and not out["fileData"].get("mimeType"):
        out.pop("fileData")

    if out.get("functionCall"):
        fc = out["functionCall"]
        has_name = bool(fc.get("name"))
        has_args = isinstance(fc.get("args"), dict) and len(fc["args"]) > 0
        if not has_name and not has_args:
            out.pop("functionCall")
        elif has_name:
            if fc.get("args") is None:
                fc["args"] = {}
            elif isinstance(fc.get("args"), str):
                if fc["args"]:
                    try:
                        parsed = json.loads(fc["args"])
                        if isinstance(parsed, (dict, list)):
                            fc["args"] = parsed
                        else:
                            fc["args"] = {}
                    except json.JSONDecodeError:
                        fc["args"] = {}
                else:
                    fc["args"] = {}

    if out.get("functionResponse"):
        fr = out["functionResponse"]
        has_name = bool(fr.get("name"))
        has_resp = isinstance(fr.get("response"), dict) and len(fr["response"]) > 0
        if not has_name and not has_resp:
            out.pop("functionResponse")
        elif isinstance(fr.get("response"), str) and fr["response"]:
            fr["response"] = {"result": fr["response"]}

    if out.get("inlineData") and not out["inlineData"].get("data"):
        out.pop("inlineData")

    has_content = (
        out.get("text")
        or out.get("thought")
        or out.get("thoughtSignature")
        or out.get("inlineData")
        or out.get("fileData")
        or out.get("functionCall")
        or out.get("functionResponse")
        or out.get("executableCode")
        or out.get("codeExecutionResult")
        or out.get("videoMetadata")
        or out.get("mediaResolution")
    )
    return out if has_content else None


def _truthy(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v != ""
    if isinstance(v, (list, dict)):
        return len(v) > 0
    return True


def _chunk_has_content(chunk: dict) -> bool:
    if "candidates" in chunk and chunk["candidates"] is not None:
        return True
    if chunk.get("usageMetadata") or chunk.get("promptFeedback"):
        return True
    if chunk.get("modelVersion") or chunk.get("responseId") or chunk.get("createTime"):
        return True
    return False


def chunk_finish_reason(chunk: Any) -> Optional[str]:
    if isinstance(chunk, list):
        for item in chunk:
            fr = chunk_finish_reason(item)
            if fr:
                return fr
        return None
    if not isinstance(chunk, dict):
        return None
    cands = chunk.get("candidates")
    if not isinstance(cands, list) or not cands:
        return None
    cand = cands[0]
    if not isinstance(cand, dict):
        return None
    fr = cand.get("finishReason") or ""
    if fr and fr != FINISH_REASON_UNSPECIFIED:
        return fr
    return None
