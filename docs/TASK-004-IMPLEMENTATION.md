# TASK-004-IMPLEMENTATION.md - Firebase AI Logic Provider

> Provider: Gateway-native FirebaseProvider -> firebasevertexai.googleapis.com/v1beta
> Status: COMPLETE - 241 tests passing (62 Firebase-specific)
> Real credentials used: NO

## 1. Modified files

| File | Change |
|------|--------|
| core/models.py | Additive: ChatRequest 5 new optional fields + ChatMessage.content relaxed + ChatResponse.tool_calls |
| protocol/openai.py | parse_openai_chat_request passes through new fields; to_openai_chat_response serialises tool_calls |
| app/bootstrap.py | Registers firebase ProviderDefinition |
| config.yaml.example | Adds firebase section with env placeholders |

## 2. New files

| File | Purpose |
|------|---------|
| providers/firebase/provider.py | FirebaseProvider(Provider) orchestrates Auth/Client/Payload/Response |
| providers/firebase/factory.py | FirebaseProviderFactory / FirebaseResourceFactory |
| tests/providers/_firebase_fakes.py | FakeHttp, FakeResponse, make_sse, make_resource |
| tests/providers/test_firebase.py | Integration: complete, stream, 401, health, factory, cooldown |
| tests/providers/test_firebase_auth.py | JWT lifecycle: fetch, cache, refresh, invalidate, errors |
| tests/providers/test_firebase_payload.py | OpenAI->Gemini payload mapping |
| tests/providers/test_firebase_response.py | Gemini->ChatResponse/ChatChunk |
| tests/providers/test_firebase_streaming.py | Firebase SSE parsing |
| tests/providers/test_firebase_errors.py | Error classification and retry-after |

Pre-existing stubs completed: auth.py, client.py, errors.py, payload.py, resource.py, response.py, streaming.py

## 3. Provider Architecture

FirebaseProvider
  FirebaseResource        one Firebase Project per resource
  FirebaseAuth            Debug Token -> App Check JWT (cached, 401 invalidate)
  FirebaseClient          generateContent / streamGenerateContent?alt=sse
  FirebasePayloadBuilder  OpenAI ChatRequest -> Gemini generateContent JSON
  FirebaseResponseParser  Gemini JSON -> ChatResponse / ChatChunk
  FirebaseErrorMapper     HTTP status -> core ProviderError (scope=resource on 429)

## 4. Auth

- exchangeDebugToken: debug_token -> JWT (ttl ~3600s)
- JWT cached; refreshed 300s before expiry
- 401 -> invalidate -> next request forces re-exchange
- JWT never logged

## 5. Request mapping

| OpenAI | Gemini |
|--------|--------|
| messages[role=system] | systemInstruction |
| messages[role=user] | contents[].role=user + parts |
| messages[role=assistant]+tool_calls | contents[].role=model + functionCall |
| messages[role=tool] | contents[].role=user + functionResponse |
| tools + tool_choice | tools + toolConfig.functionCallingConfig |
| max_tokens/top_p/stop | generationConfig |
| reasoning_effort | thinkingConfig.thinkingLevel |
| multimodal (image/audio data URL) | inline_data parts |

## 6. Response mapping

| Gemini | OpenAI |
|--------|--------|
| parts[].text | message.content |
| parts[].functionCall | message.tool_calls |
| finishReason | finish_reason |
| usageMetadata | usage |
| thought/thoughtSignature | stripped (not exposed) |

## 7. Streaming

- Firebase upstream: SSE wire format (data: {...} + [DONE]), NOT NDJSON
- iter_sse_events reads aiter_bytes() -> line-split -> JSON parse
- Gateway output layer (format_sse/DONE_SSE) reused as-is

## 8. Error mapping

| HTTP | Gateway | retryable |
|------|---------|-----------|
| 400 | InvalidRequestError | no |
| 401 | AuthenticationError | no |
| 403 | AuthorizationError | no |
| 404 | ModelNotFoundError | no |
| 429 | RateLimitError (scope=resource) | yes |
| 5xx | UpstreamUnavailableError | yes |
| timeout | TimeoutError | yes |
| connection | NetworkError | yes |

## 9. Retry / cooldown

- 429: Retry-After honoured; otherwise exponential backoff (core CooldownManager)
- 401: One forced JWT refresh inside FirebaseClient
- 5xx/timeout: cooldown with degrade after 3 consecutive failures
- No own retry in provider; fully delegated to Gateway Scheduler

## 10. Model configuration

- Config snapshot; default: [gemini-3.8-flash, gemini-3.7-flash]
- No upstream list API; 404 = model unavailable
- firebase2api model table NOT copied

## 11. Test results

- 62 Firebase tests: PASSED
- 241 Total suite: PASSED (no regressions)

## 12. Real credentials: NONE

## 13. Known limitations

- safetySettings: not injected (Firebase defaults apply)
- 429 sub-classification: unified RateLimitError until real samples
- response_format / JSON mode: not implemented
- Anti-trunc: excluded
- Developer messages: dropped

## 14. Next suggestions

- Vertex provider: share Gemini payload/response modules
- Safety settings config: per-resource when production blocks encountered
- Live integration test with real Firebase project
- 429 fixture: capture real 429 JSON for finer quota-mapping

