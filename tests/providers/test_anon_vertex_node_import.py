"""ANON-007 acceptance tests: node text import / normalization."""

from __future__ import annotations

from pathlib import Path

import pytest

from providers.anonymous_vertex.node_import import (
    NodeLineParseError,
    endpoint_node_id,
    import_node_sources,
    import_node_text,
    normalized_endpoint,
    parse_node_line,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "anonymous_vertex" / "node_import_sample.txt"


def _load_fixture() -> list:
    """Fixture data lines (comments and blanks are not data)."""
    lines = []
    for raw in FIXTURE.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if stripped and not stripped.startswith("#"):
            lines.append(stripped)
    return lines


# ---------------------------------------------------------------------------
# parse_node_line: legal inputs
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("line", "host", "port", "label"),
    [
        ("example.com:443", "example.com", 443, None),
        ("  example.com:443  ", "example.com", 443, None),  # whitespace trimmed
        ("EXAMPLE.COM:443", "example.com", 443, None),  # lowercase normalize
        ("1.2.3.4:443", "1.2.3.4", 443, None),
        ("[2606:4700::1111]:443", "2606:4700::1111", 443, None),
        ("[2001:0DB8::0001]:8443", "2001:db8::1", 8443, None),  # canonical IPv6
        ("example.com:1", "example.com", 1, None),
        ("example.com:65535", "example.com", 65535, None),
        ("example.com:443#foo", "example.com", 443, "foo"),
        ("example.com:443#foo#bar", "example.com", 443, "foo#bar"),  # first '#'
        ("store.ubi.com.:443", "store.ubi.com", 443, None),  # trailing dot
        ("example.com:443#", "example.com", 443, None),  # empty label
        ("[::1]:6443", "::1", 6443, None),
    ],
)
def test_parse_legal_lines(line, host, port, label):
    assert parse_node_line(line) == (host, port, label)


# ---------------------------------------------------------------------------
# parse_node_line: illegal inputs
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "line",
    [
        ":443",              # empty host
        "example.com:",      # empty port
        "example.com:abc",   # non-numeric port
        "example.com:0",     # port below range
        "example.com:65536", # port above range
        "example.com:+443",  # sign is not a digit
        "[::1]",             # no port after bracket
        "[::1]:abc",         # non-numeric port after bracket
        "[::1:443",          # unclosed bracket
        "::1:443",           # bare IPv6 — must be rejected, not guessed
        "2606:4700::1111",   # bare IPv6 without any port
        "example.com",       # no port at all
        "host with space:443",
        "bad_host:443",      # underscore is not a hostname char
        "1.2.3.4.:443",      # dotted-quad with trailing dot is not a host
        "999.1.1.1:443",     # invalid IPv4 literal
    ],
)
def test_parse_illegal_lines_rejected(line):
    with pytest.raises(NodeLineParseError):
        parse_node_line(line)


def test_parse_error_carries_line_and_reason():
    with pytest.raises(NodeLineParseError) as ei:
        parse_node_line("example.com:abc")
    assert ei.value.line == "example.com:abc"
    assert "port" in ei.value.reason
    assert "abc" in str(ei.value)


# ---------------------------------------------------------------------------
# stable node_id
# ---------------------------------------------------------------------------
def test_node_id_stable_and_identity_relevant():
    a1 = endpoint_node_id("162.159.198.1", 443)
    a2 = endpoint_node_id("162.159.198.1", 443)
    assert a1 == a2
    assert a1.startswith("anv-")

    # host or port changes -> different id
    assert endpoint_node_id("162.159.198.1", 8443) != a1
    assert endpoint_node_id("162.159.197.1", 443) != a1

    # normalization feeds identity: case / trailing dot / IPv6 spelling
    assert endpoint_node_id("EXAMPLE.COM.", 443) == endpoint_node_id("example.com", 443)
    assert endpoint_node_id("2001:0DB8::0001", 443) == endpoint_node_id("2001:db8::1", 443)

    # normalized endpoint string: IPv6 uses bracket form
    assert normalized_endpoint("2606:4700::1111", 443) == "[2606:4700::1111]:443"
    assert normalized_endpoint("example.com", 443) == "example.com:443"


# ---------------------------------------------------------------------------
# dedup / label merge / provenance
# ---------------------------------------------------------------------------
def test_same_source_dedup_merges_labels():
    result = import_node_text(
        ["example.com:443#A", "example.com:443#B", "example.com:443#A"],
        "src-1",
    )
    assert len(result.endpoints) == 1
    ep = result.endpoints[0]
    assert (ep.host, ep.port) == ("example.com", 443)
    assert ep.labels == ("A", "B")  # merged, first-seen order, deduped
    assert ep.source_ids == ("src-1",)
    assert ep.raw_entries == ("example.com:443#A", "example.com:443#B")
    assert not result.rejected


def test_cross_source_merge_keeps_one_node():
    result = import_node_sources({
        "source-a": ["example.com:443#A"],
        "source-b": ["example.com:443#B"],
    })
    assert len(result.endpoints) == 1
    ep = result.endpoints[0]
    assert ep.labels == ("A", "B")
    assert ep.source_ids == ("source-a", "source-b")
    assert ep.raw_entries == ("example.com:443#A", "example.com:443#B")
    # identity independent of labels / sources
    assert ep.node_id == endpoint_node_id("example.com", 443)


def test_import_preserves_first_appearance_order():
    result = import_node_text(
        ["b.com:443#1", "a.com:443#2", "c.com:443#3", "a.com:443#4"], "s"
    )
    assert [ep.host for ep in result.endpoints] == ["b.com", "a.com", "c.com"]
    assert result.endpoints[1].labels == ("2", "4")


def test_blank_lines_ignored_and_invalid_lines_collected():
    result = import_node_text(
        [
            "",
            "   ",
            "example.com:443#ok",
            ":443",
            "example.com:abc",
            "::1:443",
        ],
        "s",
    )
    assert len(result.endpoints) == 1
    assert [r.raw_line for r in result.rejected] == [":443", "example.com:abc", "::1:443"]
    assert all(r.source_id == "s" for r in result.rejected)
    assert all(r.reason for r in result.rejected)


def test_case_and_trailing_dot_dedup_to_one_endpoint():
    result = import_node_text(
        ["example.com:443#A", "EXAMPLE.COM:443#B", "example.com.:443#C"], "s"
    )
    assert len(result.endpoints) == 1
    assert result.endpoints[0].labels == ("A", "B", "C")
    assert result.endpoints[0].node_id == endpoint_node_id("example.com", 443)


# ---------------------------------------------------------------------------
# semantic boundary: no protocol / transport inference
# ---------------------------------------------------------------------------
def test_no_protocol_or_transport_inference():
    result = import_node_text(
        ["162.159.198.1:443", "openai.com:443#MASQUE", "w.com:443#WireGuard|QUIC"],
        "s",
    )
    for ep in result.endpoints:
        # the import DTO exposes exactly these fields — no scheme, no
        # transport, no ProxyConfig
        assert set(ep.__dict__) == {
            "node_id", "host", "port", "labels", "source_ids", "raw_entries",
        }
    ep_plain = result.endpoints[0]
    assert ep_plain.host == "162.159.198.1" and ep_plain.port == 443
    assert ep_plain.labels == ()  # no label invented

    # a MASQUE/WireGuard label changes NOTHING about identity or shape
    ep_masque = import_node_text(["162.159.198.1:443#官方入口 | MASQUE1"], "s").endpoints[0]
    assert ep_masque.node_id == ep_plain.node_id
    assert ep_masque.host == "162.159.198.1"
    assert ep_masque.port == 443
    assert ep_masque.labels == ("官方入口 | MASQUE1",)  # kept verbatim, one label


def test_pipe_is_never_a_field_separator():
    host, port, label = parse_node_line("162.159.198.1:8443#EDT导航优选域名 | BestCF.pages.dev")
    assert (host, port) == ("162.159.198.1", 8443)
    assert label == "EDT导航优选域名 | BestCF.pages.dev"


# ---------------------------------------------------------------------------
# official fixture: the user's 32-line TXT
# ---------------------------------------------------------------------------
def test_user_fixture_full_parse():
    """The user's canonical 32-line TXT parses completely and faithfully."""
    lines = _load_fixture()
    assert len(lines) == 32

    result = import_node_text(lines, "sample")
    assert result.rejected == (), [r.raw_line for r in result.rejected]
    assert len(result.endpoints) == 32  # no duplicate endpoints in the TXT

    endpoints = {(ep.host, ep.port): ep for ep in result.endpoints}

    # :8443 and :443 are different endpoints (and different node ids)
    assert ("162.159.198.1", 8443) in endpoints
    assert ("162.159.198.1", 443) in endpoints
    assert (endpoints[("162.159.198.1", 8443)].node_id
            != endpoints[("162.159.198.1", 443)].node_id)

    # Chinese labels fully preserved, '|' never split, one label per line
    assert endpoints[("162.159.198.1", 8443)].labels == (
        "EDT导航优选域名 | BestCF.pages.dev",
    )
    assert endpoints[("162.159.197.1", 443)].labels == ("官方入口 | ZeroTrust",)
    assert endpoints[("162.159.193.1", 443)].labels == ("官方入口 | WireGuard",)
    assert endpoints[("www.mskcc.org", 443)].labels == ("更多选择 | MSKCC",)
    for ep in result.endpoints:
        assert len(ep.labels) == 1
        assert " | " in ep.labels[0]

    # hostname / IPv4 mix, real samples present
    hosts = {ep.host for ep in result.endpoints}
    for expected in (
        "openai.com",
        "www.decathlon.com",
        "chrono24.com",
        "www.wto.org",
        "store.ubi.com",
        "serviceshub.samsclub.com",
        "mycareer.verizon.com",
        "digitalocean.com",
    ):
        assert expected in hosts, expected
    assert sum(ep.host.startswith("162.159.") for ep in result.endpoints) == 5

    # every line yields a stable derived node id
    for ep in result.endpoints:
        assert ep.node_id == endpoint_node_id(ep.host, ep.port)
        assert ep.node_id.startswith("anv-")
        assert ep.source_ids == ("sample",)

    # re-import is deterministic
    assert import_node_text(lines, "sample") == result


def test_fixture_cross_source_view():
    """The same TXT split across two sources still yields one node per
    endpoint, with provenance merged (spec section 8)."""
    lines = _load_fixture()
    half = len(lines) // 2
    result = import_node_sources({"paste-1": lines[:half], "paste-2": lines[half:]})
    assert not result.rejected
    single = import_node_text(lines, "sample")
    assert [ep.node_id for ep in result.endpoints] == [
        ep.node_id for ep in single.endpoints
    ]
    # the canonical TXT has no duplicate endpoints, so each endpoint
    # keeps exactly the single source it came from — yet node identity is
    # identical to the single-source import (spec section 8)
    assert all(len(ep.source_ids) == 1 for ep in result.endpoints)
    assert {ep.source_ids[0] for ep in result.endpoints} == {"paste-1", "paste-2"}
    assert [ep.node_id for ep in result.endpoints] == [
        ep.node_id for ep in single.endpoints
    ]
