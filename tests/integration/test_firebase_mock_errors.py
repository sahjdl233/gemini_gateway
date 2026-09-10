# TASK-005: Firebase Mock Error Matrix
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from core.cooldown import CooldownManager
from core.errors import AuthenticationError, AuthorizationError, InvalidRequestError, ModelNotFoundError, NetworkError, ProviderError, RateLimitError, TimeoutError, UpstreamUnavailableError, is_retryable
from core.health import HealthState
from core.models import ChatMessage, ChatRequest
from core.pool import InMemoryPool
from core.scheduler import Scheduler
from providers.firebase.errors import FirebaseAuthError, FirebaseNetworkError, FirebaseRateLimitError, FirebaseTimeoutError, FirebaseUnavailableError, classify_http_error
from providers.firebase.provider import FirebaseProvider
from tests.conftest import FakeClock
from tests.providers._firebase_fakes import FakeHttp, FakeResponse, make_resource, make_sse

def error_body(message="boom"):
    return ('{"error": {"message": "%s"}}' % message).encode()

def firebase_config():
    return {"scheduler":{"max_retries":2,"cooldown":{"base_delay":0.2,"factor":2.0,"max_delay":10.0,"jitter":0.1}},"providers":{"firebase":{"enabled":True,"models":["gemini-3.8-flash"],"resources":[{"id":"firebase-project-01","provider":"firebase","project_id":"test-project","api_key":"AIzaSyTESTAPIKEY","app_id":"1:12345:web:abc123","debug_token":"debug-token-0000"}]}}}

class TestErrorMatrix:
    async def test_200_normal(self):
        http = FakeHttp()
        http.responses.append(http.exchange_ok())
        http.responses.append(FakeResponse(200, json_body={"candidates":[{"content":{"role":"model","parts":[{"text":"OK"}]},"finishReason":"STOP"}],"usageMetadata":{"totalTokenCount":5}}))
        provider = FirebaseProvider(http_client=http)
        resp = await provider.complete(ChatRequest(model="gemini-3.8-flash", messages=[ChatMessage(role="user", content="hi")]), make_resource())
        assert resp.text == "OK"
    async def test_400_invalid_request(self):
        http = FakeHttp()
        http.responses.append(http.exchange_ok())
        http.responses.append(FakeResponse(400, content=error_body("bad request")))
        provider = FirebaseProvider(http_client=http)
        with pytest.raises(InvalidRequestError):
            await provider.complete(ChatRequest(model="gemini-3.8-flash", messages=[ChatMessage(role="user", content="hi")]), make_resource())
    async def test_401_after_retry(self):
        http = FakeHttp()
        http.responses.append(http.exchange_ok(token="jwt-1"))
        http.responses.append(FakeResponse(401, content=error_body("expired")))
        http.responses.append(http.exchange_ok(token="jwt-2"))
        http.responses.append(FakeResponse(401, content=error_body("still expired")))
        provider = FirebaseProvider(http_client=http)
        with pytest.raises(AuthenticationError):
            await provider.complete(ChatRequest(model="gemini-3.8-flash", messages=[ChatMessage(role="user", content="hi")]), make_resource())
    async def test_403_authorization(self):
        http = FakeHttp()
        http.responses.append(http.exchange_ok())
        http.responses.append(FakeResponse(403, content=error_body("forbidden")))
        provider = FirebaseProvider(http_client=http)
        with pytest.raises(AuthorizationError):
            await provider.complete(ChatRequest(model="gemini-3.8-flash", messages=[ChatMessage(role="user", content="hi")]), make_resource())
    async def test_404_model_not_found(self):
        http = FakeHttp()
        http.responses.append(http.exchange_ok())
        http.responses.append(FakeResponse(404, content=error_body("not found")))
        provider = FirebaseProvider(http_client=http)
        with pytest.raises(ModelNotFoundError) as ei:
            await provider.complete(ChatRequest(model="gemini-3.8-flash", messages=[ChatMessage(role="user", content="hi")]), make_resource())
        assert not is_retryable(ei.value)
    async def test_429_rate_limit(self):
        http = FakeHttp()
        http.responses.append(http.exchange_ok())
        http.responses.append(FakeResponse(429, headers={"Retry-After": "10"}, content=error_body("quota")))
        provider = FirebaseProvider(http_client=http)
        with pytest.raises(RateLimitError) as ei:
            await provider.complete(ChatRequest(model="gemini-3.8-flash", messages=[ChatMessage(role="user", content="hi")]), make_resource())
        assert ei.value.retry_after == 10.0
    async def test_500_502_503_upstream_unavailable(self):
        for code in [500, 502, 503]:
            http = FakeHttp()
            http.responses.append(http.exchange_ok())
            http.responses.append(FakeResponse(code, content=error_body("down")))
            provider = FirebaseProvider(http_client=http)
            with pytest.raises(UpstreamUnavailableError):
                await provider.complete(ChatRequest(model="gemini-3.8-flash", messages=[ChatMessage(role="user", content="hi")]), make_resource())
    async def test_timeout(self):
        http = FakeHttp()
        http.responses.append(http.exchange_ok())
        http.raise_exc = TimeoutError("timed out")
        provider = FirebaseProvider(http_client=http)
        with pytest.raises(TimeoutError) as ei:
            await provider.complete(ChatRequest(model="gemini-3.8-flash", messages=[ChatMessage(role="user", content="hi")]), make_resource())
        assert isinstance(ei.value, FirebaseTimeoutError)
    async def test_connection_error(self):
        http = FakeHttp()
        http.responses.append(http.exchange_ok())
        http.raise_exc = ConnectionError("connection refused")
        provider = FirebaseProvider(http_client=http)
        with pytest.raises(NetworkError) as ei:
            await provider.complete(ChatRequest(model="gemini-3.8-flash", messages=[ChatMessage(role="user", content="hi")]), make_resource())
        assert isinstance(ei.value, FirebaseNetworkError)
    async def test_retryable_classification(self):
        for code in [429, 500, 502, 503]:
            err = classify_http_error(code, error_body("x"))
            assert is_retryable(err), str(code) + " should be retryable"
        for code in [400, 401, 403, 404]:
            err = classify_http_error(code, error_body("x"))
            assert not is_retryable(err), str(code) + " should NOT be retryable"

class TestRetryAfter:
    async def test_extracted_from_429(self):
        http = FakeHttp()
        http.responses.append(http.exchange_ok())
        http.responses.append(FakeResponse(429, headers={"Retry-After": "30"}, content=error_body("quota")))
        provider = FirebaseProvider(http_client=http)
        with pytest.raises(RateLimitError) as ei:
            await provider.complete(ChatRequest(model="gemini-3.8-flash", messages=[ChatMessage(role="user", content="hi")]), make_resource())
        assert ei.value.retry_after == 30.0

class TestSchedulerCooldown:
    async def test_429_cooldowns_resource(self, fake_clock):
        resource = make_resource()
        http = FakeHttp()
        http.responses.append(http.exchange_ok())
        http.responses.append(FakeResponse(429, headers={"Retry-After": "5"}, content=error_body("quota exceeded")))
        provider = FirebaseProvider(http_client=http)
        cooldown = CooldownManager(now_fn=lambda: fake_clock.now)
        pool = InMemoryPool(provider="firebase", resources=[resource], cooldown=cooldown)
        scheduler = Scheduler(providers={"firebase": provider}, pools={"firebase": pool}, max_retries=1)
        with pytest.raises(RateLimitError):
            await scheduler.chat_completion(ChatRequest(model="gemini-3.8-flash", messages=[ChatMessage(role="user", content="hi")]))
        assert resource.health == HealthState.COOLDOWN
        delta = resource.cooldown_until - fake_clock.now
        assert 5.0 <= delta.total_seconds() <= 5.5
    async def test_404_does_not_cooldown(self, fake_clock):
        resource = make_resource()
        http = FakeHttp()
        http.responses.append(http.exchange_ok())
        http.responses.append(FakeResponse(404, content=error_body("model not found")))
        provider = FirebaseProvider(http_client=http)
        cooldown = CooldownManager(now_fn=lambda: fake_clock.now)
        pool = InMemoryPool(provider="firebase", resources=[resource], cooldown=cooldown)
        scheduler = Scheduler(providers={"firebase": provider}, pools={"firebase": pool}, max_retries=1)
        with pytest.raises(ModelNotFoundError):
            await scheduler.chat_completion(ChatRequest(model="gemini-3.8-flash", messages=[ChatMessage(role="user", content="hi")]))
        assert resource.health != HealthState.COOLDOWN

class TestGatewayErrors:
    def _make_app(self, fake_http):
        from providers.firebase.factory import FirebaseProviderFactory as FPF
        original = FPF.create_provider
        FPF.create_provider = lambda self, pid, cfg=None: FirebaseProvider(http_client=fake_http, models=(cfg.get("models") if cfg else None))
        try:
            app = create_app(firebase_config())
        finally:
            FPF.create_provider = original
        return app
    async def test_400_returns_400(self):
        http = FakeHttp()
        http.responses.append(http.exchange_ok())
        http.responses.append(FakeResponse(400, content=error_body("bad request")))
        client = TestClient(self._make_app(http))
        resp = client.post("/v1/chat/completions", json={"model":"gemini-3.8-flash","messages":[{"role":"user","content":"hi"}]})
        assert resp.status_code == 400
        assert "error" in resp.json()
    async def test_404_returns_404(self):
        http = FakeHttp()
        http.responses.append(http.exchange_ok())
        http.responses.append(FakeResponse(404, content=error_body("not found")))
        client = TestClient(self._make_app(http))
        resp = client.post("/v1/chat/completions", json={"model":"gemini-3.8-flash","messages":[{"role":"user","content":"hi"}]})
        assert resp.status_code == 404
    async def test_429_returns_429(self):
        http = FakeHttp()
        http.responses.append(http.exchange_ok())
        http.responses.append(FakeResponse(429, content=error_body("quota")))
        client = TestClient(self._make_app(http))
        resp = client.post("/v1/chat/completions", json={"model":"gemini-3.8-flash","messages":[{"role":"user","content":"hi"}]})
        assert resp.status_code == 429
    async def test_500_returns_503(self):
        http = FakeHttp()
        http.responses.append(http.exchange_ok())
        http.responses.append(FakeResponse(500, content=error_body("server error")))
        client = TestClient(self._make_app(http))
        resp = client.post("/v1/chat/completions", json={"model":"gemini-3.8-flash","messages":[{"role":"user","content":"hi"}]})
        assert resp.status_code == 503
    async def test_unknown_model_returns_404(self):
        http = FakeHttp()
        client = TestClient(self._make_app(http))
        resp = client.post("/v1/chat/completions", json={"model":"no-such-model","messages":[{"role":"user","content":"hi"}]})
        assert resp.status_code == 404

@pytest.fixture
def fake_http():
    return FakeHttp()
@pytest.fixture
def fake_clock():
    return FakeClock()
