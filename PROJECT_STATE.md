# Gemini Gateway

Current Phase:
TASK-001.5

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
- Only 'fake' is registered in this phase (no real Google providers)

Not implemented:
- Firebase (TASK-002)
- Vertex (TASK-004)
- Express (TASK-006)
- CLI (TASK-007)
- Build (TASK-008)
- Antigravity (TASK-009)
- Advanced scheduler strategies (P2C, weighted, race, latency/quota scoring)
- Management API (TASK-011)
- Production deployment (TASK-012)

 No real Google API requests; no Google credentials stored.

Test results (TASK-001.5 acceptance):
- 116 tests passed (pytest), covering:
  core (errors / models / resource / cooldown / pool / scheduler),
  protocol (OpenAI mapping + SSE helpers),
  providers (FakeProvider scenarios, no-network guarantee),
  app routes (/v1/models, /v1/chat/completions stream + non-stream, 400/404/429),
  config loader (env substitution, no secrets committed),
  factory & wiring (registry create, unknown-provider error, resource factory,
  main.py no direct FakeResource, Resource identity)
- Server boot verified: uvicorn starts, / and /v1/models respond, streaming SSE works.

Next:
TASK-002

