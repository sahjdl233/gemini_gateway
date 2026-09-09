# Gemini Gateway

Current Phase:
TASK-000

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

Test results (TASK-000 acceptance):
- 67 tests passed (pytest), covering:
  core (errors / models / resource / cooldown / pool / scheduler),
  protocol (OpenAI mapping + SSE helpers),
  providers (FakeProvider scenarios, no-network guarantee),
  app routes (/v1/models, /v1/chat/completions stream + non-stream, 400/404/429),
  config loader (env substitution, no secrets committed)
- Server boot verified: uvicorn starts, / and /v1/models respond, streaming SSE works.

Next:
TASK-001
