# DB-RESOURCE-DESIGN-003 — ResourceDefinition Persistence Boundary (ADR)

Status: accepted (boundary freeze — no PostgreSQL implementation in this task)
Scope baseline: DB-RESOURCE-001..008
Next: the PostgreSQL implementation of the durable definition store, gated by
this ADR's frozen contract.

---

## 0. Problem

Two abstractions currently coexist, and a third (durable PostgreSQL
definitions) is about to be built.  Without freezing the boundary first, the
schema and the repository interface would be redesigned repeatedly under
pressure from whatever the next feature needs (admin UI, reload, audit).

Existing pieces:

| Piece | Contract | Role |
|---|---|---|
| `core/resource_repository.py` (001-2) | `ResourceRepository` ABC — async CRUD: add/get/require/list/update/delete | **Durable sink** (bootstrap apply target) |
| `core/resource_definition_repository.py` (004) | `ResourceDefinitionRepository` Protocol — read-only `list_definitions` / `get_definition` | **Definition source** (bootstrap diff, runtime reconciliation) |
| `core/resource_postgres.py` (001-4) | `PostgreSQLResourceRepository(ResourceRepository)` | PostgreSQL CRUD (existing, definition-shaped schema) |
| `core/resource_repository_memory.py` (006) | `MemoryResourceRepository(ResourceRepository)` | In-memory sink |
| `core/resource_definition_loader.py` (003-2) | `ConfigResourceDefinitionRepository` (004 impl) | YAML/config source |
| `ResourceRepositoryDefinitionSource` (005) | adapter | Connects a durable store to the read Protocol |

Proven by DB-RESOURCE-004..008: the abstraction holds — the same bootstrap
and reconciliation services run unchanged over Config, Memory and
PostgreSQL-fake stores.

---

## 1. Decision: the contract hierarchy is frozen as-is

```
                ResourceDefinitionRepository (read-only Protocol, 004)
                        ▲                        ▲
        Config impl (003-2)               Memory impl (004)
                        \
                         \  (a durable store reaches the read side
                          \  ONLY via ResourceRepositoryDefinitionSource)
                           \
                ResourceRepository (CRUD ABC, 001-2)   ← the future PostgreSQL
                        ▲                               definition store is THIS,
        Memory impl (006) ─ PostgreSQL impl (future)    not a merged interface
```

* The PostgreSQL definition store implements **`ResourceRepository`** (the
  CRUD ABC) — exactly like `PostgreSQLCredentialRepository` does for
  credentials.  It does NOT implement `list_definitions`/`get_definition`
  directly; read-side consumers keep using
  `ResourceRepositoryDefinitionSource`.
* No new protocol is introduced, and the two existing ones are never merged.
  Read and write surfaces stay separate: bootstrap diff/plan and runtime
  reconciliation read; bootstrap apply and (future) Admin API write.
* Acceptance gate for the future implementation: the unmodified contract
  suite `tests/core/test_resource_repository_contract.py` (now parameterized
  over fake / memory / postgres, DB-RESOURCE-009) must pass, plus the
  existing `tests/core/test_resource_postgres_contract.py` lifecycle tests
  (commit/rollback/close, cancellation).

### Frozen CRUD semantics (identical for every implementation)

| Operation | Frozen semantics |
|---|---|
| `add` | INSERT; duplicate composite key → `DuplicateResourceDefinitionError(provider, resource_id)`; driver errors never exposed as contract errors |
| `get` | missing → `None`, never raises |
| `require` | missing → `UnknownResourceDefinitionError(provider, resource_id)` |
| `list` | deterministic ascending by `(provider, resource_id)`; optional provider filter ascending by `resource_id` |
| `update` | full replacement decided by the write itself (rowcount); missing → `UnknownResourceDefinitionError`; **never an upsert** |
| `delete` | idempotent; unknown key is a no-op |

### Frozen identity rules

1. Identity is the composite `(provider, resource_id)` — `resource_id` is
   never globally unique and never gets a standalone UNIQUE constraint.
2. `provider` is a stored column (the discriminant), never derived from the
   JSONB body; a `provider` key inside the stored body is rejected on read
   (`resource_definition_from_row`).
3. No foreign key from `resource_definitions.credential_id` to `credentials`
   — the reference is loose by contract; integrity is the credential layer's
   concern.
4. The JSONB `definition` column holds only the provider-specific
   allowlisted body (`to_definition_json()`); runtime state, secrets and
   metadata never enter it (guarded by the 001-5 serialization audit tests).

---

## 2. Decision: what the PostgreSQL implementation must look like (when built)

* Schema: the existing `resource_definitions` table (001-3) — unchanged.
  `CREATE TABLE IF NOT EXISTS`, composite PK, JSONB body, no FK, no extra
  indexes until query evidence demands one.
* Async all the way (001-4 `_transaction` pattern: commit on success,
  rollback + propagate on failure, close on both paths, `BaseException`-safe
  against cancellation).
* Statement constants + duck-typed async connections so the existing fakes
  apply unchanged.
* It plugs into `create_resource_definition_repository` (the factory) as a
  third source option **only** when a durable backend is explicitly
  configured; config/memory remain the defaults (ADR-002 §2).

---

## 3. Decision: no `version` column — rejected (for now)

Considered and **rejected**:

* **No consumer exists.** Definitions are config-shaped records with
  full-replacement updates; nothing reads a version.  YAGNI.
* **The DTO must stay clean.** `ResourceDefinition` is a strict,
  `extra="forbid"` persistence DTO.  Adding `version`/`updated_at` would
  either pollute the DTO (and then every YAML import would need to fake
  values) or force a second "stored envelope" type — two shapes to keep in
  sync for zero current benefit.
* **The frozen equality property would break.** The contract suite now
  asserts `stored == original DTO` with no metadata attributes; adding
  timestamps means redefining equality semantics across every
  implementation and test.
* **Bootstrap has no concurrency.** Definitions are written at startup
  (single writer) and diffed against the same store they wrote.

Re-introduction triggers (any ONE of these justifies an ADR revision):

1. An Admin UI shows "last modified" per definition (needs `updated_at`).
2. Multi-process deployment with concurrent Admin mutations needs
   conflict detection (needs `version`).
3. An audit/compliance requirement demands change tracking (better served
   by an append-only audit table than by columns on the definition row).

---

## 4. Decision: no optimistic concurrency — rejected (for now)

Considered and **rejected**:

* Current writers are sequential: startup bootstrap (single flow) and —
  in the future — Admin API mutations, which are request-scoped and
  human-paced.  Lost-update risk is negligible at this scale.
* Optimistic concurrency would add `version` to the schema, a
  compare-and-swap variant of `update` (or a precondition parameter) to the
  frozen contract, and a mapped conflict error to the Admin layer — a
  three-layer change for a problem that does not exist yet.
* The full-replacement `update` with `rowcount`-decided existence is
  already atomic per operation (001-4 `_transaction`); concurrent identical
  replacements are harmless, and concurrent duplicate `add`s are guarded by
  the composite PK.

Re-introduction triggers: multi-writer deployment (same as §3 trigger 2),
or the Admin API growing programmatic (non-human) writers.  The chosen
mechanism at that point would be a `version`/`updated_at` precondition on
`update` — a contract revision requiring this ADR to be reopened, not a
stealth schema change.

---

## 5. Frozen boundary tests (landed with this ADR)

* `tests/core/test_resource_repository_contract.py` — the CRUD suite now
  runs over **three** implementations (fake store, production memory sink,
  PostgreSQL-over-fake) and asserts:
  * the frozen CRUD/identity semantics table above;
  * `stored == original DTO` with **no** `updated_at` / `created_at` /
    `version` / `revision` / `etag` attributes, on `add` and after `update`
    (§3 decision made executable);
  * the read-side implementations (`Config`/`Memory`) expose no write verbs
    — the read/write split is structural, not conventional.
* The PostgreSQL implementation, when built, joins this suite as a fourth
  parameter — no test edits expected, only a factory entry.

---

## 6. What this ADR deliberately does not do

* No PostgreSQL implementation (gated on the frozen contract, next task).
* No Admin API write-path switch, no reload/watcher, no audit table.
* No schema change of any kind.
