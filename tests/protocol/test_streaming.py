"""SSE streaming helper tests."""

from __future__ import annotations

import pytest

from transport.streaming import parse_sse_line, sse_events


def test_parse_sse_line():
    event = parse_sse_line('data: {"ok": true}')
    assert event == {"ok": True}


def test_parse_done_line():
    assert parse_sse_line("data: [DONE]") == {}


def test_parse_non_data_line_raises():
    with pytest.raises(ValueError):
        parse_sse_line("event: foo")


async def test_sse_events_filter_and_parse():
    async def lines():
        yield 'data: {"a": 1}'
        yield ""
        yield "data: [DONE]"

    events = [e async for e in sse_events(lines())]
    assert events == [{"a": 1}]
