# Anonymous Vertex Protocol Archaeology (TASK-002A)

Based entirely on the reference source in vertex-singbox (internal/engine/vertex and internal/engine/transform). No external articles, no guessing. Where the source is unambiguous, this document states the fact; where it is not, it says BLOCKED.

Security note: no real credentials, cookies, tokens, or API keys are recorded anywhere in this repository. Any sensitive literal found in the source is noted only as <REDACTED>.

---

## 3.1 Request entry point

From client.go and payload.go:

Method: POST
Base URL: https://cloudconsole-pa.clients6.google.com
Path: /v3/entityServices/AiplatformEntityService/schemas/AIPLATFORM_GRAPHQL:batchGraphql
Query parameters: ?key=<apiKey>&prettyPrint=false
  - When cfg.VertexAPIKey() is empty, the client falls back to a hardcoded anonymous API key. That key is a real credential and is NOT reproduced here (recorded as <REDACTED>).

https://cloudconsole-pa.clients6.google.com/v3/entityServices/AiplatformEntityService/schemas/AIPLATFORM_GRAPHQL:batchGraphql?key=<REDACTED>&prettyPrint=false

Dynamic URL construction: getBatchGraphqlURL() reads the configured API key at request time (SIGHUP hot-reload), falls back to the anonymous key, then appends ?key=...&prettyPrint=false.

---

## 3.2 Request headers

From transport/headers.go XHRHeaders(), used for the batchGraphql POST (contentType application/json, accept */*, origin/referer console URLs, sec-fetch-site cross-site). This is a Chrome 150 fingerprint.

### Must headers (browser-fingerprint TLS + UA set)
| Header | Value |
|--------|-------|
| user-agent | Chrome 150 UA (Windows NT 10.0, Win64, x64) |
| sec-ch-ua | "Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150" |
| sec-ch-ua-mobile | ?0 |
| sec-ch-ua-platform | "Windows" |
| sec-ch-ua-arch / -bitness / -full-version / -full-version-list / -platform-version / -model / -wow64 / -form-factors | fixed Chrome 150 values |
| accept | */* (batchGraphql) |
| origin | https://console.cloud.google.com |
| referer | https://console.cloud.google.com/ |
| sec-fetch-site | cross-site |
| sec-fetch-mode / -dest | cors / empty |
| accept-encoding | gzip, deflate, br |
| accept-language | zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6 |
| priority | u=1, i |
| content-type | application/json |

### Dynamic headers
| Header | Derivation |
|--------|-----------|
| x-browser-validation | base64(sha1(<REDACTED-key> + userAgent)) -- GenerateXBrowserValidation() |

### Fixed Google console headers
| Header | Value |
|--------|-------|
| x-goog-authuser | 0 |
| x-browser-channel | stable |
| x-browser-copyright | Copyright 2026 Google LLC. All Rights Reserved. |
| x-browser-year | 2026 |
| x-goog-ext-353267353-jspb | [null,null,null,194274] |

Note: header order is significant (an HeaderOrderKey list is kept in the Go code). The TLS ClientHello fingerprint is part of the anti-bot check; a plain httpx client will not reproduce it. This is a transport-level concern and is documented as a limitation for the Python provider.

---

## 4. querySignature

From payload.go:

const querySignature = "2/l8eCsMMY49imcDQ/lwwXyL8cYtTjxZBF2dNqy69LodY="
const operationName  = "StreamGenerateContentAnonymous"

Conclusion (source-based):

1. Input: none. It is a hardcoded base64 constant, not derived from any request.
2. Related to request parameters? No.
3. Related to time? No.
4. Related to URL? No.
5. Fixed constant? Yes -- a literal base64 string.
6. Hash / encode / crypto? No runtime crypto. It is stored as a literal. (The signature.go module computes thoughtSignature for history parts, which is a *different* field -- see below -- and is *not* the querySignature.)
7. Final location: BatchGraphQLPayload.querySignature, serialized as a top-level JSON field of the POST body.

So querySignature is NOT a Google authentication signature. It is a FIXED frontend protocol constant -- a static value the console sends on this private GraphQL operation. It does not vary by request, time, or URL.

### Separate: thoughtSignature (history parts)

signature.go handles a *different* field, thoughtSignature, inside the contents[].parts[] of the request body. The anonymous endpoint enforces that model turns carrying functionCall / thought parts have a non-empty base64 thoughtSignature. The gateway injects a sentinel:

base64("skip_thought_signature_validator") = "c2tpcF90aG91Z2h0X3NpZ25hdHVyZV92YWxpZGF0b3I="

This is a PSEUDO-signature sentinel, not a real signature. It is only relevant for multi-turn history containing model tool-call/thought parts. A plain single user-model text conversation does not require it.

---

## 5. Request body analysis

The POST body is a GraphQL envelope (BatchGraphQLPayload):

{
  "requestContext": { ... },
  "querySignature": "2/l8eCsMMY49imcDQ/lwwXyL8cYtTjxZBF2dNqy69LodY=",
  "operationName": "StreamGenerateContentAnonymous",
  "variables": { ... }
}

### requestContext (fixed-ish, some random)
{
  "clientVersion": "boq_cloud-boq-clientweb-vertexaistudio_20260630.00_p0",
  "pagePath": "/agent-platform/studio/multimodal",
  "pageViewId": <random 16-digit int>,
  "trackingId": "d<16 random digits>",
  "backendOverrides": {},
  "clientSessionId": "<random UUID v4>",
  "selectedPurview": {},
  "jurisdiction": "global",
  "localizationData": { "locale": "zh_CN", "timezone": "Asia/Hong_Kong" }
}

### variables
GeminiVariables embeds a GeminiRequest plus:
- model -- the resolved Gemini model id (e.g. gemini-2.5-flash)
- region -- global
- recaptchaToken -- a fresh reCAPTCHA Enterprise token (see section 4.5 / recaptcha)
- all GeminiRequest fields flattened at top level:

{
  "contents": [
    { "role": "user|model", "parts": [ { "text": "..." } ] }
  ],
  "systemInstruction": { "role": "system", "parts": [ { "text": "..." } ] },
  "safetySettings": [ { "category": "HARM_CATEGORY_*", "threshold": "BLOCK_NONE" } ... ],
  "generationConfig": { "temperature": ..., "maxOutputTokens": ..., "topP": ... },
  "tools": [ ... ],
  "toolConfig": { ... }
}

### Gateway to Anonymous Vertex mapping
| Gateway (ChatRequest) | Anonymous Vertex (variables) | Notes |
|-----------------------|------------------------------|-------|
| model | variables.model | resolved id |
| (n/a) | variables.region | fixed global |
| (n/a) | variables.recaptchaToken | fetched per request |
| messages (role=system) | systemInstruction | first system message |
| messages (role=user) | contents[] role=user | merged contiguous same-role |
| messages (role=assistant) | contents[] role=model | merged contiguous same-role |
| temperature | generationConfig.temperature | clamped per model spec |
| max_tokens | generationConfig.maxOutputTokens | default/clamped per model spec |
| (n/a) | generationConfig.topP | default injected per spec |
| tools | tools[].functionDeclarations | native-schema conversion |
| (n/a) | safetySettings | fixed 4xOFF (BLOCK_NONE) |
| (n/a) | thoughtSignature on model parts | sentinel injected for history |

---

## 6. Response analysis (non-stream)

The anonymous endpoint wraps payloads. Non-streaming completion is actually implemented by collecting streaming chunks (complete.go runSingleCandidate executeStreamingAttempt), so there is no separate non-stream JSON shape used in this codebase; both paths read the streaming frame format. The final response is assembled as a GeminiResponse:

{
  "candidates": [
    { "index": 0, "content": { "role": "model", "parts": [ { "text": "..." } ] },
      "finishReason": "STOP", "safetyRatings": [...], "tokenCount": ... }
  ],
  "usageMetadata": { "promptTokenCount": ..., "candidatesTokenCount": ..., "totalTokenCount": ... },
  "modelVersion": "...",
  "responseId": "..."
}

### Conversion
GeminiResponse to Internal ChatResponse
  candidates[0].content.parts[*].text concatenated to ChatResponse.text
  candidates[0].finishReason to ChatResponse.finish_reason (map to stop)
  usageMetadata to ChatResponse.usage
FINISH_REASON_UNSPECIFIED is treated as not finished and dropped.

---

## 7. Streaming analysis

The upstream transport is raw NDJSON over HTTP chunked transfer, NOT SSE.

stream_scanner.go reads the HTTP body byte-by-byte, counting brace depth across network chunks, extracting one complete JSON object per frame. There are no event: or data: SSE lines -- the raw response is a stream of concatenated JSON objects (one per newline-less chunk, but JSON-boundary delimited).

### Frame envelope
Each JSON object (processStreamingObject) has this shape:

{
  "results": [
    {
      "errors": [ ... ],
      "data": {
        "ui": {
          "streamGenerateContentAnonymous": { <chunk> }
        }
      }
    }
  ]
}

where <chunk> is a GeminiChunk (same shape as a Gemini response: candidates, usageMetadata, modelVersion, ...). ui.streamGenerateContentAnonymous may be an object or an array of items (array form carries per-item chunks plus outer meta).

### Critical finishReason red line
Each incremental frame carries finishReason: "FINISH_REASON_UNSPECIFIED" by default. The stream must NOT terminate on UNSPECIFIED. Only a non-empty finishReason != "FINISH_REASON_UNSPECIFIED" (e.g. STOP, MAX_TOKENS, SAFETY, ...) signals the end.

### Boundary / fields
- event boundary: JSON object braces (parsed by depth counting)
- data field: results[].data.ui.streamGenerateContentAnonymous
- text delta: candidates[0].content.parts[*].text
- finish event: candidate finishReason != FINISH_REASON_UNSPECIFIED
- error event: results[].errors[] (e.g. "Failed to verify action")
- heartbeat: none observed
- connection close: clean EOF after a real finishReason, or after metadata only

---

## 8. Error mapping

From errors.go and stream_chat.go. Upstream HTTP/gRPC status to VertexError:

| Upstream | VertexError Kind | HTTP code |
|----------|------------------|-----------|
| 401 / UNAUTHENTICATED / "Failed to verify action" / "The caller does not have permission" | auth | 502 |
| 429 / RESOURCE_EXHAUSTED | ratelimit | 429 |
| 403 / PERMISSION_DENIED | permission | 403 |
| 400 / INVALID_ARGUMENT | invalid | 400 |
| 404 / NOT_FOUND | notfound | 404 |
| 503 / UNAVAILABLE | unavailable | 503 |
| 5xx / network / empty response | network/internal/server | 502/500 |

### Gateway error mapping (Provider layer)
| VertexError | Gateway ProviderError |
|-------------|----------------------|
| ratelimit (429) | RateLimitError (carry Retry-After) |
| auth (502) | AuthenticationError |
| permission (403) | AuthorizationError |
| invalid (400) | InvalidRequestError |
| notfound (404) | ModelNotFoundError |
| network / 502 | UpstreamUnavailableError / NetworkError |
| server / 503 | UpstreamUnavailableError |

---

## 9. ReCAPTCHA token flow (needed for every request)

recaptcha.go: the batchGraphql request requires variables.recaptchaToken, a reCAPTCHA Enterprise token fetched fresh per request:

1. GET https://www.google.com/recaptcha/enterprise/anchor?...&k=<siteKey>&co=<base64-origin>&hl=zh-CN&v=<release>... to parse recaptcha-token hidden input (base token).
2. POST https://www.google.com/recaptcha/enterprise/reload?k=<siteKey> with form fields (v, reason=q, k, c (base token), co, hl, ...) to parse rresp","<token> -- final reCAPTCHA token.
3. Put the token into variables.recaptchaToken.

<siteKey> and the origin co value are real Google constants (not user credentials). <REDACTED> in this doc refers to user-owned credentials; the recaptcha siteKey/co are public Google values tied to the console page.

BLOCKED (transport): The Go implementation relies on a TLS ClientHello fingerprint (tls-client) and exact header ordering that a stock httpx client cannot reproduce. The reference project anti-bot value is a TLS-fingerprint + recaptcha combination. Without a TLS-fingerprint-capable transport, the Python gateway may be rejected upstream. This is a documented risk; the provider is built with a swappable HTTP client so a TLS-fingerprint transport can be dropped in later. The provider own logic (signature, body, parsing, mapping) is fully reproducible from source.

---

## 10. Supported models

config/models.json (text-family subset relevant to this task):

gemini-2.5-flash, gemini-2.5-flash-lite, gemini-2.5-pro, gemini-3-flash-preview, gemini-3.1-flash-lite, gemini-3.1-pro-preview, gemini-3.5-flash, gemini-3.5-flash-lite, gemini-3.6-flash, gemini-3.7-flash

(Image/TTS models exist in the source but are out of scope for this text task.)

---

## 11. Blocked items (from the stop-condition)

- Transport fingerprint: TLS ClientHello + header ordering cannot be reproduced by plain httpx. This is a real constraint, not a guess. See section 9.
---

## 12. Protocol Layer Code Layout (TASK-002-A)

The provider is decomposed into a layered protocol stack so the Provider
never assembles Google GraphQL dictionaries itself:

```text
AnonymousVertexProvider (provider.py: lifecycle / request orchestration)
        |
        v
AnonymousVertexClient  (client.py: URL / headers / POST / error mapping)
        |
        v
AnonymousVertexProtocol (protocol.py: GraphQL envelope + requestContext)
        |
        v
HTTP Transport         (transport.py: minimal async POST contract)
```

Module responsibilities:

| Module | Responsibility |
|--------|----------------|
| provider.py | Provider lifecycle, resource wiring, ChatRequest orchestration. Never builds Google payloads. |
| client.py | Owns the wire: endpoint URL, XHR headers, envelope serialization, POST, upstream error classification. |
| protocol.py | ONLY module that knows the batchGraphql envelope (requestContext / querySignature / operationName / variables). |
| models.py | Internal AnonymousVertexRequest model + GraphQLPayload dataclasses. |
| request.py | OpenAI/ChatRequest -> AnonymousVertexRequest conversion (contents, tools, gen config). |
| response.py | GeminiChunk/candidates -> ChatResponse / ChatChunk conversion. |
| headers.py | Chrome 150 fingerprint XHR headers (documented source/usage). |
| streaming.py | Brace-counting JSON object scanner + frame extraction (NDJSON, not SSE). |
| signature.py | Single source of protocol constants (endpoint, querySignature, operationName, thoughtSignature sentinel). |
| errors.py | Protocol error hierarchy (AnonymousVertexProtocolError subclasses + core mapping). |
| recaptcha.py | RecaptchaTokenProvider abstraction + real anchor/reload flow + FakeRecaptchaTokenProvider. |
| transport.py | HTTPTransport protocol + HttpxTransport adapter. |

### querySignature — final answer

- Fixed constant? **YES** — a hardcoded base64 literal from payload.go.
- Dynamic computation? **NO** — never recomputed per request.
- Source? vertex-singbox internal/engine/vertex/payload.go: `const querySignature = "2/l8eCsMMY49imcDQ/lwwXyL8cYtTjxZBF2dNqy69LodY="`.

The name is misleading (it is not an authentication signature); it is a fixed
frontend protocol constant on the private GraphQL operation. Defined once in
signature.py as `QUERY_SIGNATURE`.

### Streaming — final answer

- Google raw stream format: raw NDJSON over HTTP chunked transfer; concatenated
  JSON objects (one per frame), NOT SSE (no `event:`/`data:` lines).
- Parser: `StreamingObjectScanner` increments brace depth across network chunks;
  it yields a complete JSON object only when braces balance at depth 0. A single
  JSON object may span multiple HTTP chunks; the parser never assumes
  `HTTP chunk == JSON object`.
