# Gemini Gateway

Current Phase:
Post-WEBUI-002 / Post-AUTH-015 maintenance state

## Current Architecture

- **FastAPI** application (`app/main.py`); routes split under `app/routes/`
  (`models`, `chat`, `admin`).
- **Provider / Resource / Scheduler** abstraction (`core/provider.py`,
  `core/resource.py`, `core/scheduler.py`) with a unified error hierarchy
  (`core/errors.py`).
- **ResourcePool** (`core/pool.py`): health/cooldown-aware, round-robin,
  in-flight tracking.
- **ModelRegistry** (`core/model_registry.py`): model index with TTL refresh.
- **ProviderRegistry / ResourceFactory** (`core/provider_registry.py`,
  `core/resource_factory.py`): builtin provider registration and resource
  construction are decoupled from `app/main.py`; `app/bootstrap.py`
  (`register_builtin_providers()`) is the single place a builtin provider is
  added.
- **Transport / proxy seam** (`transport/`): HTTP, proxy and streaming live
  outside provider implementations, so providers stay unaware of proxies.
- **ExecutionBackend seam** (`execution/base.py`, `execution/http.py`): only
  `providers/antigravity/` is currently on this seam; the other providers
  still use their legacy adapter path.
- **OpenAI-compatible API**: `GET /v1/models` and `POST /v1/chat/completions`,
  both streaming (SSE) and non-streaming.

## Completed

- Core architecture: models, resources, pools, scheduler, errors, health, cooldown.
- Provider registry / factory separation: registration and resource creation no
  longer live in `app/main.py`.
- Cooldown: `Retry-After` support, exponential backoff + jitter, 429 as a
  first-class `RateLimitError` (scope + retry_after).
- Retry / fallback in the scheduler, with health-aware selection and round-robin.
- OpenAI compatibility: `/v1/models`, `/v1/chat/completions` (stream + non-stream).
- `FakeProvider` with success / 429 / auth_error / timeout / stream scenarios.
- `Anonymous Vertex` provider (unofficial upstream, see Providers).
- `Firebase` (Firebase AI Logic) provider.
- `Gemini CLI` provider.
- `Antigravity` provider.
- Credential architecture: `Resource.credential_id` -> `Credential`.
- `CredentialRepository` with memory and PostgreSQL backends.
- Encrypted credential storage (AES-256-GCM envelope, per-encryption nonce).
- Keyring / key rotation (`GEMINI_GATEWAY_ENCRYPTION_KEYS` + `..._KEY_ID`; AAD
  binds credential id/type).
- Refresh-token persistence with fail-closed semantics; legacy Resource
  credential fields are migrated into encrypted Credentials in postgres mode.
- Admin API for Resource management and Credential management.
- Vue 3 + TypeScript + Vite Admin WebUI in `webui/`, served by FastAPI from
  `webui/dist`.

## Providers

Currently registered as builtin providers in `app/bootstrap.py`:

| provider_id | directory | notes |
|---|---|---|
| `fake` | `providers/fake/` | Offline test scenarios. |
| `anonymous_vertex` | `providers/anonymous_vertex/` | **Unofficial, reverse-engineered** Google Agent Platform (`batchGraphql`) interface; not a supported API. Each request goes through a live reCAPTCHA Enterprise token flow. |
| `firebase` | `providers/firebase/` | Firebase AI Logic (`firebasevertexai`). One Firebase Project = one Resource. |
| `gemini_cli` | `providers/gemini_cli/` | Google Code Assist (`cloudcode-pa.googleapis.com/v1internal`, OAuth Bearer). One Google account = one Resource. |
| `antigravity` | `providers/antigravity/` | Currently the only provider on the `ExecutionBackend` seam. |

`providers/vertex/` exists in the tree but is **not** registered as a builtin
provider by `app/bootstrap.py`, so it is not part of the current runtime
provider set.

## Credential / Persistence

```
Resource
  └── credential_id
        ↓
Credential
  ↓
CredentialRepository
  ├── memory    (default)
  └── postgres  (opt-in)
```

- PostgreSQL is an **optional** persistence backend, selected with
  `credential_repository.backend: postgres`. The default is the in-process
  memory store.
- Credential payloads are encrypted with **AES-256-GCM** before persistence.
- A keyring with key rotation is supported; `kid` selects the keyring entry at
  decryption time.
- The Credential API returns a **redacted** view and never returns raw secret
  payloads.
- Resources reference credentials through `credential_id`. New Admin API
  writes should not push secrets into Resource secret fields directly.
- Legacy per-provider Resource credential fields still exist as a
  compatibility / migration path only.

## Admin WebUI

- Vue 3 + TypeScript + Vite, source in `webui/`.
- Views: Dashboard, Resources, Credentials; secrets are redacted in the UI and
  in API responses.
- The Admin API is guarded by the `ADMIN_TOKEN` environment variable.
- Production build: `cd webui && npm install && npm run build`, output in
  `webui/dist`.
- FastAPI serves the built bundle via `mount_admin_assets()`. The production
  runtime is Python + Uvicorn only: **Node.js is not required at runtime**,
  only for frontend development and for rebuilding the bundle.

## Tests / Verification

- A pytest suite exists under `tests/` covering `core`, `protocol`,
  `providers`, `app` and `execution`.
- PostgreSQL E2E tests are **opt-in** and skipped unless a real database is
  configured (`tests/core/test_credential_postgres.py`).
- Some provider tests have pre-existing failures; fixing them is out of scope
  for documentation sync.
- This repository records no CI pipeline and no production deployment
  validation, so neither is claimed here.

## Known Limitations

- `anonymous_vertex` targets a non-official, reverse-engineered endpoint, so it
  is exposed to upstream changes, TLS ClientHello fingerprinting and anti-bot
  measures.
- `anonymous_vertex` obtains a reCAPTCHA Enterprise token for every request. The
  default runtime path is `fetch_recaptcha_token()`, which performs real Google
  reCAPTCHA anchor (GET) and reload (POST) requests against
  `https://www.google.com/recaptcha/enterprise/...`. `FakeRecaptchaTokenProvider`
  is a deterministic, network-free test double; it is only used when a
  `token_fetcher` is injected (or `set_token_fetcher()` is called), never as the
  runtime default. This makes the provider dependent on a live reCAPTCHA flow
  and on Google's endpoint availability.
- Model-family support varies per provider and follows each provider's
  configured model snapshot.
- Only `providers/antigravity/` has been migrated to `ExecutionBackend`; the
  other providers still use their legacy adapter path.

## Historical Milestones

Recorded for context only. These are **not** the current completion state.

- TASK-001 / TASK-001.5: core architecture; provider + resource factory
  decoupling into `app/bootstrap.py`.
- TASK-002: Anonymous Vertex provider.
- TASK-004: Firebase provider.
- TASK-008: Gemini CLI provider.
- TASK-009: Antigravity provider.
- AUTH-002 / AUTH-007 / AUTH-008 / AUTH-009 / AUTH-010 / AUTH-012: credential
  model, AES-256-GCM envelope, keyring and key rotation, PostgreSQL backend,
  legacy-credential migration.
- WEBUI-002: Vue 3 Admin WebUI.
