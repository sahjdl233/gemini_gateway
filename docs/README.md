# docs index

Design records and ADRs.  Series prefixes: `DB-RESOURCE-*` (persistence
boundary), `CONFIG-*` (bootstrap / configuration policy), `CONTROL-*`
(lifecycle policy), plus task research / implementation notes.

## ADR / policy

| Document | Status | Subject |
|---|---|---|
| [DB-RESOURCE-DESIGN-001](DB-RESOURCE-DESIGN-001.md) | design baseline | ResourceDefinition persistence boundary (§2.3/§4/§6/§8 referenced by the repository) |
| [DB-RESOURCE-DESIGN-002](DB-RESOURCE-DESIGN-002.md) | design baseline | Bootstrap import/diff engine (mode semantics, failure policy) |
| [DB-RESOURCE-DESIGN-003](DB-RESOURCE-DESIGN-003.md) | accepted | ResourceDefinition persistence boundary freeze (repository/source/source-of-record split) |
| [CONTROL-007-RUNTIME-STATE-POLICY](CONTROL-007-RUNTIME-STATE-POLICY.md) | accepted | Runtime state lifecycle at the credential boundary (observability vs scheduling state) |
| [CONFIG-001-BOOTSTRAP-SOURCE-OF-TRUTH](CONFIG-001-BOOTSTRAP-SOURCE-OF-TRUTH.md) | accepted | YAML = seed, repository = source of truth, pool = projection; `overwrite` banned as a startup mode (one-shot migration command instead) |
| [CONFIG-R2B-MODEL-REGISTRY-INVALIDATION](CONFIG-R2B-MODEL-REGISTRY-INVALIDATION.md) | accepted | `ModelRegistry.invalidate()` contract: definition changes may invalidate the model index; runtime scheduling state never may |
| [ADR-CONFIG-R4-PERSISTENCE-BOUNDARY](ADR-CONFIG-R4-PERSISTENCE-BOUNDARY.md) | accepted | Definition (reference-only, no secrets/runtime state) vs Credential material vs discardable runtime state; lifecycle + DTO contract tests |

## Notes

* Task research / implementation records: `TASK-004-*.md` … `TASK-008-*.md`.
* `anonymous-vertex-protocol.md`: provider protocol notes.
