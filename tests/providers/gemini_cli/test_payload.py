"""OpenAI request -> Code Assist envelope (TASK-008)."""
from __future__ import annotations

from typing import Any, Dict, List

from core.models import ChatMessage, ChatRequest
from providers.gemini_cli.payload import (
    build_payload,
    build_envelope,
    get_model_name,
    _convert_messages,
    _convert_tools,
    _convert_tool_choice,
    _build_generation_config,
    _build_thinking_config,
    _is_gemini_2,
)


def test_system_message_becomes_system_instruction():
    req = ChatRequest(
        model="gemini-2.5-flash",
        messages=[
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="user", content="Hello"),
        ],
    )
    payload = build_payload(req)
    assert "systemInstruction" in payload
    assert payload["systemInstruction"]["parts"][0]["text"] == "You are a helpful assistant."
    assert len(payload["contents"]) == 1
    assert payload["contents"][0]["role"] == "user"


def test_user_message_becomes_content():
    req = ChatRequest(
        model="gemini-2.5-flash",
        messages=[ChatMessage(role="user", content="Hello world")],
    )
    payload = build_payload(req)
    assert payload["contents"][0]["role"] == "user"
    assert payload["contents"][0]["parts"][0]["text"] == "Hello world"


def test_assistant_with_tool_calls():
    req = ChatRequest(
        model="gemini-2.5-flash",
        messages=[
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"location": "Tokyo"}',
                        },
                    }
                ],
            )
        ],
    )
    payload = build_payload(req)
    parts = payload["contents"][0]["parts"]
    assert len(parts) == 1
    fc = parts[0]["functionCall"]
    assert fc["name"] == "get_weather"
    assert fc["args"]["location"] == "Tokyo"


def test_tool_message_becomes_function_response():
    req = ChatRequest(
        model="gemini-2.5-flash",
        messages=[
            ChatMessage(role="assistant", content="", tool_calls=[
                {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}
            ]),
            ChatMessage(role="tool", content='{"temp": 20}', name="get_weather"),
        ],
    )
    payload = build_payload(req)
    # content list should have user role with functionResponse
    assert payload["contents"][1]["role"] == "user"
    assert "functionResponse" in payload["contents"][1]["parts"][0]


def test_tools_and_tool_choice():
    req = ChatRequest(
        model="gemini-2.5-flash",
        messages=[ChatMessage(role="user", content="x")],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather",
                    "parameters": {"type": "object", "properties": {"location": {"type": "string"}}},
                },
            }
        ],
        tool_choice="required",
    )
    payload = build_payload(req)
    assert "tools" in payload
    assert payload["tools"][0]["functionDeclarations"][0]["name"] == "get_weather"
    assert payload["toolConfig"]["functionCallingConfig"]["mode"] == "ANY"


def test_tool_choice_none():
    req = ChatRequest(
        model="gemini-2.5-flash",
        messages=[ChatMessage(role="user", content="x")],
        tools=[],
        tool_choice="none",
    )
    payload = build_payload(req)
    assert "tools" not in payload
    # tool_choice=none should still produce toolConfig.NONE
    # but only if tools exist - in this case it won't be there
    # implementation adds toolConfig only when tools exist


def test_temperature_max_tokens_top_p_stop():
    req = ChatRequest(
        model="gemini-2.5-flash",
        messages=[ChatMessage(role="user", content="x")],
        temperature=0.7,
        max_tokens=100,
        top_p=0.9,
        stop=["###", "END"],
    )
    payload = build_payload(req)
    gc = payload["generationConfig"]
    assert gc["temperature"] == 0.7
    assert gc["maxOutputTokens"] == 100
    assert gc["topP"] == 0.9
    assert gc["stopSequences"] == ["###", "END"]


def test_max_completion_tokens_fallback():
    req = ChatRequest(
        model="gemini-2.5-flash",
        messages=[ChatMessage(role="user", content="x")],
        max_completion_tokens=123,
    )
    payload = build_payload(req)
    assert payload["generationConfig"]["maxOutputTokens"] == 123


def test_thinking_budget_gemini_2():
    req = ChatRequest(
        model="gemini-2.5-flash",
        messages=[ChatMessage(role="user", content="x")],
        reasoning_effort="high",
    )
    payload = build_payload(req)
    tc = payload["generationConfig"].get("thinkingConfig", {})
    assert tc["thinkingBudget"] == 16000


def test_thinking_level_gemini_3():
    req = ChatRequest(
        model="gemini-3.8-flash",
        messages=[ChatMessage(role="user", content="x")],
        reasoning_effort="medium",
    )
    payload = build_payload(req)
    tc = payload["generationConfig"].get("thinkingConfig", {})
    assert tc["thinkingLevel"] == "MEDIUM"


def test_thinking_unknown_ignored():
    req = ChatRequest(
        model="gemini-2.5-flash",
        messages=[ChatMessage(role="user", content="x")],
        reasoning_effort="unknown",
        temperature=0.5,
    )
    payload = build_payload(req)
    assert "thinkingConfig" not in payload["generationConfig"]


def test_safety_settings_always_present():
    req = ChatRequest(
        model="gemini-2.5-flash",
        messages=[ChatMessage(role="user", content="x")],
    )
    payload = build_payload(req)
    safety = payload["safetySettings"]
    assert len(safety) == 10
    for s in safety:
        assert s["threshold"] == "BLOCK_NONE"


def test_build_envelope_wraps():
    req = ChatRequest(model="gemini-2.5-flash", messages=[ChatMessage(role="user", content="hi")])
    env = build_envelope(req, project="proj-123")
    assert env["model"] == "gemini-2.5-flash"
    assert env["project"] == "proj-123"
    assert "request" in env
    assert "contents" in env["request"]


def test_get_model_name():
    req = ChatRequest(model="gemini-2.5-flash", messages=[])
    assert get_model_name(req) == "gemini-2.5-flash"


def test_is_gemini_2_detection():
    assert _is_gemini_2("gemini-2.5-flash") is True
    assert _is_gemini_2("gemini-2.5-pro") is True
    assert _is_gemini_2("gemini-3.8-flash") is False
    assert _is_gemini_2("gemini-3.0-pro") is False
