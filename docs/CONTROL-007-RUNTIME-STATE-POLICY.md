# CONTROL-007-RUNTIME-STATE-POLICY — Runtime State Lifecycle at the Credential Boundary (ADR)

Status: accepted (CONTROL-007-DECISION-001)
Scope baseline: TASK-STATE-001 (state preservation contract), CONTROL-006/007
(lifecycle audit chain)
Decided: 2026-10-04

---

## 0. Problem

`InMemoryPool.reconcile_resources` preserves a Resource's runtime state by
`ResourceKey = (provider, id)` when the definition is replaced
(TASK-STATE-001).  The key deliberately does NOT include `credential_id`, so
a credential rebind (PATCH `credential_id` A→B) inherited the full captured
state — including `health`, `cooldown_until` and `consecutive_failures`.

CONTROL-007 (lifecycle audit) flagged this as an undecided policy point:
rate limits, bans and degraded upstream identity are properties of the
**upstream account (the credential)**, not of the resource id.  Under the
inherit rule, a rebind performed precisely to escape a rate-limited or
burned credential made the NEW credential serve the OLD credential's
penalty: an immediately blocked resource (inherited `cooldown_until`), a
longer first backoff (inherited `consecutive_failures` feeds the
exponential delay, `core/cooldown.py:_delay_seconds`), and a misleading
DEGRADED health display.

The auth-adapter layer is not affected by this decision: adapter OAuth
state (rotated refresh token, access token) is already invalidated on any
definition change since CONTROL-006-FIX-1, and rotated refresh tokens are
durably persisted to the credential store (AUTH-014) — dropping runtime
state never drops valid refresh material.

## 1. Decision

Runtime state splits into two classes with different carriers:

| State | Class | Carrier | On credential rebind | On any other definition change | On delete → recreate | On restart |
|---|---|---|---|---|---|---|
| `total_requests` | observability | ResourceKey | **preserved** | preserved | lost | lost |
| `total_failures` | observability | ResourceKey | **preserved** | preserved | lost | lost |
| `health` | scheduling | credential identity | **reset to HEALTHY** | preserved | lost | lost |
| `cooldown_until` | scheduling | credential identity | **reset to None** | preserved | lost | lost |
| `consecutive_failures` | scheduling | credential identity | **reset to 0** | preserved | lost | lost |
| `in_flight` | live request count | — | never inherited (blocks reconcile while > 0) | same | same | same |
| `last_error` | — | — | does not exist as persistent state (transient `HealthResult.message` only) | | | |

Rules:

1. **Observability counters follow the resource id.**  They describe
   traffic routed through `(provider, id)` and are never reset by
   configuration changes.  They participate in no scheduling decision.
2. **Scheduling state follows the credential identity.**  When a
   reconcile replaces a resource whose `credential_id` changed, `health`,
   `cooldown_until` and `consecutive_failures` reset to fresh defaults.
   The new credential starts with a clean slate; the old credential's
   penalty dies with it.
3. **All other definition changes keep TASK-STATE-001 semantics
   unchanged** — full scheduling-state preservation by key (disable →
   enable, project_id edits, enabled flips, provider-specific fields).
4. **Delete → recreate the same id inherits nothing** (the state dies with
   the removed pool object — unchanged behaviour, now contract-tested).
5. **Restart loses all runtime state** (unchanged — state is memory-only
   by design).
6. The **legacy path applies the same rule**: an in-place `credential_id`
   change in `_legacy_update` resets the scheduling state on the spot
   (app/management.py).

## 2. Implementation surface

* `core/pool.py` — `_RuntimeState.apply_to(..., credential_changed=)`:
  the only place captured state is transferred; `reconcile_resources`
  computes `credential_changed` from old vs new `credential_id` on the
  same key.  `_RuntimeState` and `reconcile_resources` docstrings record
  the boundary.
* `app/management.py` — `_legacy_update` resets the scheduling state on a
  legacy in-place credential rebind (the repository path needs no extra
  code: its rebind always flows through `reconcile_resources`).

Deliberately NOT touched: the pool reconciliation algorithm's atomicity or
in-flight rules, the scheduler, the repository contract, the cooldown
manager.

## 3. Consequences

* Rebinding to escape a rate-limited credential works immediately: the
  resource is eligible again right after the PATCH (assuming the new
  credential resolves).
* Cross-credential observability stays intact: lifetime traffic counters
  per resource id are uninterrupted by rebinds.
* A resource id that oscillates between credentials in a tight loop would
  lose its backoff each time — accepted: backoff abuse via rebind is an
  operator action (Admin writes are serialized and auditable), not a
  scheduler-observable event.
* The reset applies on ANY credential_id transition, including A → None
  (unbind to legacy fields) and None → A (first binding).  Both are
  identity changes in the same sense.

## 4. Test contract

`tests/core/test_pool_reconcile.py`:

* credential rebind resets `health`/`cooldown_until`/`consecutive_failures`
  and still preserves `total_requests`/`total_failures`;
* a definition change with the SAME credential preserves the full state
  (the reset trigger is exactly the credential transition);
* delete → recreate the same id yields fresh defaults.

`tests/app/test_resource_write_path_repository.py`:

* repository-path PATCH `credential_id` → live pool resource is HEALTHY
  with counters preserved (integration through the manager).

The legacy-path rule is covered by the pool unit contract plus the
management-level rebind tests from CONTROL-006-FIX-1 (adapter
invalidation), which share the same trigger condition.
