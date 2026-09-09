# Gemini Gateway

Current Phase:
TASK-002

Completed:
- Core architecture: models, resources, pools, scheduler, errors, health, cooldown
- Provider abstraction and unified error hierarchy
- Cooldown: Retry-After + exponential backoff + jitter
 429 handling as first-class (RateLimitError with scope + retry_after)
- Streaming-first architecture (Provider async generator -> SSE)
- FakeProvider: success / 429 / auth_error / timeout / stream scenarios
- ResourcePool: health/cooldown-aware, round-robin, in-flight tracking
- Scheduler: health-aware selection + cooldown + round robin + retry + fallback
- OpenAI-compatible API: GET /v1/models, POST /v1/chat/completions (stream + non-stream)
- Transport layer: proxy/egress seams independent from providers
- Config loader with env secret substitution (no database, no Docker requirement)
- pytest suite (core / protocol / providers / app)

TASK-001.5 (Provider Wiring & Resource Factory Cleanup):
- Provider registration fully decoupled from main.py
  - app/bootstrap.py: register_builtin_providers() is the single place to add a builtin provider
  - main.py no longer maintains any provider-specific factory map
- Resource creation decoupled from main.py
  - core/resource_factory.py: ResourceFactory protocol
  - providers/fake/factory.py: FakeProviderFactory + FakeResourceFactory
  - main.py no longer instantiates FakeResource directly (uses registry.create_resources)
- ProviderRegistry now owns both provider and resource creation
  - ProviderDefinition(provider_id, provider_factory, resource_factory)
  - registry.create() / registry.create_resources()
  - UnknownProviderError raised for unknown ids (no fallback to fake)
- ResourceKey identity preserved across providers/resources
- Only "fake" is registered in this phase (no real Google providers)

TASK-002 (Anonymous Vertex Provider):
- Anonymous Vertex / Agent Platform Studio batchGraphql protocol adapted
- docs/anonymous-vertex-protocol.md: full protocol archaeology document
- providers/anonymous_vertex/ package with 10 modules:
  - __init__.py: exports Provider, Resource, Factories
  - provider.py: AnonymousVertexProvider (Provider interface implementation)
  - resource.py: AnonymousVertexResource (Resource model)
  - factory.py: AnonymousVertexProviderFactory / AnonymousVertexResourceFactory
  - protocol.py: request/response conversion (ChatRequest -> Gemini -> ChatResponse/ChatChunk)
  - streaming.py: NDJSON brace-counting scanner, frame extraction, chunk normalization
  - headers.py: Chrome 150 fingerprint headers, x-browser-validation
  - signature.py: querySignature (fixed constant), thoughtSignature sentinel, content normalization
  - request.py: GraphQL envelope builder (requestContext, querySignature, variables)
  - recaptcha.py: reCAPTCHA Enterprise token fetch (anchor GET -> reload POST)
  - errors.py: upstream error parsing -> Gateway ProviderError mapping
- Only text-family models supported (10 models from config/models.json)
- Fixed querySignature: "2/l8eCsMMY49imcDQ/lwwXyL8cYtTjxZBF2dNqy69LodY=" (NOT an auth signature)
- thoughtSignature: pseudo-signature sentinel for history parts with functionCall/thought
- Streaming: NDJSON (brace-counting), NOT SSE; finishReason=UNSPECIFIED ignored
- SafetySettings: fixed 4x BLOCK_NONE per source
- reCAPTCHA token fetched per request (mocked in tests)
- Provider registered in app/bootstrap.py as "anonymous_vertex"
- 33 provider tests: request conversion, signature, response conversion, streaming, 429, Retry-After header extraction, malformed stream, mock integration
- 4 app wiring tests: build_runtime + /v1/chat/completions (non-stream & stream) through mock upstream, unknown-model 404
- Retry-After response header extracted on 429 and carried to Core Runtime (cooldown honours it exactly)
- No real network calls in tests; all via mock transport / recorded fixtures

Not implemented:
- Firebase (TASK-003)
- Vertex (TASK-004)
- Express (TASK-006)
- CLI (TASK-007)
- Build (TASK-008)
- Antigravity (TASK-009)
- Advanced scheduler strategies (P2C, weighted, race, latency/quota scoring)
- Management API (TASK-011)
- Production deployment (TASK-012)

 No real Google API requests; no Google credentials stored.

Test results (TASK-002 acceptance):
- 153 tests passed (pytest), covering:
  core (errors / models / resource / cooldown / pool / scheduler / factory-wiring),
  protocol (OpenAI mapping + SSE helpers),
  providers (FakeProvider scenarios, no-network guarantee, Anonymous Vertex protocol, mock integration),
  app routes (/v1/models, /v1/chat/completions stream + non-stream, 400/404/429),
  config loader (env substitution, no secrets committed),
  factory & wiring (registry create, unknown-provider error, resource factory,
  main.py no direct FakeResource, Resource identity)
- Server boot verified: uvicorn starts, / and /v1/models respond, streaming SSE works.

Next:
TASK-003

Known technical debt / limitations:
- TLS ClientHello fingerprint not reproduced by plain httpx (anti-bot); transport-level limitation
  documented in docs/anonymous-vertex-protocol.md. Provider uses swappable HTTP client.
- reCAPTCHA token fetch is a live google.com call; mocked in tests.
- Image/Audio model families exist in source but not implemented in provider.
- Function calling / tools conversion implemented but untested against live upstream.
