"""GATEWAY-003 contract tests for the provider-independent tool-call seam."""

from __future__ import annotations

import json

import pytest

from core.models import ChatMessage, ChatRequest
from protocol.openai import parse_openai_chat_request, to_openai_chat_response
from providers.antigravity.provider import AntigravityProvider
from providers.antigravity.resource import AntigravityResource
from providers.firebase.payload import build_payload as firebase_payload
from providers.firebase.response import parse_response as firebase_response
from providers.firebase.streaming import iter_chunks as firebase_stream_chunks
from providers.gemini_cli.payload import build_payload as cli_payload
from providers.gemini_cli.response import parse_response as cli_response
from providers.gemini_cli.streaming import iter_chunks as cli_stream_chunks
from protocol.common import tool_call_deltas


CALLS = [
    {
        "id": "call-weather",
        "type": "function",
        "function": {"name": "get_weather", "arguments": '{"city":"London"}'},
    },
    {
        "id": "call-calendar",
        "type": "function",
        "function": {"name": "create_event", "arguments": '{"title":"demo"}'},
    },
]


def _messages_with_out_of_order_results() -> list[ChatMessage]:
    return [
        ChatMessage(role="user", content="do both"),
        ChatMessage(role="assistant", content=None, tool_calls=CALLS),
        ChatMessage(role="tool", tool_call_id="call-calendar", content='{"ok":true}'),
        ChatMessage(role="tool", tool_call_id="call-weather", content='{"temp":12}'),
    ]


def _function_response_names(payload: dict) -> list[str]:
    return [
        part["functionResponse"]["name"]
        for content in payload["contents"]
        for part in content["parts"]
        if "functionResponse" in part
    ]


def test_openai_parser_preserves_tool_call_id():
    request = parse_openai_chat_request(
        {
            "model": "gemini-3.8-flash",
            "messages": [
                {"role": "tool", "tool_call_id": "call-1", "content": "ok"}
            ],
        }
    )
    assert request.messages[0].tool_call_id == "call-1"


@pytest.mark.parametrize("builder", [firebase_payload, cli_payload])
def test_gemini_payloads_bind_tool_results_by_id_not_fifo(builder):
    payload = builder(ChatRequest(model="m", messages=_messages_with_out_of_order_results()))
    assert _function_response_names(payload) == ["create_event", "get_weather"]


def test_firebase_function_call_has_complete_openai_shape():
    response = firebase_response(
        {
            "candidates": [{
                "content": {"parts": [{"functionCall": {
                    "name": "get_weather", "args": {"city": "London"}
                }}]},
                "finishReason": "STOP",
            }]
        },
        "m",
    )
    call = response.tool_calls[0]
    assert call["id"]
    assert call["type"] == "function"
    assert call["function"] == {
        "name": "get_weather", "arguments": '{"city": "London"}'
    }
    assert response.finish_reason == "tool_calls"


def test_gemini_cli_function_call_has_complete_openai_shape():
    response = cli_response(
        {"response": {"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "get_weather", "args": {"city": "London"}}}
        ]}}]}},
        "m",
    )
    call = response.tool_calls[0]
    assert call["id"]
    assert call["type"] == "function"
    assert call["function"]["name"] == "get_weather"
    assert json.loads(call["function"]["arguments"]) == {"city": "London"}
    assert response.finish_reason == "tool_calls"


def test_antigravity_request_and_response_cover_function_calls():
    provider = AntigravityProvider(backend=None)
    request = ChatRequest(
        model="m",
        messages=[
            ChatMessage(role="assistant", content=None, tool_calls=[CALLS[0]]),
            ChatMessage(role="tool", tool_call_id="call-weather", content='{"temp":12}'),
        ],
    )
    payload = provider._build_payload(request, AntigravityResource(id="r", project_id="p"))
    assert payload["request"]["contents"][0]["parts"][0]["functionCall"]["name"] == "get_weather"
    assert payload["request"]["contents"][1]["parts"][0]["functionResponse"]["name"] == "get_weather"

    response = provider._parse_response(
        {"response": {"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "get_weather", "args": {"city": "London"}}}
        ]}, "finishReason": "STOP"}]}},
        "m",
    )
    assert response.tool_calls[0]["type"] == "function"
    assert response.tool_calls[0]["function"]["name"] == "get_weather"
    assert response.finish_reason == "tool_calls"


class _SseResponse:
    def __init__(self, events: list[dict]):
        self.events = [f"data: {json.dumps(event)}\n".encode() for event in events]

    async def aiter_bytes(self):
        for event in self.events:
            yield event


@pytest.mark.asyncio
async def test_streaming_tool_call_id_index_and_arguments_are_stable():
    events = [
        {"candidates": [{"content": {"parts": [{"functionCall": {
            "name": "get_weather", "args": '{"city":'
        }}]}}]},
        {"candidates": [{"content": {"parts": [{"functionCall": {
            "name": "get_weather", "args": '{"city":"London"}'
        }}]}, "finishReason": "STOP"}]},
    ]
    chunks = [chunk async for chunk in firebase_stream_chunks(_SseResponse(events), "m")]
    first, second = chunks
    assert first.tool_calls[0]["id"] == second.tool_calls[0]["id"]
    assert first.tool_calls[0]["index"] == second.tool_calls[0]["index"] == 0
    assert first.tool_calls[0]["function"]["arguments"] == '{"city":'
    assert second.tool_calls[0]["function"]["arguments"] == '"London"}'
    assert first.finish_reason is None
    assert second.finish_reason == "tool_calls"


@pytest.mark.asyncio
async def test_gemini_cli_streaming_uses_same_tool_contract():
    events = [
        {
            "response": {
                "candidates": [{
                    "content": {"parts": [{
                        "functionCall": {"name": "get_weather", "args": '{"city":'}
                    }]}
                }]
            }
        },
        {
            "response": {
                "candidates": [{
                    "content": {"parts": [{
                        "functionCall": {"name": "get_weather", "args": '{"city":"London"}'}
                    }]},
                    "finishReason": "STOP",
                }]
            }
        },
    ]
    chunks = [chunk async for chunk in cli_stream_chunks(_SseResponse(events), "m")]
    assert chunks[0].tool_calls[0]["id"] == chunks[1].tool_calls[0]["id"]
    assert [chunk.tool_calls[0]["index"] for chunk in chunks] == [0, 0]
    assert chunks[0].finish_reason is None
    assert chunks[1].finish_reason == "tool_calls"


def test_streaming_index_is_stable_when_calls_arrive_in_separate_chunks():
    state = {}
    first = tool_call_deltas([
        {"id": "a", "function": {"name": "one", "arguments": "{}"}}
    ], state)
    second = tool_call_deltas([
        {"id": "b", "function": {"name": "two", "arguments": "{}"}}
    ], state)
    assert first[0]["index"] == 0
    assert second[0]["index"] == 1


class _AntigravityStreamResponse:
    status_code = 200
    headers = {}

    def __init__(self, events: list[dict]):
        self.events = [f"data: {json.dumps(event)}\n".encode() for event in events]

    async def aiter_bytes(self):
        for event in self.events:
            yield event

    async def aclose(self):
        pass


class _AntigravityStreamBackend:
    def __init__(self, response):
        self.response = response

    async def execute_stream(self, method, url, **kwargs):
        return self.response

    async def execute(self, method, url, **kwargs):
        raise AssertionError("non-stream request was not expected")

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_antigravity_streaming_tool_call_contract():
    response = _AntigravityStreamResponse([
        {
            "response": {
                "candidates": [{
                    "content": {"parts": [{
                        "functionCall": {"name": "get_weather", "args": '{"city":'}
                    }]}
                }]
            }
        },
        {
            "response": {
                "candidates": [{
                    "content": {"parts": [{
                        "functionCall": {"name": "get_weather", "args": '{"city":"London"}'}
                    }]},
                    "finishReason": "STOP",
                }]
            }
        },
    ])
    provider = AntigravityProvider(backend=_AntigravityStreamBackend(response))
    request = ChatRequest(model="m", messages=[ChatMessage(role="user", content="weather")])
    chunks = [chunk async for chunk in provider.stream(
        request, AntigravityResource(id="r", project_id="p")
    )]
    assert chunks[0].tool_calls[0]["id"] == chunks[1].tool_calls[0]["id"]
    assert [chunk.tool_calls[0]["index"] for chunk in chunks] == [0, 0]
    assert chunks[0].tool_calls[0]["function"]["arguments"] == '{"city":'
    assert chunks[1].tool_calls[0]["function"]["arguments"] == '"London"}'
    assert chunks[1].finish_reason == "tool_calls"


def test_round_trip_keeps_tool_call_id_for_tool_result():
    first = parse_openai_chat_request({
        "model": "m",
        "messages": [{"role": "user", "content": "weather"}],
    })
    assistant = firebase_response(
        {"candidates": [{"content": {"parts": [{"functionCall": {
            "name": "get_weather", "args": {"city": "London"}
        }}]}}]},
        "m",
    )
    openai_assistant = to_openai_chat_response(assistant)["choices"][0]["message"]
    second = parse_openai_chat_request({
        "model": "m",
        "messages": [
            {"role": "user", "content": "weather"},
            openai_assistant,
            {"role": "tool", "tool_call_id": openai_assistant["tool_calls"][0]["id"], "content": '{"temp":12}'},
        ],
    })
    payload = firebase_payload(second)
    assert _function_response_names(payload) == ["get_weather"]
