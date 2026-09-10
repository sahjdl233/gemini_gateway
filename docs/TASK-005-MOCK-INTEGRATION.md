# TASK-005: Firebase Mock Integration Report

## 1. Test Objective

Verify FirebaseProvider from HTTP layer to Gateway OpenAI API layer
using Mock HTTP Server, without any real Firebase access.

```text
Mock Firebase
      |
FirebaseProvider
      |
Scheduler / ResourcePool
      |
Gateway
      |
OpenAI-compatible API
```

## 2. Mock Firebase Design

Uses `FakeHttp` from `tests/providers/_firebase_fakes.py` (TASK-004).
- `post()` returns responses from FIFO queue (auth exchange + AI calls)
- `stream()` returns responses from FIFO queue (SSE streaming)
- No new dependencies introduced

### Mock Endpoints
```text
POST https://firebaseappcheck.googleapis.com/v1/projects/{project}/apps/{app}:exchangeDebugToken
POST https://firebasevertexai.googleapis.com/v1beta/projects/{project}/models/{model}:generateContent
POST https://firebasevertexai.googleapis.com/v1beta/projects/{project}/models/{model}:streamGenerateContent?alt=sse
```

## 3. App Check Flow

| Test | Result |
|------|--------|
| First request: no cache -> exchange -> cache JWT | PASS |
| Second request: cached JWT, no re-exchange | PASS |
| Near-expiry (<300s): automatic refresh | PASS |
| 401 from AI: force refresh + retry once | PASS |
| 401 twice: no infinite loop, error surfaced | PASS |

## 4. GenerateContent Flow

| Test | Result |
|------|--------|
| Gemini 200 -> OpenAI ChatResponse | PASS |
| choices[0].message.content verified | PASS |
| choices[0].finish_reason = "stop" | PASS |
| usage.prompt_tokens / completion_tokens / total_tokens | PASS |

## 5. Streaming Flow

| Test | Result |
|------|--------|
| Firebase SSE -> iter_sse_events -> JSON events | PASS |
| Multiple chunks + [DONE] termination | PASS |
| Split across network chunks | PASS |
| SSE -> FirebaseProvider.stream -> ChatChunk | PASS |
| Stream URL contains ?alt=sse | PASS |
| 401 retry with fresh JWT in stream | PASS |
| No duplicate text in stream chunks | PASS |
| Firebase SSE does NOT use NDJSON parser | PASS |
| Gateway SSE output: data: {JSON} format | PASS |
| Final line: data: [DONE] | PASS |
| Each SSE chunk carries only delta text | PASS |

## 6. OpenAI Gateway

| Test | Result |
|------|--------|
| POST /v1/chat/completions (stream=false) -> 200 | PASS |
| POST /v1/chat/completions (stream=true) -> SSE | PASS |
| GET /v1/models includes firebase models | PASS |

## 7. Multi-turn Dialogue

| Test | Result |
|------|--------|
| system -> systemInstruction (NOT contents) | PASS |
| user -> user content | PASS |
| assistant -> model content | PASS |
| Order preserved across 5+ messages | PASS |

## 8. Tools / Function Calling

| Test | Result |
|------|--------|
| OpenAI tools -> Gemini functionDeclarations | PASS |
| Gemini functionCall -> OpenAI tool_calls | PASS |
| OpenAI tool message -> Gemini functionResponse | PASS |
| tool_choice="required" -> mode=ANY | PASS |
| tool_choice="none" -> mode=NONE | PASS |
| tool_choice=AUTO (default) | PASS |

## 9. Multimodal

| Test | Result |
|------|--------|
| data:image/png;base64 -> inline_data | PASS |
| data:audio/mpeg;base64 -> inline_data | PASS |
| Multiple images in one message | PASS |

## 10. Error Matrix

| Firebase Status | Expected | Result |
|-----------------|----------|--------|
| 200 | Normal response | PASS |
| 400 | InvalidRequestError | PASS |
| 401 | refresh + retry once | PASS |
| 403 | AuthorizationError | PASS |
| 404 | ModelNotFoundError (no retry) | PASS |
| 429 | RateLimitError + Retry-After | PASS |
| 500 | UpstreamUnavailableError (retryable) | PASS |
| 502 | UpstreamUnavailableError (retryable) | PASS |
| 503 | UpstreamUnavailableError (retryable) | PASS |
| timeout | FirebaseTimeoutError | PASS |
| connection error | FirebaseNetworkError | PASS |

### Gateway Error Responses
| Status | Gateway HTTP Response | Result |
|--------|----------------------|--------|
| 400 | 400 Bad Request | PASS |
| 404 | 404 Not Found | PASS |
| 429 | 429 Too Many Requests | PASS |
| 500 | 503 Service Unavailable | PASS |
| unknown model | 404 Not Found | PASS |

## 11. ResourcePool

| Test | Result |
|------|--------|
| Resource A on cooldown -> Resource B selected | PASS |
| Two resources both usable (round-robin) | PASS |
| 429 -> cooldown -> failover verified | PASS |

## 12. Model Discovery

| Test | Result |
|------|--------|
| ModelRegistry includes firebase models | PASS |
| Default models (gemini-3.8-flash, gemini-3.7-flash) | PASS |

## 13. Credential Redaction

| Test | Result |
|------|--------|
| 400 error message: no api_key/debug_token leak | PASS |
| 429 error message: no credential leak | PASS |
| Network error: no credential leak | PASS |
| Auth exchange error: no debug_token leak | PASS |

## 14. pytest Results

```text
tests/integration/test_firebase_gateway.py       24 passed
tests/integration/test_firebase_mock_streaming.py 10 passed
tests/integration/test_firebase_mock_errors.py   18 passed
tests/providers/ (all existing)                 241 passed
---
TOTAL: 293 passed, 0 failed
```

## 15. Current Unverified Items

- Real Firebase App Check token exchange (requires real project)
- Real Firebase AI Logic generateContent (requires real project)
- Real Firebase SSE streaming (requires real project)
- Production credential rotation under load
- Real-world rate limiting behavior

## 16. Future Real Firebase Verification

When real Firebase credentials are available:
```text
FIREBASE_INTEGRATION_TEST=1 pytest -q
```

The current mock integration confirms that FirebaseProvider
has sufficient code-level behavior to enter provider lockdown.

## 17. Completion Status

- [x] Mock App Check: first fetch, cache, refresh, 401 retry
- [x] GenerateContent -> OpenAI ChatResponse
- [x] Streaming SSE -> OpenAI SSE
- [x] Gateway /v1/chat/completions (stream + non-stream)
- [x] Multi-turn dialogue
- [x] Tools / Function Calling
- [x] Multimodal inline data
- [x] Error matrix (all status codes)
- [x] ResourcePool failover
- [x] Model Registry integration
- [x] Credential redaction
- [x] No real network access (default pytest)
- [x] No TASK-004 regression (241 existing tests preserved)
- [x] 0 failed
