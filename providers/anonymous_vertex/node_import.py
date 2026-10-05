"""Node source text import / normalization for Anonymous Vertex (ANON-007).

Boundary: turn raw pasted TXT lines into standardized, deduplicated node
endpoints — nothing more.

    raw text -> parse -> normalized endpoint -> dedup / label merge
             -> NodeImportResult (ParsedNodeEndpoint tuples)

Deliberately NOT done here: HTTP/subscription fetching, reCAPTCHA probing,
admission checks, DNS lookups, ranking, cooldown, quarantine, persistence,
Admin API, WebUI — and above all NO protocol/transport inference.  A label
like ``MASQUE`` or ``WireGuard`` is provenance text, never a decided
transport configuration; the importer never produces a ProxyConfig.

Line formats (v1)::

    host:port
    host:port#label
    host:port#label | another label

Only the FIRST ``#`` splits endpoint from label; the whole remainder is ONE
label (``|`` is not a field separator).  IPv6 must use bracket form
``[IPv6]:port``; bare IPv6 is rejected instead of guessed.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

__all__ = [
    "ParsedNodeEndpoint",
    "NodeImportResult",
    "RejectedLine",
    "NodeImportError",
    "NodeLineParseError",
    "parse_node_line",
    "normalize_host",
    "normalized_endpoint",
    "endpoint_node_id",
    "import_node_text",
    "import_node_sources",
]

#: Length of the hex body of a derived node_id.
_NODE_ID_HEX_LEN = 16
#: Prefix of a derived node_id (anonymous-vertex node).
_NODE_ID_PREFIX = "anv-"

#: Default source_id for a single pasted text blob.
SOURCE_MANUAL = "manual"

#: Default source_id for a single pasted text blob.

_HOSTNAME_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)
_DOTTED_QUAD_RE = re.compile(r"^\d+(\.\d+){3}$")


class NodeImportError(ValueError):
    """Base class for node import failures."""


class NodeLineParseError(NodeImportError):
    """A single raw line could not be parsed into a node endpoint."""

    def __init__(self, line: str, reason: str) -> None:
        self.line = line
        self.reason = reason
        super().__init__(f"invalid node line {line!r}: {reason}")


@dataclass(frozen=True)
class ParsedNodeEndpoint:
    """Import-stage DTO for one deduplicated endpoint.

    This is NOT a NodeDefinition: raw text and provenance stay here so the
    execution-node definition never absorbs source noise.  ``node_id`` is
    stably derived from the normalized endpoint only — identical across
    imports, independent of labels and sources.
    """

    node_id: str
    host: str
    port: int
    labels: Tuple[str, ...] = ()
    source_ids: Tuple[str, ...] = ()
    raw_entries: Tuple[str, ...] = ()


@dataclass(frozen=True)
class RejectedLine:
    """One raw line that failed to parse (audit trail, not fatal)."""

    source_id: str
    raw_line: str
    reason: str


@dataclass(frozen=True)
class NodeImportResult:
    """Standardized import outcome across one or more sources."""

    endpoints: Tuple[ParsedNodeEndpoint, ...] = ()
    rejected: Tuple[RejectedLine, ...] = ()


# ---------------------------------------------------------------------------
# parsing / normalization
# ---------------------------------------------------------------------------
def normalize_host(raw_host: str) -> str:
    """Normalize a host to its canonical string form.

    * DNS hostnames: lowercased, one trailing dot removed (fixed policy),
      light per-label validation (no deep RFC/DNS implementation);
    * IP literals: canonical form via :mod:`ipaddress` (IPv6 compressed
      lowercase; IPv4 dotted quad) — an IP is never rewritten as a DNS
      name and vice versa;
    * bare IPv6 with multiple colons is NOT accepted here (the line
      parser requires bracket form).
    """
    host = raw_host.strip().lower()
    if not host:
        raise NodeImportError("host is empty")

    try:  # IP literal (IPv4 or bracket-stripped IPv6) -> canonical form
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass

    if ":" in host:  # looked like IPv6 but is not a valid address
        raise NodeImportError(f"invalid IPv6 literal: {raw_host!r}")
    if _DOTTED_QUAD_RE.match(host):  # looked like IPv4 but is not valid
        raise NodeImportError(f"invalid IPv4 literal: {raw_host!r}")

    if host.endswith("."):  # DNS trailing root dot: normalized away
        host = host[:-1]
    if _DOTTED_QUAD_RE.match(host):
        # a dotted quad that failed ip_address above is an invalid IP,
        # never a DNS hostname (even after a trailing-dot strip)
        raise NodeImportError(f"invalid IPv4 literal: {raw_host!r}")
    if not _HOSTNAME_RE.match(host):
        raise NodeImportError(f"invalid hostname: {raw_host!r}")
    return host


def normalized_endpoint(host: str, port: int) -> str:
    """Canonical endpoint string (bracket form for IPv6 hosts)."""
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def endpoint_node_id(host: str, port: int) -> str:
    """Stable node identity derived ONLY from the normalized endpoint.

    Same endpoint -> same id; different host or port -> different id;
    labels and source ids never participate.  The host is normalized
    first, so case / trailing-dot / IPv6 spelling cannot fork identity.
    """
    host = normalize_host(host)
    digest = hashlib.sha256(
        normalized_endpoint(host, port).encode("utf-8")
    ).hexdigest()
    return f"{_NODE_ID_PREFIX}{digest[:_NODE_ID_HEX_LEN]}"


def parse_node_line(line: str) -> Tuple[str, int, Optional[str]]:
    """Parse one raw line into ``(normalized_host, port, label)``.

    ``label`` is everything after the FIRST ``#`` (trimmed, ``None`` when
    the line has no label part or the label is empty).  Raises
    :class:`NodeLineParseError` on any invalid input.
    """
    stripped = (line or "").strip()
    if not stripped:
        raise NodeLineParseError(line, "empty line")

    if "#" in stripped:
        endpoint_text, label = stripped.split("#", 1)
        label = label.strip() or None
    else:
        endpoint_text, label = stripped, None
    endpoint_text = endpoint_text.strip()

    # --- endpoint split (IPv6-aware; never a naive split(":")) ---
    if endpoint_text.startswith("["):
        closing = endpoint_text.find("]")
        if closing == -1:
            raise NodeLineParseError(line, "unclosed IPv6 bracket")
        host_part = endpoint_text[1:closing]
        rest = endpoint_text[closing + 1:]
        if not rest.startswith(":"):
            raise NodeLineParseError(line, "missing port after IPv6 bracket")
        port_text = rest[1:]
    elif ":" in endpoint_text:
        if endpoint_text.count(":") > 1:
            raise NodeLineParseError(
                line,
                "bare IPv6 is not supported; use [IPv6]:port bracket form",
            )
        host_part, port_text = endpoint_text.split(":", 1)
    else:
        raise NodeLineParseError(line, "missing port (expected host:port)")

    try:
        host = normalize_host(host_part)
    except NodeImportError as exc:
        raise NodeLineParseError(line, str(exc)) from None

    if not port_text.isdigit():
        raise NodeLineParseError(line, f"invalid port: {port_text!r}")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise NodeLineParseError(line, f"port out of range: {port}")

    return host, port, label


# ---------------------------------------------------------------------------
# import / dedup / merge
# ---------------------------------------------------------------------------
@dataclass
class _Accumulator:
    """Merge state for one deduplicated endpoint (first-seen order)."""

    host: str
    port: int
    labels: List[str]
    source_ids: List[str]
    raw_entries: List[str]


def import_node_sources(sources: Mapping[str, Iterable[str]]) -> NodeImportResult:
    """Import several named text sources into deduplicated endpoints.

    * same normalized endpoint (within or across sources) -> ONE
      :class:`ParsedNodeEndpoint` with one stable ``node_id``;
    * labels / source_ids / raw_entries merge preserving first-appearance
      order, deduplicated;
    * blank / whitespace-only lines are ignored; invalid lines are
      collected in ``rejected`` (with reason) and never abort the batch.
    """
    order: List[Tuple[str, int]] = []
    merged: Dict[Tuple[str, int], _Accumulator] = {}
    rejected: List[RejectedLine] = []

    for source_id, lines in sources.items():
        for raw in lines:
            stripped = (raw or "").strip()
            if not stripped:
                continue
            try:
                host, port, label = parse_node_line(stripped)
            except NodeLineParseError as exc:
                rejected.append(
                    RejectedLine(
                        source_id=source_id,
                        raw_line=stripped,
                        reason=exc.reason,
                    )
                )
                continue

            key = (host, port)
            acc = merged.get(key)
            if acc is None:
                acc = _Accumulator(host, port, [], [], [])
                merged[key] = acc
                order.append(key)
            if label and label not in acc.labels:
                acc.labels.append(label)
            if source_id not in acc.source_ids:
                acc.source_ids.append(source_id)
            if stripped not in acc.raw_entries:
                acc.raw_entries.append(stripped)

    endpoints = tuple(
        ParsedNodeEndpoint(
            node_id=endpoint_node_id(host, port),
            host=host,
            port=port,
            labels=tuple(merged[(host, port)].labels),
            source_ids=tuple(merged[(host, port)].source_ids),
            raw_entries=tuple(merged[(host, port)].raw_entries),
        )
        for host, port in order
    )
    return NodeImportResult(endpoints=endpoints, rejected=tuple(rejected))


def import_node_text(
    lines: Iterable[str], source_id: str = SOURCE_MANUAL
) -> NodeImportResult:
    """Convenience wrapper: import one pasted text blob as one source."""
    return import_node_sources({source_id: lines})


