# TASK-005: Firebase Provider Mock Integration - Full Gateway Chain
from __future__ import annotations
import logging
from typing import Any, Dict
import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from core.cooldown import CooldownManager
from core.errors import ModelNotFoundError, RateLimitError
from core.health import HealthState
from core.models import ChatMessage, ChatRequest
from core.pool import InMemoryPool
from core.scheduler import Scheduler
from protocol.openai import to_openai_chat_response
from providers.firebase.factory import FirebaseProviderFactory
from providers.firebase.provider import FirebaseProvider, DEFAULT_MODELS
from tests.conftest import FakeClock
from tests.providers._firebase_fakes import (
    FakeHttp, FakeResponse, make_resource, make_sse,
)

def response_json(text="OK"):
    return {"candidates":[{"content":{"role":"model","parts":[{"text":text}]},"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":10,"candidatesTokenCount":2,"thoughtsTokenCount":0,"totalTokenCount":12}}

def sse_body(*texts, finish="STOP"):
    events=[]
    for t in texts:
        events.append({"candidates":[{"content":{"role":"model","parts":[{"text":t}]}}]})
    events.append({"candidates":[{"content":{"role":"model","parts":[{"text":""}]},"finishReason":finish}],"usageMetadata":{"totalTokenCount":len(texts)*2}})
    events.append("[DONE]")
    return make_sse(*events)

def firebase_config(resources=None, models=None, max_retries=2):
    if resources is None:
        resources=[{"id":"firebase-project-01","provider":"firebase","project_id":"test-project","api_key":"AIzaSyTESTAPIKEY","app_id":"1:12345:web:abc123","debug_token":"debug-token-0000"}]
    if models is None:
        models=["gemini-3.8-flash"]
    return {"scheduler":{"max_retries":max_retries,"cooldown":{"base_delay":0.2,"factor":2.0,"max_delay":10.0,"jitter":0.1}},"providers":{"firebase":{"enabled":True,"models":models,"resources":resources}}}

def _load_fake_http(responses, stream_responses=None):
    http=FakeHttp()
    for r in responses:
        http.responses.append(r)
    if stream_responses:
        for r in stream_responses:
            http.stream_responses.append(r)
    return http
class TestAppCheck:
    @pytest.fixture(autouse=True)
    def _setup(self):
        self.http = FakeHttp()
        self.provider = FirebaseProvider(http_client=self.http)
        self.resource = make_resource()
    async def test_first_fetch_exchanges_debug_token(self):
        self.http.responses.append(self.http.exchange_ok(token="mock-app-check-jwt", ttl="3600s"))
        self.http.responses.append(FakeResponse(200, json_body=response_json()))
        resp = await self.provider.complete(ChatRequest(model="gemini-3.8-flash", messages=[ChatMessage(role="user", content="hello")]), self.resource)
        assert resp.text == "OK"
        assert len(self.http.post_calls) == 2
        assert "exchangeDebugToken" in self.http.post_calls[0]["url"]
        assert "generateContent" in self.http.post_calls[1]["url"]
        assert self.http.post_calls[1]["headers"]["X-Firebase-AppCheck"] == "mock-app-check-jwt"
    async def test_second_request_uses_cached_token(self):
        self.http.responses.append(self.http.exchange_ok(token="jwt-cached"))
        self.http.responses.append(FakeResponse(200, json_body=response_json()))
        req = ChatRequest(model="gemini-3.8-flash", messages=[ChatMessage(role="user", content="first")])
        await self.provider.complete(req, self.resource)
        first_call_count = len(self.http.post_calls)
        self.http.responses.append(FakeResponse(200, json_body=response_json()))
        await self.provider.complete(ChatRequest(model="gemini-3.8-flash", messages=[ChatMessage(role="user", content="second")]), self.resource)
        assert len(self.http.post_calls) == first_call_count + 1
        assert "generateContent" in self.http.post_calls[-1]["url"]
    async def test_near_expiry_triggers_refresh(self):
        self.http.responses.append(self.http.exchange_ok(token="jwt-a", ttl="3600s"))
        self.http.responses.append(FakeResponse(200, json_body=response_json()))
        req = ChatRequest(model="gemini-3.8-flash", messages=[ChatMessage(role="user", content="hello")])
        await self.provider.complete(req, self.resource)
        import time
        for cached_client in self.provider._clients.values():
            cached_client._auth._jwt_exp = time.time() + 100
            break
        self.http.responses.append(self.http.exchange_ok(token="jwt-b"))
        self.http.responses.append(FakeResponse(200, json_body=response_json()))
        await self.provider.complete(ChatRequest(model="gemini-3.8-flash", messages=[ChatMessage(role="user", content="after-expiry")]), self.resource)
        assert len(self.http.post_calls) == 4
        assert "exchangeDebugToken" in self.http.post_calls[2]["url"]
    async def test_401_forces_refresh_and_retries_once(self):
        self.http.responses.append(self.http.exchange_ok(token="jwt-stale"))
        self.http.responses.append(FakeResponse(401, content=b'{"error":{"message":"token expired"}}'))
        self.http.responses.append(self.http.exchange_ok(token="jwt-fresh"))
        self.http.responses.append(FakeResponse(200, json_body=response_json()))
        resp = await self.provider.complete(ChatRequest(model="gemini-3.8-flash", messages=[ChatMessage(role="user", content="retry")]), self.resource)
        assert resp.text == "OK"
        assert len(self.http.post_calls) == 4
        assert self.http.post_calls[3]["headers"]["X-Firebase-AppCheck"] == "jwt-fresh"
    async def test_401_twice_does_not_loop(self):
        self.http.responses.append(self.http.exchange_ok(token="jwt-1"))
        self.http.responses.append(FakeResponse(401, content=b'{"error":{"message":"still expired"}}'))
        self.http.responses.append(self.http.exchange_ok(token="jwt-2"))
        self.http.responses.append(FakeResponse(401, content=b'{"error":{"message":"still expired 2"}}'))
        from core.errors import ProviderError
        with pytest.raises(ProviderError):
            await self.provider.complete(ChatRequest(model="gemini-3.8-flash", messages=[ChatMessage(role="user", content="fail")]), self.resource)
        assert len(self.http.post_calls) == 4

class TestGenerateContent:
    async def test_response_converts_to_openai_format(self):
        http = _load_fake_http([FakeHttp().exchange_ok(), FakeResponse(200, json_body=response_json("OK"))])
        provider = FirebaseProvider(http_client=http)
        resp = await provider.complete(ChatRequest(model="gemini-3.8-flash", messages=[ChatMessage(role="user", content="Reply with exactly OK")]), make_resource())
        openai = to_openai_chat_response(resp)
        assert openai["object"] == "chat.completion"
        assert openai["choices"][0]["message"]["content"] == "OK"
        assert openai["choices"][0]["finish_reason"] == "stop"
        assert openai["usage"]["prompt_tokens"] == 10
        assert openai["usage"]["completion_tokens"] == 2
        assert openai["usage"]["total_tokens"] == 12

class TestGatewayAPI:
    def _make_app(self, fake_http, config=None):
        if config is None:
            config = firebase_config()
        def fake_create_provider(self_factory, provider_id, cfg=None):
            models = None
            if cfg and isinstance(cfg, dict):
                models = cfg.get("models")
            return FirebaseProvider(http_client=fake_http, models=models)
        from providers.firebase.factory import FirebaseProviderFactory as FPF
        original = FPF.create_provider
        FPF.create_provider = fake_create_provider
        try:
            app = create_app(config)
        finally:
            FPF.create_provider = original
        return app
    async def test_non_streaming_gateway(self):
        http = _load_fake_http([FakeHttp().exchange_ok(), FakeResponse(200, json_body=response_json("OK"))])
        app = self._make_app(http)
        client = TestClient(app)
        resp = client.post("/v1/chat/completions", json={"model":"gemini-3.8-flash","messages":[{"role":"user","content":"Reply with exactly OK"}],"stream":False})
        assert resp.status_code == 200
        body = resp.json()
        assert body["object"] == "chat.completion"
        assert body["choices"][0]["message"]["content"] == "OK"
        assert body["choices"][0]["finish_reason"] == "stop"
    async def test_streaming_gateway(self):
        http = _load_fake_http([FakeHttp().exchange_ok()], stream_responses=[FakeResponse(200, sse_chunks=[sse_body("OK")])])
        app = self._make_app(http)
        client = TestClient(app)
        with client.stream("POST", "/v1/chat/completions", json={"model":"gemini-3.8-flash","messages":[{"role":"user","content":"Reply with exactly OK"}],"stream":True}) as resp:
            assert resp.status_code == 200
            lines = [line for line in resp.iter_lines() if line]
        assert lines[-1] == "data: [DONE]"
        data_lines = [l for l in lines if l.startswith("data: {")]
        assert len(data_lines) >= 1
    async def test_models_endpoint_includes_firebase(self):
        http = FakeHttp()
        app = self._make_app(http, firebase_config(models=["gemini-3.8-flash","gemini-3.7-flash"]))
        client = TestClient(app)
        resp = client.get("/v1/models")
        assert resp.status_code == 200
        body = resp.json()
        ids = [m["id"] for m in body["data"]]
        assert "gemini-3.8-flash" in ids
        assert "gemini-3.7-flash" in ids
        assert all(m["owned_by"] == "firebase" for m in body["data"])
class TestMultiTurn:
    async def test_system_user_assistant_user(self):
        http = FakeHttp()
        provider = FirebaseProvider(http_client=http)
        resource = make_resource()
        http.responses.append(http.exchange_ok())
        http.responses.append(FakeResponse(200, json_body=response_json("Alice")))
        req = ChatRequest(model="gemini-3.8-flash", messages=[ChatMessage(role="system", content="You are a test assistant."),ChatMessage(role="user", content="My name is Alice."),ChatMessage(role="assistant", content="Nice to meet you."),ChatMessage(role="user", content="What is my name?")])
        resp = await provider.complete(req, resource)
        assert resp.text == "Alice"
        payload = http.post_calls[1]["json"]
        assert payload["systemInstruction"] == {"parts":[{"text":"You are a test assistant."}]}
        contents = payload["contents"]
        assert contents[0]["role"] == "user"
        assert contents[1]["role"] == "model"
        assert contents[2]["role"] == "user"
        assert len(contents) == 3
    async def test_system_prompt_not_in_contents(self):
        http = FakeHttp()
        provider = FirebaseProvider(http_client=http)
        http.responses.append(http.exchange_ok())
        http.responses.append(FakeResponse(200, json_body=response_json()))
        req = ChatRequest(model="gemini-3.8-flash", messages=[ChatMessage(role="system", content="secret"),ChatMessage(role="user", content="hello")])
        await provider.complete(req, make_resource())
        payload = http.post_calls[1]["json"]
        assert "systemInstruction" in payload
        assert len(payload["contents"]) == 1

class TestToolCalling:
    async def test_tools_in_payload(self):
        http = FakeHttp()
        provider = FirebaseProvider(http_client=http)
        http.responses.append(http.exchange_ok())
        http.responses.append(FakeResponse(200, json_body=response_json()))
        tools=[{"type":"function","function":{"name":"get_weather","description":"Get weather","parameters":{"type":"object","properties":{"city":{"type":"string"}}}}}]
        req = ChatRequest(model="gemini-3.8-flash", messages=[ChatMessage(role="user", content="weather")], tools=tools)
        await provider.complete(req, make_resource())
        payload = http.post_calls[1]["json"]
        assert "tools" in payload
        assert payload["tools"][0]["functionDeclarations"][0]["name"] == "get_weather"
        assert payload["toolConfig"]["functionCallingConfig"]["mode"] == "AUTO"
    async def test_function_call_response(self):
        fc_resp={"candidates":[{"content":{"role":"model","parts":[{"functionCall":{"name":"add","args":{"a":1,"b":2}}}]},"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":5,"candidatesTokenCount":3,"totalTokenCount":8}}
        http = _load_fake_http([FakeHttp().exchange_ok(), FakeResponse(200, json_body=fc_resp)])
        provider = FirebaseProvider(http_client=http)
        resp = await provider.complete(ChatRequest(model="gemini-3.8-flash", messages=[ChatMessage(role="user", content="add 1 and 2")]), make_resource())
        assert resp.tool_calls is not None
        assert resp.tool_calls[0]["function"]["name"] == "add"
    async def test_tool_choice_any(self):
        http = FakeHttp()
        provider = FirebaseProvider(http_client=http)
        http.responses.append(http.exchange_ok())
        http.responses.append(FakeResponse(200, json_body=response_json()))
        tools=[{"type":"function","function":{"name":"f","parameters":{"type":"object"}}}]
        req = ChatRequest(model="gemini-3.8-flash", messages=[ChatMessage(role="user", content="go")], tools=tools, tool_choice="required")
        await provider.complete(req, make_resource())
        assert http.post_calls[1]["json"]["toolConfig"]["functionCallingConfig"]["mode"] == "ANY"
    async def test_tool_choice_none(self):
        http = FakeHttp()
        provider = FirebaseProvider(http_client=http)
        http.responses.append(http.exchange_ok())
        http.responses.append(FakeResponse(200, json_body=response_json()))
        tools=[{"type":"function","function":{"name":"f","parameters":{"type":"object"}}}]
        req = ChatRequest(model="gemini-3.8-flash", messages=[ChatMessage(role="user", content="go")], tools=tools, tool_choice="none")
        await provider.complete(req, make_resource())
        assert http.post_calls[1]["json"]["toolConfig"]["functionCallingConfig"]["mode"] == "NONE"

class TestMultimodal:
    async def test_image_data_url_converted(self):
        http = FakeHttp()
        provider = FirebaseProvider(http_client=http)
        http.responses.append(http.exchange_ok())
        http.responses.append(FakeResponse(200, json_body=response_json()))
        req = ChatRequest(model="gemini-3.8-flash", messages=[ChatMessage(role="user", content=[{"type":"text","text":"describe"},{"type":"image_url","image_url":{"url":"data:image/png;base64,AAAA"}}])])
        await provider.complete(req, make_resource())
        parts = http.post_calls[1]["json"]["contents"][0]["parts"]
        assert parts[0] == {"text":"describe"}
        assert parts[1] == {"inline_data":{"mime_type":"image/png","data":"AAAA"}}
    async def test_audio_data_url_converted(self):
        http = FakeHttp()
        provider = FirebaseProvider(http_client=http)
        http.responses.append(http.exchange_ok())
        http.responses.append(FakeResponse(200, json_body=response_json()))
        req = ChatRequest(model="gemini-3.8-flash", messages=[ChatMessage(role="user", content=[{"type":"text","text":"transcribe"},{"type":"input_audio","input_audio":{"data":"data:audio/mpeg;base64,BBBB","format":"audio/mpeg"}}])])
        await provider.complete(req, make_resource())
        parts = http.post_calls[1]["json"]["contents"][0]["parts"]
        assert parts[-1] == {"inline_data":{"mime_type":"audio/mpeg","data":"BBBB"}}

class TestResourcePool:
    async def test_a_cooldown_b_succeeds(self, fake_clock):
        resource_a = make_resource(id="project-a", project_id="project-a")
        resource_b = make_resource(id="project-b", project_id="project-b")
        cooldown = CooldownManager(now_fn=lambda: fake_clock.now)
        pool = InMemoryPool(provider="firebase", resources=[resource_a, resource_b], cooldown=cooldown)
        resource_a.health = HealthState.COOLDOWN
        import datetime
        resource_a.cooldown_until = fake_clock.now + datetime.timedelta(hours=1)
        resource = await pool.acquire()
        assert resource is not None
        assert resource.id == "project-b"
        await pool.release(resource)
    async def test_two_resources_both_usable(self, fake_clock):
        resource_a = make_resource(id="project-a", project_id="project-a")
        resource_b = make_resource(id="project-b", project_id="project-b")
        pool = InMemoryPool(provider="firebase", resources=[resource_a, resource_b], cooldown=CooldownManager(now_fn=lambda: fake_clock.now))
        r1 = await pool.acquire()
        r2 = await pool.acquire()
        assert r1 is not None and r2 is not None
        assert r1.id != r2.id

class TestModelDiscovery:
    async def test_model_registry_includes_firebase(self):
        http = FakeHttp()
        provider = FirebaseProvider(http_client=http, models=["gemini-3.8-flash"])
        from core.model_registry import ModelRegistry
        registry = ModelRegistry(providers={"firebase": provider})
        models = await registry.list_models()
        ids = [m.id for m in models]
        assert "gemini-3.8-flash" in ids
    async def test_default_models(self):
        provider = FirebaseProvider()
        models = await provider.list_models()
        ids = [m.id for m in models]
        assert ids == DEFAULT_MODELS

class TestCredentialRedaction:
    SENSITIVE = ["AIzaSyTESTAPIKEY", "debug-token-0000", "mock-app-check-jwt"]
    async def test_400_no_leak(self):
        http = _load_fake_http([FakeHttp().exchange_ok(), FakeResponse(400, content=b'{"error":{"message":"bad request"}}')])
        provider = FirebaseProvider(http_client=http)
        from core.errors import ProviderError
        with pytest.raises(ProviderError) as ei:
            await provider.complete(ChatRequest(model="gemini-3.8-flash", messages=[ChatMessage(role="user", content="fail")]), make_resource())
        for s in self.SENSITIVE:
            assert s not in str(ei.value)
    async def test_429_no_leak(self):
        http = _load_fake_http([FakeHttp().exchange_ok(), FakeResponse(429, content=b'{"error":{"message":"quota"}}')])
        provider = FirebaseProvider(http_client=http)
        from core.errors import RateLimitError
        with pytest.raises(RateLimitError) as ei:
            await provider.complete(ChatRequest(model="gemini-3.8-flash", messages=[ChatMessage(role="user", content="fail")]), make_resource())
        for s in self.SENSITIVE:
            assert s not in str(ei.value)
    async def test_network_no_leak(self):
        http = FakeHttp()
        http.raise_exc = RuntimeError("connection refused to firebaseappcheck.googleapis.com")
        provider = FirebaseProvider(http_client=http)
        from core.errors import ProviderError
        with pytest.raises(ProviderError) as ei:
            await provider.complete(ChatRequest(model="gemini-3.8-flash", messages=[ChatMessage(role="user", content="fail")]), make_resource())
        for s in self.SENSITIVE:
            assert s not in str(ei.value)

@pytest.fixture
def fake_http():
    return FakeHttp()
@pytest.fixture
def fake_clock():
    return FakeClock()
