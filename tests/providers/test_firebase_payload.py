"""TASK-004 tests: OpenAI ChatRequest -> Gemini generateContent payload."""
from __future__ import annotations

from protocol.openai import parse_openai_chat_request
from providers.firebase.payload import build_payload
from core.models import ChatMessage, ChatRequest


def make_request(messages, **kwargs):
    return ChatRequest(model="gemini-3.8-flash", messages=messages, **kwargs)


def test_system_and_user():
    req = make_request(
        [
            ChatMessage(role="system", content="You are a helpful assistant"),
            ChatMessage(role="user", content="hello"),
        ]
    )
    payload = build_payload(req)
    assert payload["systemInstruction"] == {
        "parts": [{"text": "You are a helpful assistant"}]
    }
    assert payload["contents"] == [
        {"role": "user", "parts": [{"text": "hello"}]}
    ]


def test_assistant_content_becomes_model():
    req = make_request(
        [
            ChatMessage(role="user", content="hi"),
            ChatMessage(role="assistant", content="hello there"),
        ]
    )
    payload = build_payload(req)
    assert payload["contents"][1]["role"] == "model"
    assert payload["contents"][1]["parts"] == [{"text": "hello there"}]


def test_tool_message_becomes_functionResponse():
    req = make_request(
        [
            ChatMessage(role="user", content="what is 2+2?"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "add", "arguments": "{\"a\":1,\"b\":1}"},
                    }
                ],
            ),
            ChatMessage(role="tool", content="4", name="add"),
        ]
    )
    payload = build_payload(req)
    # assistant -> model with functionCall part
    assert payload["contents"][1]["role"] == "model"
    fc_part = payload["contents"][1]["parts"][0]
    assert fc_part["functionCall"]["name"] == "add"
    assert fc_part["functionCall"]["args"] == {"a": 1, "b": 1}
    # tool -> user with functionResponse part
    assert payload["contents"][2]["role"] == "user"
    # "4" is valid JSON, so it parses to the number 4
    assert payload["contents"][2]["parts"][0]["functionResponse"] == {
        "name": "add",
        "response": 4,
    }


def test_tools_and_tool_choice():
    req = make_request(
        [ChatMessage(role="user", content="call a tool")],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather",
                    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                },
            }
        ],
        tool_choice={
            "type": "function",
            "function": {"name": "get_weather"},
        },
    )
    payload = build_payload(req)
    assert payload["tools"] == [
        {
            "functionDeclarations": [
                {
                    "name": "get_weather",
                    "description": "Get weather",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                    },
                }
            ]
        }
    ]
    assert payload["toolConfig"] == {
        "functionCallingConfig": {
            "mode": "ANY",
            "allowed_function_names": ["get_weather"],
        }
    }


def test_tool_choice_none_and_required():
    req = make_request(
        [ChatMessage(role="user", content="hi")],
        tools=[
            {
                "type": "function",
                "function": {"name": "f", "parameters": {"type": "object"}},
            }
        ],
        tool_choice="none",
    )
    payload = build_payload(req)
    assert payload["toolConfig"]["functionCallingConfig"]["mode"] == "NONE"

    req2 = make_request(
        [ChatMessage(role="user", content="hi")],
        tools=[
            {
                "type": "function",
                "function": {"name": "f", "parameters": {"type": "object"}},
            }
        ],
        tool_choice="required",
    )
    assert build_payload(req2)["toolConfig"]["functionCallingConfig"]["mode"] == "ANY"


def test_generation_config_full():
    req = make_request(
        [ChatMessage(role="user", content="hi")],
        temperature=0.3,
        max_tokens=256,
        max_completion_tokens=512,
        top_p=0.9,
        stop=["END", "STOP"],
    )
    gc = build_payload(req)["generationConfig"]
    assert gc["temperature"] == 0.3
    assert gc["maxOutputTokens"] == 256  # max_tokens wins over max_completion_tokens
    assert gc["topP"] == 0.9
    assert gc["stopSequences"] == ["END", "STOP"]


def test_max_completion_tokens_fallback():
    req = make_request(
        [ChatMessage(role="user", content="hi")],
        max_completion_tokens=512,
    )
    assert build_payload(req)["generationConfig"]["maxOutputTokens"] == 512


def test_reasoning_effort_thinking_levels():
    for effort, level in [("low", "LOW"), ("medium", "MEDIUM"), ("high", "HIGH")]:
        req = make_request(
            [ChatMessage(role="user", content="hi")],
            reasoning_effort=effort,
        )
        assert build_payload(req)["generationConfig"]["thinkingConfig"] == {
            "thinkingLevel": level
        }


def test_reasoning_effort_none_no_thinking():
    req = make_request([ChatMessage(role="user", content="hi")])
    gc = build_payload(req).get("generationConfig", {})
    assert "thinkingConfig" not in gc


def test_multimodal_image_data_url():
    req = make_request(
        [
            ChatMessage(
                role="user",
                content=[
                    {"type": "text", "text": "describe this"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                ],
            )
        ]
    )
    payload = build_payload(req)
    parts = payload["contents"][0]["parts"]
    assert parts == [
        {"text": "describe this"},
        {"inline_data": {"mime_type": "image/png", "data": "AAAA"}},
    ]


def test_multimodal_audio_data_url():
    req = make_request(
        [
            ChatMessage(
                role="user",
                content=[
                    {"type": "text", "text": "transcribe"},
                    {
                        "type": "input_audio",
                        "input_audio": {"data": "data:audio/mpeg;base64,BBBB", "format": "audio/mpeg"},
                    },
                ],
            )
        ]
    )
    payload = build_payload(req)
    parts = payload["contents"][0]["parts"]
    assert parts[-1] == {
        "inline_data": {"mime_type": "audio/mpeg", "data": "BBBB"}
    }


def test_plain_text_only():
    req = make_request([ChatMessage(role="user", content="hello")])
    payload = build_payload(req)
    assert payload == {
        "contents": [{"role": "user", "parts": [{"text": "hello"}]}]
    }


def test_openai_parser_passes_firebase_fields():
    parsed = parse_openai_chat_request(
        {
            "model": "gemini-3.8-flash",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.2,
            "max_tokens": 100,
            "top_p": 0.8,
            "stop": ["."],
            "tool_choice": {"type": "function", "function": {"name": "f"}},
            "reasoning_effort": "low",
        }
    )
    assert parsed.top_p == 0.8
    assert parsed.stop == ["."]
    assert parsed.tool_choice["function"]["name"] == "f"
    assert parsed.reasoning_effort == "low"
    gc = build_payload(parsed)["generationConfig"]
    assert gc["topP"] == 0.8
    assert gc["thinkingConfig"] == {"thinkingLevel": "LOW"}
