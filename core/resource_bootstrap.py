"""Resource bootstrap service core (DB-RESOURCE-003-1).

Implements the provider-agnostic import/diff engine specified by
``docs/DB-RESOURCE-DESIGN-002.md`` §2 (empty-table import), §4 (drift
comparison), §7 (import command core) and §8 (failure policy).

Deliberately decoupled from YAML: the service consumes an already-parsed
list of :class:`core.resource_definition.ResourceDefinitionBase` DTOs.
The YAML → DTO adapter is DB-RESOURCE-003-2; startup wiring and the CLI
are later tasks.  This module never imports yaml, config loaders, or
anything from ``app/``.

Comparison semantics (DESIGN-002 §3):

* Canonical payload = the persisted shape — ``provider``, ``resource_id``,
  ``enabled``, ``credential_id`` and the provider-specific
  ``to_definition_json()`` body.  Never a DTO repr, never YAML text.
* Canonical payloads are plain dicts, so JSON object key order cannot
  influence equality.
* Field order in the payload dict is fixed by construction, making the
  canonical form stable for logging and hashing.

Mode semantics (DESIGN-002 §4):

* ``CHECK``     — compare only; nothing is written.
* ``IMPORT``    — insert missing; identical is a no-op; differing keys are
                  reported as conflicts and never overwritten.
* ``OVERWRITE`` — insert missing; identical is a no-op; differing keys are
                  updated (explicit operator decision).
* DELETE is out of scope by design: YAML missing a DB resource yields a
  ``db_only`` record and the row is kept — YAML is a seed, not a sync
  source.

Failure policy (DESIGN-002 §8):

* Fail-closed plan stage: duplicate ``(provider, resource_id)`` identities
  or non-DTO entries are rejected before any write.  This is an input-plan
  error — it must not be left for the repository's duplicate constraint to
  discover mid-execution.
* The :class:`core.resource_repository.ResourceRepository` contract
  currently provides only per-operation atomicity (each add/update is one
  transaction).  The service therefore validates fully, computes the
  complete plan, then executes writes in deterministic
  ``(provider, resource_id)`` order — and says so in every result's
  ``notes``.  It does NOT pretend to offer an atomic batch transaction.
* Repository errors are never swallowed or translated:
  ``DuplicateResourceDefinitionError`` / ``UnknownResourceDefinitionError``
  raised mid-execution (e.g. a row inserted after the plan was computed)
  propagate unchanged with their original semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.resource_definition import ResourceDefinitionBase
from core.resource_definition_repository import (
    ResourceDefinitionRepository,
    ResourceRepositoryDefinitionSource,
)
from core.resource_repository import ResourceRepository

__all__ = [
    "BootstrapMode",
    "BootstrapRecord",
    "BootstrapConflict",
    "BootstrapResult",
    "ResourceBootstrapError",
    "ResourceBootstrapConflictError",
    "ResourceBootstrapService",
    "canonical_payload",
    "canonical_diff",
]


class BootstrapMode(str, Enum):
    """Bootstrap strategy (DESIGN-002 §4)."""

    CHECK = "check"
    IMPORT = "import"
    OVERWRITE = "overwrite"


class ResourceBootstrapError(Exception):
    """Invalid bootstrap input, rejected before any repository write.

    Raised at plan stage — e.g. duplicate ``(provider, resource_id)``
    identities inside the incoming definitions, or an entry that is not a
    :class:`ResourceDefinitionBase`.  This is an input-plan error, not a
    database race, and must never surface as a repository exception.
    """


class ResourceBootstrapConflictError(Exception):
    """Raised on demand for result conflicts (CLI/report helper).

    The normal :meth:`ResourceBootstrapService.run` flow reports conflicts
    structurally on :class:`BootstrapResult` instead of raising — 003-2's
    CLI decides what a conflict means for its exit code.  Call
    :meth:`BootstrapResult.raise_if_conflicts` to turn recorded conflicts
    into this exception.
    """

    def __init__(self, conflicts: Sequence["BootstrapConflict"]) -> None:
        self.conflicts = list(conflicts)
        keys = ", ".join(
            f"({c.provider}, {c.resource_id})" for c in self.conflicts
        )
        super().__init__(
            f"bootstrap conflict for {len(self.conflicts)} definition(s): "
            f"{keys}"
        )


def canonical_payload(definition: ResourceDefinitionBase) -> Dict[str, Any]:
    """Canonical comparison form of a definition.

    Mirrors the persisted column semantics exactly: identity columns plus
    the provider-specific ``to_definition_json()`` body.  Stable for the
    same DTO; JSON object key order inside the body cannot affect
    comparison because payloads are compared as dicts.
    """
    return {
        "provider": definition.provider,
        "resource_id": definition.id,
        "enabled": definition.enabled,
        "credential_id": definition.credential_id,
        "definition": definition.to_definition_json(),
    }


#: Field-name markers whose values must never appear in logs or diff
#: reports (CONFIG-001 P2 / TASK-CONFIG-002 Part B).  Matched as
#: case-insensitive substrings at any payload depth, so future
#: secret-shaped fields stay covered by default.  Definitions are not
#: supposed to carry secrets at all (the strict DTO boundary rejects
#: them) — this is defence in depth for reporting, not a validation
#: change.
_SECRET_FIELD_MARKERS = ("secret", "token", "password", "api_key")

_REDACTED = "<redacted>"


def _is_secret_field(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _SECRET_FIELD_MARKERS)


def canonical_diff(
    existing: Dict[str, Any],
    incoming: Dict[str, Any],
    *,
    _prefix: str = "",
) -> Dict[str, Dict[str, Any]]:
    """Secret-free per-field diff between two canonical payloads.

    Returns ``{field_path: {"existing": ..., "incoming": ...}}`` for
    every differing leaf, nested dict bodies flattened to dotted paths
    (e.g. ``definition.project_id``).  Values under secret-shaped field
    names are replaced with ``<redacted>`` — the diff is meant for logs
    and operator reports.  ``credential_id`` is NOT secret (it is the
    loose reference, not material) and is shown as-is.
    """
    diff: Dict[str, Dict[str, Any]] = {}
    for key in sorted(set(existing) | set(incoming)):
        path = f"{_prefix}{key}"
        old = existing.get(key)
        new = incoming.get(key)
        if _is_secret_field(key):
            if old != new:
                diff[path] = {"existing": _REDACTED, "incoming": _REDACTED}
            continue
        if isinstance(old, dict) and isinstance(new, dict):
            diff.update(canonical_diff(old, new, _prefix=f"{path}."))
        elif old != new:
            diff[path] = {"existing": old, "incoming": new}
    return diff


@dataclass(frozen=True)
class BootstrapRecord:
    """One non-conflicting classification outcome, with its canonical
    payload (useful for dry-run reports)."""

    provider: str
    resource_id: str
    payload: Dict[str, Any] = field(compare=False)

    @property
    def key(self) -> Tuple[str, str]:
        return (self.provider, self.resource_id)


@dataclass(frozen=True)
class BootstrapConflict:
    """A differing definition for a key that exists on both sides."""

    provider: str
    resource_id: str
    existing: Dict[str, Any]
    incoming: Dict[str, Any]

    @property
    def key(self) -> Tuple[str, str]:
        return (self.provider, self.resource_id)

    def diff(self) -> Dict[str, Dict[str, Any]]:
        """Secret-free per-field diff (existing vs incoming canonical
        payloads) — the reportable form of a conflict
        (docs/CONFIG-001-BOOTSTRAP-SOURCE-OF-TRUTH.md §4)."""
        return canonical_diff(self.existing, self.incoming)


@dataclass
class BootstrapResult:
    """Structured outcome of one bootstrap run (DESIGN-002 §6)."""

    mode: BootstrapMode
    added: List[BootstrapRecord] = field(default_factory=list)
    unchanged: List[BootstrapRecord] = field(default_factory=list)
    conflicts: List[BootstrapConflict] = field(default_factory=list)
    db_only: List[BootstrapRecord] = field(default_factory=list)
    #: ``notes`` carries lifecycle facts every consumer should see — in
    #: particular that the repository contract provides only
    #: per-operation atomicity, so an execution failure mid-plan can leave
    #: earlier writes committed (fail-closed planning limits this to
    #: well-formed plans; it cannot make the batch atomic).
    notes: List[str] = field(default_factory=list)

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicts)

    def raise_if_conflicts(self) -> None:
        if self.conflicts:
            raise ResourceBootstrapConflictError(self.conflicts)


class ResourceBootstrapService:
    """Diff-and-import engine over a
    :class:`core.resource_definition_repository.ResourceDefinitionRepository`.

    Dependencies are injected by role (DB-RESOURCE-005):

    * ``repository`` — the definition **source** the service diffs
      against, via the read-only 004 Protocol.  The service knows YAML,
      in-memory seeds and PostgreSQL only as interchangeable sources
      behind this abstraction; use
      :class:`~core.resource_definition_repository.ResourceRepositoryDefinitionSource`
      to serve definitions from a durable CRUD repository, or
      ``MemoryResourceDefinitionRepository`` /
      ``ConfigResourceDefinitionRepository`` for seeds.
    * ``sink`` — the **write target** for IMPORT/OVERWRITE, typed as the
      DB-RESOURCE-001-2 CRUD ``ResourceRepository`` (add/update with
      per-operation transactions).  CHECK mode never writes and may omit
      it; a write mode without a sink is a plan-stage error.

    The service opens no connections itself; per-operation transactions
    are the sink's.
    """

    def __init__(
        self,
        repository: ResourceDefinitionRepository,
        *,
        sink: Optional[ResourceRepository] = None,
    ) -> None:
        self._repository = repository
        self._sink = sink

    @classmethod
    def over_repository(
        cls, repository: ResourceRepository
    ) -> "ResourceBootstrapService":
        """Build a service that diffs against and writes to the same
        durable CRUD repository (the common single-store deployment)."""
        return cls(
            ResourceRepositoryDefinitionSource(repository),
            sink=repository,
        )

    # -- plan ----------------------------------------------------------------

    def _validate_incoming(
        self, definitions: Sequence[ResourceDefinitionBase]
    ) -> Dict[Tuple[str, str], ResourceDefinitionBase]:
        """Reject malformed input before anything else happens.

        Returns the incoming definitions keyed by composite identity.
        """
        incoming: Dict[Tuple[str, str], ResourceDefinitionBase] = {}
        duplicates: List[Tuple[str, str]] = []
        for definition in definitions:
            if not isinstance(definition, ResourceDefinitionBase):
                raise ResourceBootstrapError(
                    "bootstrap input must be ResourceDefinitionBase "
                    f"instances, got {type(definition).__name__}"
                )
            key = (definition.provider, definition.id)
            if key in incoming:
                duplicates.append(key)
            else:
                incoming[key] = definition
        if duplicates:
            listed = ", ".join(f"({p}, {r})" for p, r in sorted(duplicates))
            raise ResourceBootstrapError(
                "duplicate (provider, resource_id) in bootstrap input: "
                f"{listed}"
            )
        return incoming

    async def _classify(
        self, definitions: Sequence[ResourceDefinitionBase]
    ) -> Tuple[
        Dict[Tuple[str, str], ResourceDefinitionBase],
        Dict[Tuple[str, str], Dict[str, Any]],
        List[BootstrapRecord],
        List[BootstrapRecord],
        List[BootstrapConflict],
        List[BootstrapRecord],
    ]:
        """Validate input, read the repository and classify every key.

        Returns ``(incoming, db_payloads, added, unchanged, conflicts,
        db_only)``, every list sorted by composite key.
        """
        incoming = self._validate_incoming(definitions)
        # Source side: read through the injected definition repository,
        # then canonicalize.  Its rows are authoritative for "what is
        # stored right now" — wherever they came from.
        db_definitions = await self._repository.list_definitions()
        db_payloads: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for stored in db_definitions:
            db_payloads[(stored.provider, stored.id)] = canonical_payload(
                stored
            )

        added: List[BootstrapRecord] = []
        unchanged: List[BootstrapRecord] = []
        conflicts: List[BootstrapConflict] = []
        for key, definition in incoming.items():
            payload = canonical_payload(definition)
            if key not in db_payloads:
                added.append(
                    BootstrapRecord(
                        provider=key[0],
                        resource_id=key[1],
                        payload=payload,
                    )
                )
            elif db_payloads[key] == payload:
                unchanged.append(
                    BootstrapRecord(
                        provider=key[0],
                        resource_id=key[1],
                        payload=payload,
                    )
                )
            else:
                conflicts.append(
                    BootstrapConflict(
                        provider=key[0],
                        resource_id=key[1],
                        existing=db_payloads[key],
                        incoming=payload,
                    )
                )

        db_only = [
            BootstrapRecord(
                provider=key[0],
                resource_id=key[1],
                payload=payload,
            )
            for key, payload in db_payloads.items()
            if key not in incoming
        ]

        sort_key = lambda record: (record.provider, record.resource_id)
        added.sort(key=sort_key)
        unchanged.sort(key=sort_key)
        conflicts.sort(key=lambda c: (c.provider, c.resource_id))
        db_only.sort(key=sort_key)
        return incoming, db_payloads, added, unchanged, conflicts, db_only

    # -- run -----------------------------------------------------------------

    async def run(
        self,
        definitions: Sequence[ResourceDefinitionBase],
        mode: BootstrapMode,
    ) -> BootstrapResult:
        """Classify the incoming definitions against the repository and,
        for non-CHECK modes, execute the allowed writes.

        Write order is deterministic — ascending ``(provider,
        resource_id)`` within ``added`` then, for OVERWRITE, within the
        conflicting updates.  Every repository error propagates
        unchanged.
        """
        if not isinstance(mode, BootstrapMode):
            raise ResourceBootstrapError(
                f"bootstrap mode must be a BootstrapMode, got {mode!r}"
            )
        (
            incoming,
            _db_payloads,
            added,
            unchanged,
            conflicts,
            db_only,
        ) = await self._classify(definitions)

        result = BootstrapResult(
            mode=mode,
            added=added,
            unchanged=unchanged,
            conflicts=conflicts,
            db_only=db_only,
            notes=[
                "repository contract provides per-operation atomicity "
                "only; the bootstrap plan is validated before any write, "
                "but the batch is not a single transaction"
            ],
        )
        if mode is BootstrapMode.CHECK:
            return result

        if self._sink is None:
            raise ResourceBootstrapError(
                f"mode {mode.value!r} writes to the durable repository, "
                "but no sink was provided — pass sink= (a CRUD "
                "ResourceRepository) or use CHECK for plan-only runs"
            )

        # IMPORT / OVERWRITE: additive inserts for missing keys.
        for record in added:
            await self._sink.add(incoming[record.key])

        if mode is BootstrapMode.IMPORT:
            # Conflicts are reported, never overwritten (DESIGN-002 §4).
            return result

        # OVERWRITE: explicit operator decision to accept incoming over DB.
        for conflict in conflicts:
            await self._sink.update(incoming[conflict.key])
        return result
