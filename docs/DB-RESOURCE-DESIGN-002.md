# DB-RESOURCE-DESIGN-002 — Resource Bootstrap & YAML Migration Strategy (ADR)

Status: accepted (design only — no implementation in this task)
Scope baseline: DB-RESOURCE-001-0..5 (DTO, contract, schema, CRUD, verification)
Next: DB-RESOURCE-003 — Resource bootstrap service (implements this ADR)

---

## 0. Context

DB-RESOURCE-001 delivered the durable layer:

* `core/resource_definition.py` — strict, provider-discriminated DTOs
  (`extra="forbid"`, secret fields rejected at the boundary).
* `core/resource_repository.py` — async `ResourceRepository` contract
  (composite `(provider, resource_id)` identity, typed identity errors).
* `core/resource_postgres.py` — `resource_definitions` table (composite PK,
  JSONB body, no FK to credentials) + async CRUD + repeatable `initialize()`.

YAML (`config.yaml`, per-provider `resources:` lists) is still the *only*
source from which runtime `Resource` objects are built:

```
create_app (sync, app/main.py:216-230)
  config (load_config / default_config)
  registry            (build_provider_registry)
  credential store    (build_credential_store — durable PG or in-memory)
  AUTH-010 migration  (_migrate_legacy_credentials_if_durable)
  build_runtime(cfg)  (creates Resource objects from YAML definitions)
  ResourceManager     (Admin API mutations; writes back to YAML via _persist())
```

This ADR decides how the system moves to PostgreSQL as the durable resource
definition store, in what order bootstrap runs, and what happens when YAML
and the database disagree. It deliberately does **not** implement anything:
bootstrap, migration, CLI, Admin API, YAML parser changes are all
DB-RESOURCE-003+ work.

---

## 1. Source of Truth

### Alternatives

| | A: YAML only | B: YAML primary + DB cache | C: DB primary + YAML import |
|---|---|---|---|
| Durable across deployments / containers | No — container filesystem loss loses admin-made changes | Yes (as a cache) | Yes |
| Single authority for a definition | YAML | Ambiguous — "primary" YAML but DB holds admin edits that must round-trip back into YAML | DB, unambiguously |
| Conflict surface | None (nothing else writes) | Every startup must reconcile YAML↔DB and decide a write-back direction | Only at import time, only when an operator asks for it |
| Admin API write path | `_persist()` to YAML (current, `app/management.py:300-351`) | Must write YAML *and* DB, atomically — two stores, one transaction boundary | One DB transaction |
| Matches credential precedent (AUTH-009) | No | No | Yes — credentials already treat PostgreSQL as the durable store with fail-loud startup |

### Decision: C — PostgreSQL is the runtime source of truth; YAML becomes an import seed.

Rationale:

1. **Single writer, single authority.** B reintroduces the dual-write
   problem `_persist()` already has (YAML rewrite must preserve unrelated
   config keys verbatim, `app/management.py:325`). With C, the Admin API
   write path becomes one DB transaction; YAML is only ever *read*.
2. **Precedent.** Credentials made exactly this move in AUTH-008/009:
   durable repository is authoritative, startup fails loudly when the
   durable store was explicitly configured. Resource definitions should not
   invent a second pattern.
3. **The DTO layer was built for this.** `resource_definition_from_row()`
   reconstructs strict DTOs from `(provider, resource_id, enabled,
   credential_id, definition)` columns (verified losslessly in
   DB-RESOURCE-001-5); runtime `Resource` construction can consume
   `to_runtime_definition()` output from DB rows without provider changes.

Consequences:

* `build_runtime` (DB-RESOURCE-003+) reads definitions from the repository,
  not from `cfg["providers"][*]["resources"]`.
* `ResourceManager._persist()` (YAML rewrite) is **retired** for resource
  mutations once bootstrap lands; until that refactor happens it stays
  frozen as-is (this ADR does not change Admin API behavior).
* YAML files remain supported as a *seed* and for local/no-DB deployments;
  they stop being authoritative the moment the durable repository is
  configured.

---

## 2. YAML Migration Strategy

### First-deployment flow (empty `resource_definitions` table + existing resources.yaml)

```
startup
  │
  ├─ resource repository initialize()      (CREATE TABLE IF NOT EXISTS)
  │
  ├─ detect: table empty?
  │     ├─ yes → AUTO-IMPORT (see decision below)
  │     │     ├─ load YAML resource sections
  │     │     ├─ parse_resource_definition()  — strict DTO validation
  │     │     ├─ any invalid definition → FAIL STARTUP (fail-closed, list
  │     │     │   every offending entry; never skip or coerce)
  │     │     ├─ insert all definitions (one transaction per import batch)
  │     │     └─ proceed to runtime build from DB
  │     └─ no  → drift check (§4)
  │
  └─ build runtime from DB rows
```

**Decision: the empty-table import is automatic, but strictly fail-closed
and opt-out-able.**

* Automatic because it is provably non-destructive: the table is empty, so
  nothing can be lost, and it is idempotent (an interrupted startup simply
  finds a partially/fully populated table next time and enters the drift
  path instead).
* Fail-closed because the DTO layer's contract is "unknown field, runtime
  field, or secret field fails loudly" — the import must not downgrade that
  to warnings.
* A config switch lets an operator decline the auto-import entirely
  (pure-DB deployments, tests).

**Config shape note (for 003):** implement the switch as an enum, not a
boolean — `resource_bootstrap.mode: auto | disabled | required` (plus
`check`, see §4). A boolean is sufficient for "import or not" today but
known to accrete flags later (dry-run, validate-only, migration-only are
all plausible future wants); an enum mode absorbs them without a config
schema break. `auto` = empty-table import + optional drift check;
`disabled` = never read YAML; `required` = startup fails if the table is
empty and no import source succeeds.

### Subsequent startups (non-empty table)

DB is authoritative and YAML is no longer a runtime dependency — the happy
path must not read YAML on every startup. The drift check of this section
is therefore **opt-in**, expressed as `resource_bootstrap.mode: check` (or
an explicit import command run), never a per-startup tax:

* default (`mode: auto`, non-empty table): build runtime from DB, no YAML I/O;
* `mode: check`: perform the §4 comparison and fail loudly on drift —
  intended for operators who want startup-time verification that DB and
  seed still agree (e.g. right after adopting DB-primary, or in CI);
* drift resolution always goes through the explicit import command (§7).

### What is never automatic

* Overwriting a changed DB row from YAML.
* Deleting a DB row that no longer exists in YAML (the import is additive;
  removal is an Admin API / explicit operation).
* Silently skipping invalid YAML entries.

---

## 3. Startup Bootstrap Ordering

Current:

```
config → registry → credential init → build_runtime
```

Target:

```
config
  → registry
  → credential store init + AUTH-010 legacy migration   (unchanged)
  → resource repository initialize()                    (schema, fail-loud)
  → resource bootstrap                                  (import / drift check)
  → build_runtime                                       (reads DB, not YAML)
```

Why bootstrap must complete before runtime build:

1. **Runtime objects are derived state.** `Resource` instances are built
   from definitions (`to_runtime_definition()`); building them from YAML
   first and reconciling the DB afterwards would mean runtime state exists
   that the durable store disagrees with — the exact class of bug the
   Pool-first/write-compensation discussion in DESIGN-001 §6 was about.
2. **credential_id validation needs the credential store ready.** The
   bootstrap warning for unresolved references (§5) can only be emitted
   after credential init; ordering after AUTH-010 migration also means the
   legacy migration sees the same definitions the bootstrap will persist.
3. **Fail-before-serve.** Schema errors, import validation errors and drift
   failures must abort startup before the HTTP surface accepts traffic —
   same semantics the credential repository already has ("database failure
   propagates; never silently replaced by an in-memory store").

Implementation note for DB-RESOURCE-003 (recorded here, not decided):
`create_app` is synchronous while the repository is async. The bootstrap
service will need an explicit async bridge (e.g. run the bootstrap step
inside the lifespan startup hook, or drive the repository with
`asyncio.run()` in the sync path). That is an implementation decision for
003, deliberately out of scope here.

---

## 4. Conflict Policy

Situation: DB has resource A; YAML also declares resource A with different
content.

| Option | Verdict |
|---|---|
| Overwrite DB from YAML | Rejected as a *default* — silently destroys Admin API edits made since deploy |
| Ignore YAML | Rejected as a *default* — operator edits config.yaml, restarts, nothing changes, no signal why |
| Fail startup | **Default.** Loud, deterministic, forces a human decision — matches the project's fail-closed precedent (credential startup, DTO validation) |
| Explicit migration command | **The resolution tool.** `resource import-yaml --overwrite` (§7) is how an operator deliberately accepts YAML over DB |

Precise default behavior:

* For each `(provider, resource_id)` present in both places, compare the
  canonical DTO payloads (columns + `to_definition_json()` body), not raw
  YAML text (comments/formatting must not cause false drift).
* Identical → no-op. Different → startup fails with the full list of
  differing keys and both payloads, plus the exact command to resolve.
* YAML entries missing from DB and `import_yaml: true` → additive insert
  (same as empty-table import: validated, fail-closed).
* DB rows with no YAML counterpart → untouched (DB is authoritative;
  YAML is a seed, not a sync source).

---

## 5. Credential Dependency

`ResourceDefinition.credential_id` is a **loose reference** — frozen by the
DB-RESOURCE-001-2 contract: the repository never resolves it, never touches
credential secrets, and no FK exists to `credentials`.

Bootstrap-time check design:

* After credential init, for each imported/loaded definition with a
  non-null `credential_id`, verify the credential exists in the credential
  store.
* **Failure policy: warn, keep the definition, do not fail startup.**
  Rationale: this matches current behavior for YAML-built resources
  (`app/main.py:128-135` warns on unresolved references), keeps a
  credential-store outage from escalating into a resource-store outage, and
  preserves the loose-reference boundary (the resource layer must not
  enforce credential integrity — that is the credential layer's job).
* Rejected alternatives:
  * *startup fail* — would couple resource availability to credential
    timing and break the documented warning contract;
  * *disable resource* — bootstrap silently rewriting `enabled` would
    violate "the repository does not fill DTO defaults" and hide the
    problem from the operator; a disabled resource should be an explicit
    operator action via the Admin API.
* The warning must name `(provider, resource_id)` and the missing
  `credential_id` so the log is actionable.

---

## 6. Anonymous Vertex / Proxy Credential Policy (carried over from ADR-001 §3)

Confirmed boundaries, unchanged by DB persistence:

* **Anonymous Vertex has no Credential owner in v1.** Its definitions
  persist with `credential_id=None` and carry only non-secret transport
  fields (`proxy_scheme`/`proxy_host`/`proxy_port` — no
  `proxy_username`/`proxy_password`, rejected at the DTO layer since
  DB-RESOURCE-001-1).
* **Proxy endpoints are persistable only when credential-free.** Userinfo
  and secret-bearing query parameters are rejected by
  `_validate_credential_free_proxy` for every provider that has a proxy
  field (`gemini_cli`, `firebase`). This holds at bootstrap import time
  because import goes through the same strict DTO validation.
* **Credential material never migrates into resource definitions.** The
  AUTH-010 direction was credential ← resource, never the reverse; a future
  Anonymous Vertex credential owner would be a `CredentialRepository`
  record referenced by `credential_id`, with the resource definition
  unchanged. Bootstrap and import code must not special-case tokens into
  the JSONB body — DB-RESOURCE-001-5's serialization audit tests
  (`test_persisted_body_contains_no_secret_or_runtime_fields`) are the
  guardrail and must keep passing.
* Runtime proxy/token state (rotation, expiry, health) still lives in
  runtime `Resource` / credential layers and never enters
  `resource_definitions`.

---

## 7. Import Command (direction only — not implemented here)

Decision: yes, an explicit import path is required. Two entry points, one
core:

* **Core:** a `resource bootstrap` service function in
  `core/` (DB-RESOURCE-003) implementing: load YAML → validate → diff
  against DB → insert/additive-import/overwrite per mode → report. All
  entry points call this one function; no logic lives in the CLI or API
  layer.
* **CLI (primary resolution tool):** `gemini-gateway resource import-yaml
  [--dry-run] [--overwrite]` — `--dry-run` prints the diff (add/update/conflict)
  without writing; `--overwrite` accepts YAML over DB for the listed
  conflicts and is the documented answer to a §4 startup failure.
* **Admin endpoint (secondary, later):** `POST /admin/resources/import-yaml`
  with the same modes, gated like other Admin mutations. Deferred until the
  bootstrap service exists and is CLI-tested.

Non-goals for the command: YAML export (may come later as
`resource export-yaml`), scheduled/automatic sync, partial imports (a
failed import writes nothing — one transaction per batch).

---

## 8. Failure Policy Summary

| Failure | Behavior |
|---|---|
| Schema `initialize()` error (driver/DB down) | Startup fails; original exception propagates; never an in-memory fallback (001-3 semantics) |
| Invalid YAML definition during import | Startup fails, listing every invalid entry; nothing partially imported (per-batch transaction) |
| Drift between YAML and DB (default mode) | Startup fails with key list + both payloads + resolution command |
| Unresolved `credential_id` | Warning, definition kept, resource built (current contract) |
| Duplicate composite key mid-import (race) | `DuplicateResourceDefinitionError` aborts the batch; next startup sees a non-empty table and takes the drift path |
| Cancellation mid-bootstrap | Transaction rolled back, connection closed (`_transaction` BaseException semantics, verified in 001-4-FIX) |

---

## 9. Future Implementation Boundary (for DB-RESOURCE-003)

In scope for 003:

1. `core/resource_bootstrap.py` — the service function of §7 (load,
   validate, diff, import) + startup ordering integration point.
2. Async bridge decision for the sync `create_app` path (§3 note).
3. `resource import-yaml --dry-run/--overwrite` CLI.
4. `build_runtime` reading definitions from the repository when the durable
   repository is configured (YAML path preserved for no-DB deployments).
5. Warning emission for unresolved `credential_id` during bootstrap.

Explicitly out of scope for 003 (and unchanged until separate tasks):

* `ResourceManager._persist()` retirement / Admin API write-path switch to
  DB (needs its own task + UI verification).
* `POST /admin/resources/import-yaml`.
* YAML export; scheduled sync.
* Credential-side changes of any kind (AUTH series is frozen).
* Schema changes (001-3 schema is final unless query evidence says
  otherwise).

---

## 10. Acceptance for this ADR (met by this document)

1. Decision + alternatives (§1), startup sequence (§3), migration lifecycle
   (§2), conflict & failure policy (§4, §8), implementation boundary (§9).
2. No code changed: `app/`, `core/resource_postgres.py`,
   `core/resource_repository.py`, providers, pool, scheduler untouched.
