# TASK-006 Research: Gemini CLI Provider Feasibility

> Research date: 2026-09-10
> Status: Complete

---

# 1. Executive Summary

**Decision: IMPLEMENT WITH CONDITIONS**

Gemini CLI (and its open-source reverse proxy gcli2api) does NOT use a secret CLI-only protocol. Under the hood it calls the **standard Gemini Generative AI API** (generativelanguage.googleapis.com) with **OAuth 2.0 Bearer tokens** instead of API keys. The HTTP protocol, request/response schema, and streaming format are identical to the standard Gemini API -- only the authentication layer differs.

This makes it a viable third Gateway provider. However, the OAuth 2.0 credential lifecycle (initial browser login, token refresh, multi-account rotation) introduces meaningful complexity.

Recommended path:
1. Phase 1 (TASK-007): Native GeminiCliProvider with pre-provisioned OAuth refresh tokens, handles token refresh via httpx (no browser at runtime).
2. Phase 2 (optional): Companion credential-provisioning script for one-time browser-based OAuth flow to produce credential files.
3. NOT recommended: Running gcli2api as a sidecar -- adds external process, SQLite/MongoDB, and a web panel for no protocol advantage.

---

# 2. Gemini CLI Architecture

## What it actually is

Gemini CLI is a terminal-based AI coding assistant that communicates with Google generativelanguage.googleapis.com backend.

**Key insight:** Gemini CLI does NOT have its own backend. It is a client that talks to the same API as curl or the Python SDK -- just with OAuth credentials instead of API keys.

## gcli2api (su-kaka/gcli2api)

- 5.2k stars, Python 3.12+, CNC-1.0 license
- 1,359 commits, 45 contributors, actively maintained (last commit: Sep 9 2026)
- FastAPI service exposing OpenAI/Gemini/Claude compatible endpoints
- Source structure:
  - src/api/ -- FastAPI endpoint definitions
  - src/converter/ -- Format conversion (OpenAI/Gemini, Claude/Gemini)
  - src/router/ -- Request routing (GCLI vs Antigravity mode)
  - src/auth.py -- Google OAuth 2.0 flow
  - src/credential_manager.py -- Multi-account credential rotation
  - src/google_oauth_api.py -- Google OAuth token exchange
  - src/httpx_client.py -- HTTP client with proxy support
  - src/storage/ -- SQLite / MongoDB persistence

## Two modes in gcli2api

| Mode | Backend | Auth | Notes |
|------|---------|------|-------|
| GCLI | generativelanguage.googleapis.com | OAuth 2.0 | Standard Gemini API |
| Antigravity | antigravity.googleapis.com | OAuth 2.0 | Google Antigravity platform |

---

# 3. Authentication

## OAuth 2.0 Flow

Gemini CLI uses Google OAuth 2.0 for desktop app type:

1. **Initial login:** Browser-based OAuth consent screen
   - Client ID: Google-issued (hardcoded in CLI)
   - Scopes: generative-language API access
   - Redirect: localhost callback or copy-paste code

2. **Token response:**
   - access_token: ya29.a0... (lifetime: ~1 hour)
   - refresh_token: 1//0g... (long-lived, months/years)
   - token_type: Bearer
   - expires_in: 3600

3. **Token refresh:** Standard OAuth refresh (NO browser needed)
   POST https://oauth2.googleapis.com/token
   client_id=...&client_secret=...&refresh_token=...&grant_type=refresh_token

## Credential lifecycle

| Aspect | Detail |
|--------|--------|
| Access token lifetime | ~1 hour |
| Refresh token lifetime | Long-lived (months/years) |
| Refresh mechanism | POST to oauth2.googleapis.com/token |
| Browser for refresh | **No** -- pure HTTP |
| Multi-account | Yes -- credential rotation |
| Token storage | JSON files in creds/ directory |
| No-browser operation | **Yes** -- once credentials are provisioned |

---

# 4. HTTP Endpoint

Actual endpoint: POST/GET generativelanguage.googleapis.com/v1beta/models/...

Authentication header (THE ONLY difference from API key):
- OAuth: Authorization: Bearer ya29.a0ARrdaM...
- API key: x-goog-api-key: AIza...

---

# 5. Request Protocol

Standard Gemini GenerateContent schema. Mapping to Gateway ChatRequest:

| Gateway field | Gemini field |
|---------------|-------------|
| messages[role=user] | contents[{role:user}] |
| messages[role=assistant] | contents[{role:model}] |
| messages[role=system] | systemInstruction |
| temperature | generationConfig.temperature |
| max_tokens | generationConfig.maxOutputTokens |
| tools | tools[{functionDeclarations}] |
| tool_choice | toolConfig |
| reasoning_effort | generationConfig.thinkingConfig |

---

# 6. Streaming Protocol

SSE format (alt=sse):
  data: {candidates:[{content:{parts:[{text:Hello}],role:model},finishReason:STOP}],usageMetadata:{...}}
  data: [DONE]

---

# 7. Response Schema

Standard Gemini GenerateContent response with candidates, content.parts, finishReason, usageMetadata.

---

# 8. Model Discovery

GET /v1beta/models returns available models. Models are account-dependent.

---

# 9. Quota / Resource

| Quota dimension | Detail |
|-----------------|--------|
| Per-account | ~1000 requests/day (free tier) |
| Per-model | Rate limits vary by model |
| Token budget | 1M context window |
| Rate limit | ~2 RPM free, higher for paid |

---

# 10. Error Handling

| HTTP Status | Meaning | Gateway mapping |
|-------------|---------|-----------------|
| 400 | Bad request | BadRequestError |
| 401 | Token expired | AuthError (refresh token) |
| 403 | Quota/permission | RateLimitError or AuthError |
| 404 | Model not found | ModelNotFoundError |
| 429 | Rate limit | RateLimitError (Retry-After) |
| 500 | Server error | ProviderError |

---

# 11. Adapter Feasibility

## A. Native Provider (RECOMMENDED)

Gateway -> GeminiCliProvider -> httpx -> Google backend
Dependencies: httpx (already used), json (stdlib). No additional runtime.
Complexity: Medium | RAM: ~0 extra | 1H1G: YES

## B. CLI Subprocess -- REJECTED
Node.js runtime, 200-500MB RAM, unstable IPC. Not suitable for 1H1G.

## C. External Proxy (gcli2api) -- NOT RECOMMENDED
Extra process, extra port, SQLite/MongoDB, web panel. Gateway already does everything gcli2api does except OAuth.

| Option | Complexity | RAM | Stability | Maintenance | 1H1G |
|--------|------------|-----|-----------|-------------|------|
| Native Provider | Medium | ~0 | High | Low | YES |
| CLI Subprocess | High | 200-500MB | Low | High | NO |
| External Proxy | Low | 100-300MB | Medium | Medium | WARN |

---

# 12. Comparison with Existing Providers

| Aspect | Anonymous Vertex | Firebase AI | Gemini CLI (Proposed) |
|--------|-----------------|-------------|----------------------|
| Auth | reCAPTCHA + browser | App Check JWT | OAuth 2.0 Bearer |
| Endpoint | batchGraphql | generativelanguage | generativelanguage |
| Protocol | GraphQL | REST (Gemini native) | REST (Gemini native) |
| Streaming | NDJSON (brace-count) | SSE (alt=sse) | SSE (alt=sse) |
| Token refresh | N/A (per-request) | JWT auto-refresh | OAuth refresh token |
| Browser needed | No (reCAPTCHA auto) | No | Initial login only |
| Multi-account | No | Yes (Debug Tokens) | Yes (credential rotation) |
| Model list | Hardcoded (10) | Dynamic | Dynamic |
| Runtime | httpx | httpx | httpx |
| 1H1G | YES | YES | YES (with cred helper) |

---

# 13. 1H1G Assessment

| Option | RAM | CPU | Runtime | Storage | 1H1G |
|--------|-----|-----|---------|---------|------|
| Native Provider | ~0 | Negligible | Python/httpx | Token files | YES |
| CLI Subprocess | 200-500MB | High | Node.js | Large | NO |
| External Proxy | 100-300MB | Medium | Python + SQLite | DB files | WARN |

---

# 14. Risks

1. **OAuth initial login requires browser** -- Cannot be done headlessly on first setup. Mitigated by a one-time credential provisioning script.
2. **Token expiry** -- Access tokens expire every ~1 hour. Must implement automatic refresh.
3. **Refresh token revocation** -- Google can revoke refresh tokens at any time. Need graceful error handling and cooldown.
4. **Rate limits** -- Free-tier Gemini accounts have strict RPM limits. Multi-account rotation is essential.
5. **ToS compliance** -- Using OAuth tokens for automated API calls may violate Google ToS. This is an existing risk shared with Anonymous Vertex and Firebase providers.

---

# 15. Final Decision

IMPLEMENT WITH CONDITIONS

**Conditions:**
1. OAuth initial login is handled by a separate provisioning script
2. Gateway only handles token refresh (no browser dependency at runtime)
3. Multi-account rotation is supported via ResourcePool
4. Gemini format conversion is shared with Firebase provider (DRY)
5. Rate limiting follows same cooldown pattern as Anonymous Vertex

**Next task:** TASK-007 -- GeminiCliProvider Implementation
