# TASK-005: Firebase Mock Streaming Tests
from __future__ import annotations
import json
import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from core.errors import ProviderError
from core.models import ChatMessage, ChatRequest
from providers.firebase.provider import FirebaseProvider
from providers.firebase.streaming import iter_chunks, iter_sse_events
from tests.providers._firebase_fakes import FakeHttp, FakeResponse, make_resource, make_sse

MODEL = "gemini-3.8-flash"

def text_event(text, finish_reason="STOP"):
    return {"candidates":[{"content":{"role":"model","parts":[{"text":text}]},"finishReason":finish_reason}]}

async def drain(agen):
    return [item async for item in agen]

def firebase_config():
    return {"scheduler":{"max_retries":2,"cooldown":{"base_delay":0.2,"factor":2.0,"max_delay":10.0,"jitter":0.1}},"providers":{"firebase":{"enabled":True,"models":["gemini-3.8-flash"],"resources":[{"id":"firebase-project-01","provider":"firebase","project_id":"test-project","api_key":"AIzaSyTESTAPIKEY","app_id":"1:12345:web:abc123","debug_token":"debug-token-0000"}]}}}

class TestSSEParser:
    async def test_single_chunk(self):
        body = make_sse(text_event("hi"))
        events = await drain(iter_sse_events(FakeResponse(200, sse_chunks=[body])))
        assert len(events) == 1
        assert events[0]["candidates"][0]["content"]["parts"][0]["text"] == "hi"
    async def test_multiple_chunks_and_done(self):
        body = make_sse(text_event("hello "), text_event("world"), "[DONE]")
        events = await drain(iter_sse_events(FakeResponse(200, sse_chunks=[body])))
        texts = [e["candidates"][0]["content"]["parts"][0]["text"] for e in events]
        assert texts == ["hello ", "world"]
    async def test_done_yields_no_events(self):
        events = await drain(iter_sse_events(FakeResponse(200, sse_chunks=[make_sse("[DONE]")])))
        assert events == []

class TestProviderStreaming:
    async def test_stream_yields_text_chunks(self):
        http = FakeHttp()
        provider = FirebaseProvider(http_client=http)
        http.responses.append(http.exchange_ok())
        sse = make_sse(text_event("Hello "), text_event("world"), text_event("", "STOP"), "[DONE]")
        http.stream_responses.append(FakeResponse(200, sse_chunks=[sse]))
        chunks = [c async for c in provider.stream(ChatRequest(model=MODEL, messages=[ChatMessage(role="user", content="hello")]), make_resource())]
        assert chunks[0].text == "Hello "
        assert chunks[1].text == "world"
        assert chunks[-1].finish_reason == "stop"
    async def test_stream_url_is_sse(self):
        http = FakeHttp()
        provider = FirebaseProvider(http_client=http)
        http.responses.append(http.exchange_ok())
        http.stream_responses.append(FakeResponse(200, sse_chunks=[make_sse("[DONE]")]))
        _ = [c async for c in provider.stream(ChatRequest(model=MODEL, messages=[ChatMessage(role="user", content="x")]), make_resource())]
        assert ":streamGenerateContent?alt=sse" in http.stream_calls[-1]["url"]
    async def test_stream_401_retries_with_fresh_jwt(self):
        http = FakeHttp()
        provider = FirebaseProvider(http_client=http)
        http.responses.append(http.exchange_ok(token="jwt-stale"))
        http.stream_responses.append(FakeResponse(401, content=b'{"error":{"message":"expired"}}'))
        http.responses.append(http.exchange_ok(token="jwt-fresh"))
        http.stream_responses.append(FakeResponse(200, sse_chunks=[make_sse(text_event("OK"), "[DONE]")]))
        chunks = [c async for c in provider.stream(ChatRequest(model=MODEL, messages=[ChatMessage(role="user", content="retry")]), make_resource())]
        assert chunks[0].text == "OK"
        assert http.stream_calls[-1]["headers"]["X-Firebase-AppCheck"] == "jwt-fresh"
    async def test_stream_401_twice_raises(self):
        http = FakeHttp()
        provider = FirebaseProvider(http_client=http)
        http.responses.append(http.exchange_ok(token="jwt-1"))
        http.stream_responses.append(FakeResponse(401, content=b'{"error":{"message":"expired"}}'))
        http.responses.append(http.exchange_ok(token="jwt-2"))
        http.stream_responses.append(FakeResponse(401, content=b'{"error":{"message":"still expired"}}'))
        with pytest.raises(ProviderError):
            _ = [c async for c in provider.stream(ChatRequest(model=MODEL, messages=[ChatMessage(role="user", content="fail")]), make_resource())]
    async def test_stream_not_ndjson(self):
        http = FakeHttp()
        provider = FirebaseProvider(http_client=http)
        http.responses.append(http.exchange_ok())
        ndjson_body = b'{"candidates":[{"content":{"parts":[{"text":"hi"}]}}]}\n{"candidates":[{"content":{"parts":[{"text":"there"}]}}]}\n'
        http.stream_responses.append(FakeResponse(200, sse_chunks=[ndjson_body]))
        chunks = [c async for c in provider.stream(ChatRequest(model=MODEL, messages=[ChatMessage(role="user", content="hi")]), make_resource())]
        assert chunks == []

class TestGatewaySSE:
    def _make_app(self, fake_http):
        from providers.firebase.factory import FirebaseProviderFactory as FPF
        original = FPF.create_provider
        FPF.create_provider = lambda self, pid, cfg=None: FirebaseProvider(http_client=fake_http, models=(cfg.get("models") if cfg else None))
        try:
            app = create_app(firebase_config())
        finally:
            FPF.create_provider = original
        return app
    async def test_gateway_sse_output(self):
        http = FakeHttp()
        http.responses.append(http.exchange_ok())
        sse = make_sse(text_event("Hello "), text_event("world"), "[DONE]")
        http.stream_responses.append(FakeResponse(200, sse_chunks=[sse]))
        app = self._make_app(http)
        client = TestClient(app)
        with client.stream("POST", "/v1/chat/completions", json={"model":"gemini-3.8-flash","messages":[{"role":"user","content":"hello"}],"stream":True}) as resp:
            assert resp.status_code == 200
            lines = [line for line in resp.iter_lines() if line]
        assert lines[0].startswith("data: {")
        first_payload = json.loads(lines[0].removeprefix("data: "))
        assert first_payload["object"] == "chat.completion.chunk"
        assert first_payload["choices"][0]["delta"]["content"] == "Hello "
        second_payload = json.loads(lines[1].removeprefix("data: "))
        assert second_payload["choices"][0]["delta"]["content"] == "world"
        assert lines[-1] == "data: [DONE]"
    async def test_gateway_sse_text_accumulates(self):
        http = FakeHttp()
        http.responses.append(http.exchange_ok())
        sse = make_sse(text_event("one "), text_event("two "), text_event("three"), "[DONE]")
        http.stream_responses.append(FakeResponse(200, sse_chunks=[sse]))
        app = self._make_app(http)
        client = TestClient(app)
        with client.stream("POST", "/v1/chat/completions", json={"model":"gemini-3.8-flash","messages":[{"role":"user","content":"count"}],"stream":True}) as resp:
            lines = [line for line in resp.iter_lines() if line]
        data_lines = [l for l in lines if l.startswith("data: {")]
        contents = [json.loads(l.removeprefix("data: "))["choices"][0]["delta"].get("content", "") for l in data_lines]
        assert contents == ["one ", "two ", "three"]

@pytest.fixture
def fake_http():
    return FakeHttp()
