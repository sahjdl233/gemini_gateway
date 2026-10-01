# ADR: Resource Definition PostgreSQL Persistence — Design Decisions

**ID:** DB-RESOURCE-DESIGN-001
**Status:** Proposed
**Revision:** 2
**Scope:** Read-only source audit + design
**Implementation:** Not started

> This ADR is the design baseline for the subsequent DB-RESOURCE-001 implementation tasks.

Rev. 2 changes relative to rev. 1: added §2.5 Startup Bootstrap task; **rewrote §5 from
"DB commit point / lock makes failure impossible" to explicit compensation consistency**;
confirmed Firebase `app_id` as Credential material (§3).

This ADR is a design document only. No Python source, test, configuration, database, or
existing document was modified to produce it. Runtime reload / `reconcile_resources()`
work is explicitly **out of scope**.

---

## 1. Current source facts (with paths and lines)

| Fact | Reference |
|---|---|
| `ResourceKey` = provider + id; identity is composite | `core/resource.py:23-57` |
| `Resource` has `model_config = {"arbitrary_types_allowed": True}` — **no `extra="forbid"`** anywhere in repo | `core/resource.py:60-61` (repo-wide `ConfigDict`/`forbid` search: no other hit) |
| Runtime-only fields on the same model: `cooldown_until`, `in_flight`, `total_requests`, `total_failures`, `consecutive_failures` | `core/resource.py:60-84` |
| Pool has its **own** `asyncio.Lock`, held inside each method | `core/pool.py:111`, `:123`, `:130`, `:165-196` |
| Pool mutation methods **raise** on failure: duplicate key, in-flight removal, missing entry | `core/pool.py:124-125`, `:131-132`, `:133` |
| `ResourceManager` holds a **separate** `asyncio.Lock`, distinct from the Pool lock | `app/management.py:82` |
| Scheduler reaches resources only through `pool.acquire` / `release` (takes the Pool lock) | `core/scheduler.py:74-121`; `core/pool.py:168-181` |
| Scheduler does **not** touch the DB or the manager | `core/scheduler.py:29-123` |
| Current write order is Pool-first, then `_persist()`, with Pool compensation | `app/management.py:238-248` (create), `:263-270` (update), `:283-293` (delete) |
| Pool runtime `reconcile_resources` exists but nothing at startup calls it | `core/pool.py:201-292` |
| CredentialRepository: typed errors, `get`, deterministic `list` | `core/credential.py:114-119`, `:122-151` |
| Credential PG: inline `CREATE TABLE IF NOT EXISTS`, own `initialize()`, per-op commit/rollback/close, deterministic list | `core/credential_postgres.py:63-71`, `:187-195`, `:287-292` |
| AUTH-010 maps legacy provider secrets to credential records with stable ids | `core/credential_migration.py:54-66`, `:69-71` |
| Startup order: config → registry → credential init → `build_runtime` | `app/main.py:207-215`; app state at `app/main.py:231-234` |
| Unresolved credential refs only warn today | `app/main.py:128-135` |
| `ResourceManager` is Antigravity-only | `app/management.py:68-87`, `:114-121` |
| Postgres mode already suppresses writing secrets back to `config.yaml` | `app/management.py:89-100` |
| Admin payload whitelist + secret rejection at manager level | `app/management.py:27-60`, `:176-192` |
| Admin resource path carries `resource_id` only — no provider dimension | `app/routes/admin.py:108-173`, `:114-168` |
| Credential delete protection scans only the **live Pool** | `app/routes/admin.py` `_credential_is_referenced`, `:298-311` |
| AUTH-013 fail-closed lives in the auth adapter | `core/auth_adapter.py` `require_bound_credential`; `providers/antigravity/auth_adapter.py:140-164` |
| Factories reached via registry | `core/provider_registry.py:140-152`; registration `app/bootstrap.py:36-71` |
| YAML default load path; `config.yaml` gitignored | `config/loader.py:46-89` |
| Restart-persistence test only proves YAML | `tests/app/test_admin.py:363-379` |
| PG tests opt-in via `GEMINI_GATEWAY_TEST_DATABASE_URL` | `tests/core/test_credential_postgres.py:1-17` |
| Cross-provider id reuse explicitly tested and legal | `tests/core/test_resource_identity.py:14-18`, `:35-39` |

---

## 2. Decision table

### 2.1 ResourceDefinition persistence model

| Question | Decision | Reason |
|---|---|---|
| Separate DTO from runtime `Resource`? | **Yes** — a `ResourceDefinition` DTO union discriminated by `provider`. | Runtime `Resource` mixes config + health/counters (`core/resource.py:60-84`) and has no `extra="forbid"`. |
| Common fields | `provider`, `id`, `enabled`, `credential_id` — all four persisted. | Matches `Resource`/`ResourceKey` (`core/resource.py:23-57`, `:60-84`) and current `_PERSISTED_FIELDS` intent (`app/management.py:59`, `:124-128`). |
| Provider-specific fields | One strict DTO per provider, all `extra="forbid"`. DB column may be constrained `JSONB`, but JSON is only ever *emitted* by a validated provider DTO — never passthrough. | Satisfies allowlist-not-blacklist; rejects unknown fields instead of dropping them. |
| Re-validation on read | Select DTO by `provider` → strict validation → emit the raw definition dict the Factory already accepts → rebuild via `ProviderRegistry.create_resources()` (`core/provider_registry.py:140-152`). | Reuses the existing construction path. |
| Unknown provider | Hard error (`UnknownResourceDefinitionError`), not skipped. | Silent skip loses definitions and hides drift. |
| Unknown field | Hard error at DTO validation and at import. | Required by §3 rule 2. |
| Missing required field / type error | Hard error; no partial write, no default-fill. | "Required" means Pydantic-required fields in the DTO schema only. Provider-specific optional/defaulted fields are not "required" for this rule; defaults are schema-level and apply before validation. The persistence layer cannot fill missing/invalid fields after DTO validation. |
| Runtime-only fields | Excluded by construction: no `health`, `cooldown_until`, `in_flight`, `total_requests`, `total_failures`, `consecutive_failures`. DB reads always produce fresh runtime state. | `core/resource.py:60-84` |
| Secret material | No secret field exists on any DTO; `credential_id` is the only credential reference. | §3 |
| `serialize()` shape | **Unchanged.** Runtime-only display fields may appear in responses but are never written to the Repository. | `app/management.py:150-173` is a public contract. |

### 2.2 Identity and Repository contract

| Question | Decision | Reason |
|---|---|---|
| Primary key | Composite **`(provider, resource_id)`**; no global unique on `resource_id`. | `core/resource.py:23-57`; `tests/core/test_resource_identity.py:14-18`, `:35-39`. |
| `add` duplicate | Typed `DuplicateResourceDefinitionError`, no write. | Mirrors `core/credential.py:114-119`. |
| `get` missing | Return `None`. | Matches `CredentialRepository.get` (`core/credential.py:122-151`). |
| Strict variant | `require(...)` raises `UnknownResourceDefinitionError`. | Avoids `None` handling in the manager. |
| `list` ordering | **Guaranteed ascending `(provider, resource_id)`.** | Credential repo already does deterministic list (`core/credential_postgres.py:287-292`). |
| `update` | **Full replacement** of the DTO. PATCH merging happens in `ResourceManager`, not the Repository. | Avoids read-modify-write ambiguity. |
| `delete` missing | Idempotent no-op. | Matches current manager tolerance. |
| Repository types | **DTOs only**, never runtime `Resource`. | Keeps runtime state out of the persistence boundary. |
| `upsert` | **No generic `upsert`.** Import composes `get` + `add` in one transaction. | Import must *reject* conflicts, not silently overwrite. |
| Admin API identity | Keep Antigravity-only shape; multi-provider later must add a provider dimension or restrict explicitly. | `app/routes/admin.py:114-168`. Open question (§9), not silently decided. |

### 2.3 Schema lifecycle and startup

| Question | Decision | Reason |
|---|---|---|
| Table | `resource_definitions` | Distinct from `credentials` (`core/credential_postgres.py:63-71`). |
| Key | Composite PK `(provider, resource_id)` | §2.2 |
| Provider-specific storage | `JSONB definition` holding only validated non-secret provider fields. | Stable schema while provider DTOs evolve. |
| Schema version table (v1) | **None.** | Credential uses inline `CREATE TABLE IF NOT EXISTS` (`core/credential_postgres.py:63-71`); later column changes become a separate explicit migration task. |
| Idempotent init | Yes — repeated, non-destructive `CREATE TABLE IF NOT EXISTS`. | Same pattern as credentials. |
| Share connection/init with Credential? | Reuse DSN and connection factory; **separate repository and separate `initialize()`**. | Keeps Resource persistence independent of the Credential backend. |
| DB-level FK to `credentials`? | **No** — application-layer validation. | Resource persistence must stay backend-independent. |
| PG unreachable / init fails | **Startup fails loudly.** No YAML fallback, no empty repository. | Silent fallback makes DB mode non-deterministic. |

### 2.4 YAML → PostgreSQL import and dual-source semantics

YAML in PostgreSQL mode is a non-Resource config source plus an **optional explicit
one-time import source** (an *explicit one-time import*, not a startup merge). It is *not*
merged at startup.

| Question | Decision | Reason |
|---|---|---|
| 1. Automatic on first boot? | **No** — explicit command/operation only. | "Merge every start" has no defined conflict rule; explicit import does. |
| 2. Idempotent? | Yes. | Follows from the row rules. |
| 3. Stable import identity | `(provider, resource_id)`; **no second id space**. | Identity is already composite. |
| 4. Key absent from DB | Insert. | First import populates. |
| 5. Present, identical | No-op (report unchanged). | Idempotency. |
| 6. Present, different | **Whole import aborts.** No overwrite, no partial write. | Import must never clobber DB state. |
| 7. In DB, absent from YAML | **Keep.** Import never deletes. | Deletion is an explicit admin action. |
| 8. May import overwrite? | **Never.** | Rule 2. |
| 9. YAML file modified? | **No.** | Non-destructive. |
| 10. Later YAML edits in PG mode? | **No** — DB definitions are the single source; YAML `resources` is not read at startup. | Explicit "no merge" rule. |
| 11. PG → YAML switch | Requires an explicit **export** first, else DB-only edits are lost. Operator action, never automatic. | Only safe path. |
| 12. Separate export op? | **Yes** — DB → YAML snapshot into a new file (explicit export). | Needed for 11 and for backup. |
| Legacy secrets | Reuse AUTH-010 **before** import: material goes to `CredentialRepository` (`core/credential_migration.py:54-71`); Resource Repository receives only `credential_id`. Original YAML unchanged. | Existing, proven mechanism. |

**Runtime reload / reconcile remains explicitly out of scope.**

### 2.5 Startup Bootstrap task

Current startup builds resources from YAML inside `build_runtime` and never reads a
definitions store (`app/main.py:207-215`, `:231-234`); Pool `reconcile_resources` exists
but is uncalled at startup (`core/pool.py:201-292`). A DB-backed mode therefore needs an
explicit, ordered bootstrap stage.

**Required startup order (DB-backed mode):**

```
ResourceRepository
    ↓
ResourceDefinition validation
    ↓
build_runtime
    ↓
Pool
    ↓
Scheduler
```

**A DB-backed startup does NOT use `reconcile_resources()`.** The Pool is populated by
construction from validated definitions.

| Step | Action | Failure behavior |
|---|---|---|
| 1 | Load config; if `resource_repository.backend == "postgres"`, open a connection and run the non-destructive `resource_definitions` init | Fail startup |
| 2 | `repo.list()` → ordered `ResourceDefinition` DTOs | Fail startup |
| 3 | Validate each DTO (whitelist, unknown-provider/field rejection, credential-reference existence) | Fail startup — no partial Pool |
| 4 | Convert validated DTOs to raw definitions and pass into the **existing** `build_runtime` / `ProviderRegistry.create_resources()` path (`core/provider_registry.py:140-152`) so the Pool is populated by construction | Fail startup |
| 5 | Run AUTH-010 legacy migration for YAML-sourced material, then AUTH-013 fail-closed checks apply as today (`app/main.py:128-135` becomes a hard failure in DB mode) | Fail startup |
| 6 | Construct the scheduler against the already-populated Pool; only then serve traffic | — |

**Constraints**

- Bootstrap **replaces** the YAML `resources` source in DB mode; it is **not** a
  post-startup `reconcile_resources()` call. Runtime reconcile stays out of scope.
- All definitions are validated **before** the Pool is constructed, so there is no window
  in which the Pool holds unvalidated or unpersisted state.
- In YAML (default) mode, step 1 is skipped entirely and behavior is identical to today
  (`config/loader.py:46-89`).
- Follow-up task: **DB-RESOURCE-001-0 — Startup Bootstrap ordering**, inserted ahead of
  001-5.

---

## 3. Field whitelist table

Common persisted fields for **all** providers: `provider`, `id`, `enabled`,
`credential_id`.

### Antigravity — `providers/antigravity/resource.py:14-26`

| Allowed | Rejected (secret) |
|---|---|
| `project_id`, `ide_type` | `access_token`, `refresh_token`, `client_id`, `client_secret`, `token_expiry` |

> `token_expiry` is runtime/rotated credential state and does **not** enter the persisted
> ResourceDefinition.

### Gemini CLI — `providers/gemini_cli/resource.py:20-68`

| Allowed | Rejected |
|---|---|
| `project_id`, `tier`, `pinned_model`, `ide_type`, `platform`, `plugin_type`, `preview`, and `proxy` **only if credential-free** | all OAuth/token fields; `proxy` **rejected** if it contains userinfo, token query params, or any embedded credential |

### Firebase — `providers/firebase/resource.py:16-31`

| Allowed | Rejected (Credential material) |
|---|---|
| `project_id`, `pinned_model`, and `proxy` under the same credential-free rule | `api_key`, `app_id`, `debug_token` |

> **Confirmed: `app_id` → Credential material.** `providers/firebase/resource.py:24`
> declares `app_id: str`, and `core/credential_migration.py:56` lists
> `firebase: ("api_key", "app_id", "debug_token")` as durable credential fields mapped to
> `CredentialType.API_KEY` (`:65`). It is therefore **rejected** from the Resource
> Repository and reachable only through `credential_id`. It must not be placed back into
> the ResourceDefinition.

### Anonymous Vertex — `providers/anonymous_vertex/resource.py:16-48`

| Allowed | Rejected |
|---|---|
| `proxy_scheme`, `proxy_host`, `proxy_port`, `pinned_model` | `proxy_username`, `proxy_password` |

> **Anonymous Vertex v1 policy:** proxy configuration carrying authentication information
> is **not persisted**. Only the credential-free proxy endpoint components are allowed.
> The current Credential mechanism does **not** cover Anonymous Vertex proxy credentials
> (`core/credential_migration.py:52-53` explicitly excludes `anonymous_vertex`), and this
> ADR does **not** assume otherwise. See §9 open question 3.

### Fake — `providers/fake/provider.py:47-50`

| Allowed | Rejected |
|---|---|
| `scenario`, `retry_after`, `reply_text`, `model_ids` | none (no secret fields) |

### Rules applied

1. **Allowlist, not denylist** — the tables *are* the allowlist; anything absent is rejected.
2. **Unknown fields rejected, never silently dropped** — `extra="forbid"` on every DTO,
   plus import-time rejection.
3. **`proxy` may be persisted only as a credential-free URL.** Validation rejects
   `user:pass@`, token query params, or equivalents. Credential-bearing proxies must move
   to `Credential`.
4. No field named `*token*`, `*password*`, `*secret*`, `*key*`, `*authorization*` may appear
   in any DTO.

---

## 4. Import state table

| State | First import | Repeat import | Conflicting definition | DB-only (removed from YAML) |
|---|---|---|---|---|
| PG mode, DB empty | Insert all | — | — | — |
| PG mode, key absent in DB | Insert | Insert | Insert | Keep (untouched) |
| Same key, identical definition | Insert | **No-op** | **Abort entire import** | Keep |
| Same key, different definition | — | — | **Abort; never overwrite** | — |
| YAML file itself | Read-only, unchanged | Unchanged | Unchanged | Unchanged |
| Switch to YAML mode | Requires explicit export first | — | — | DB-only rows lost without export |

Import is an **explicit one-time import**; it is never a per-startup merge.

---

## 5. Write-consistency: compensation model

**Premise.** DB and Pool cannot form a real cross-resource transaction. **No ordering makes
Pool failure impossible**: `add_resource` raises on duplicate (`core/pool.py:124-125`),
`remove_resource` raises on in-flight (`:131-132`) and on a missing entry (`:133`), and a DB
commit can succeed while a subsequent in-memory step still raises. Any claim of a
"DB commit point" guaranteeing atomicity across Pool and DB is **withdrawn**.

### 5.1 Concurrency boundary (verified)

- The Pool owns a private `asyncio.Lock` (`core/pool.py:111`), acquired **inside**
  `add_resource`, `remove_resource`, `acquire`, `release`, `record_*` (`:123`, `:130`,
  `:165-196`). There is no public API to hold it across an external call.
- `ResourceManager` owns a **separate** lock (`app/management.py:82`); it serializes
  management ops but does **not** exclude the scheduler.
- The scheduler reaches resources only via `pool.acquire` / `pool.release`
  (`core/scheduler.py:74-121`), i.e. through the Pool lock. It never touches the DB or the
  manager.
- Consequence: between a Pool mutation and its compensation, an in-flight request **can**
  acquire or use that resource. Compensation therefore cannot assume "nobody saw it".
- Management ops are serialized against each other by the manager lock, so a compensating
  action can never race another management op.

### 5.2 Consistency target

**Compensation consistency**: after any failed operation, the Pool and the Repository are
each left in a state that is either (a) the pre-operation state, or (b) an explicitly
reported divergent state. The system never claims atomicity; it makes divergence
detectable and bounded. This is **compensation consistency**, not a cross Pool/DB atomic
transaction.

### 5.3 Per-scenario defined results

| Scenario | Defined result |
|---|---|
| DB write fails **before** any Pool change | Op fails; Pool untouched. |
| Pool mutation raises (duplicate / in-flight / missing) | Op fails; **no DB write is attempted**; Pool unchanged because the Pool method raised before mutating (`core/pool.py:124-126`, `:131-133`). |
| Pool changed, then DB write fails | Compensate the Pool: `create` → `remove_resource`; `update` → restore snapshot fields; `delete` → re-`add_resource`. If compensation itself raises, the op fails **and** the divergence is surfaced (log + error), because it must not be silently swallowed. |
| DB write succeeds, Pool change then fails | Compensate the **DB** with an inverse operation (`delete` after a successful create, restore prior DTO after an update, re-`add` after a delete), inside a new transaction. If the inverse fails, the op fails and divergence is surfaced. |
| Delete: DB succeeds, Pool removal fails (e.g. became in-flight) | Compensate the DB by re-inserting the deleted DTO; Pool unchanged; op fails with a conflict error. |
| Credential reference missing | Validated **before** any write via `CredentialRepository.get()`; op fails, nothing persisted, Pool untouched. |

### 5.4 Ordering

Retain the existing **Pool-first, persist-second, compensate-on-failure** order already
implemented at `app/management.py:238-248`, `:263-270`, `:283-293`. Rationale from the
verified boundary: the Pool methods validate their preconditions before mutating
(`core/pool.py:124-126`, `:131-133`), so a Pool-side rejection happens with no DB
round-trip; the compensation path is already the tested behavior of the current code.

**Create**

1. Validate DTO, validate `credential_id`, reject duplicates.
2. `pool.add_resource()` — may raise; if it raises, abort with no DB call.
3. `_persist()` → DB commit.
4. On DB failure: `pool.remove_resource()`; if that raises, surface the divergence.

**Update**

1. Build + validate the complete target DTO (PATCH merged in the manager).
2. Under the manager lock: snapshot current values → `setattr`.
3. DB full update.
4. On DB failure: restore the snapshot before releasing the lock.

**Delete**

1. Confirm present and `in_flight == 0`.
2. `pool.remove_resource()` — may raise; if it raises, abort with no DB call.
3. DB delete.
4. On DB failure: re-`add_resource`; if that raises, surface the divergence.

**API success response** returns only after both the DB commit and the Pool application
completed. `serialize()` keeps its current shape (`app/management.py:150-173`).

**Credential deletion protection** must be extended: `_credential_is_referenced` currently
scans only the live Pool (`app/routes/admin.py:298-311`) and must also consult the
definitions Repository.

**Persisted-mode credential requirement**: for Antigravity / Gemini CLI / Firebase DB
definitions, `credential_id` should be **required non-null**; legacy YAML entries enter DB
only after AUTH-010-style migration. AUTH-013 fail-closed behavior (`core/auth_adapter.py`,
`providers/antigravity/auth_adapter.py:140-164`) is preserved unchanged.

---

## 6. Logical schema draft (no SQL)

```
resource_definitions
  provider        text      ─┐ composite primary key
  resource_id     text      ─┘
  enabled         boolean   not null
  credential_id   text      nullable   (validated in app layer; no DB FK)
  definition      jsonb     not null   (validated, non-secret provider fields only)
```

Reusable from Credential (`core/credential_postgres.py:63-71`, `:187-195`, `:287-292`): same
DSN/connection factory, separate repository with its own `initialize()`, per-operation
commit/rollback/close, fail-fast on DB error, deterministic list ordering.
**Not** reused: Credential payload encryption — the Resource table intentionally holds no
secret material.

---

## 7. Impact on existing code (conflicts only, no fixes applied)

| Existing behavior | Impact |
|---|---|
| YAML default mode (`config/loader.py:46-89`) | **Unaffected** — PG remains opt-in; bootstrap step 1 skipped. |
| AUTH-010 legacy migration (`core/credential_migration.py:54-71`) | Reused as-is by import and bootstrap step 5. |
| AUTH-013 dangling-credential fail-closed | Unchanged; DB validation adds an earlier, stricter gate. |
| Postgres-mode secret suppression (`app/management.py:89-100`) | Preserved; DB mode does not write secrets to YAML at all. |
| Admin API whitelist / secret rejection (`app/management.py:27-60`, `:176-192`) | Generalized into per-provider DTOs; Antigravity behavior preserved. |
| Admin API response shape (`app/management.py:150-173`) | Unchanged. |
| Admin resource path carries `resource_id` only (`app/routes/admin.py:114-168`) | **Conflict:** cannot express composite identity for multi-provider. Needs maintainer decision (§9). |
| Pool runtime state / reconcile (`core/pool.py:201-292`) | Not used by persistence; runtime reload out of scope. |
| Startup order (`app/main.py:207-215`, `:231-234`) | Gains the §2.5 bootstrap stage between config load and `build_runtime`. |
| Warning-only unresolved credentials (`app/main.py:128-135`) | Hard failure in DB mode; YAML mode unchanged. |

---

## 8. Follow-up task adjustments for DB-RESOURCE-001

| Task | Recommendation |
|---|---|
| **001-0 (new)** | **Startup Bootstrap ordering** — ResourceRepository → ResourceDefinition → build_runtime → Pool, per §2.5. Must land before 001-5. |
| 001-1 DTO layer | **Keep**; per-provider `extra="forbid"` + allowlist as acceptance criterion. |
| 001-2 Repository contract | **Keep**; pin composite key, `get`→`None`, `require`→typed error, deterministic `list`, full-replacement `update`, idempotent `delete`, no generic `upsert`. |
| 001-3 Schema + initialize | **Keep**; add "PG failure blocks startup". |
| 001-4 YAML import | **Keep**, scoped as *explicit one-time import*; add prerequisite **001-3b: explicit export (DB → YAML snapshot)**. |
| 001-5 Manager consistency | **Keep**, but rewrite against §5: compensation consistency, explicit divergence reporting, no "cannot fail" claims. |
| 001-6 Admin API integration | **Hold** pending §9 open question 2 (multi-provider identity). |
| 001-7 Tests | **Keep**; opt-in PG via `GEMINI_GATEWAY_TEST_DATABASE_URL` (`tests/core/test_credential_postgres.py:1-17`), plus DTO allowlist/rejection and compensation-path tests. |
| **001-8 (new)** | Extend credential-reference protection to the definitions Repository (`app/routes/admin.py:298-311`). |

---

## 9. Open questions (maintainer decision required)

1. **Export surface** — where DB → YAML export lives (CLI subcommand, admin endpoint,
   script) and the exact YAML shape emitted. `config/loader.py:46-89` defines loading, not a
   symmetric dump.
2. **Admin API multi-provider identity** — add a `provider` path dimension, or keep the API
   Antigravity-only while the Repository supports all five providers? Blocked by
   `app/routes/admin.py:114-168`. Currently an **open question**.
3. **Anonymous Vertex proxy credentials** — add a Credential kind for
   `proxy_username`/`proxy_password`, or declare them unsupported for v1?
   `core/credential_migration.py:52-53` shows no current coverage.

All other decisions here are determined by source evidence.

---

## 10. Verification record

- **Audited HEAD:** `12e4c94763120ceb00127d219368f8dc0575ff04`
  (`12e4c94 TASK-STATE-001-FIX-01: fix resource reconcile and add pool reconcile tests`)
- **Tests run:** none. Static source audit + design only; no disputed runtime behavior
  required execution.
- **Read-only commands used during the audit:** `git rev-parse HEAD`, `git log --oneline -1`,
  `git status --short`, `Get-Content -LiteralPath <file>`,
  `Select-String -LiteralPath <file> -Pattern <regex>`, `Get-ChildItem -Recurse -File`,
  `Test-Path <path>`, plus repo-wide read-only searches for `GEMINI_GATEWAY_DATABASE_URL`,
  `credential_repository`, `PostgreSQL`, `config.yaml`, `_persist`, `create_resources`,
  `extra=|model_config|ConfigDict|forbid`,
  `proxy_username|proxy_password|get_proxy_config|resource.proxy`, and
  `Lock|def |RLock|threading` over `core/pool.py` + `core/scheduler.py`.
- **Source/test/config/database changes made by this ADR:** **none.** This document is the
  only added file.
- **Not in scope of this ADR:** runtime reload, `reconcile_resources()` wiring,
  `ResourceRepository` implementation, PostgreSQL migration, `build_runtime()` changes,
  `ResourceManager` changes, Admin API changes, YAML import/export implementation,
  startup bootstrap implementation, multi-instance coordination, Resource-table
  encryption.

