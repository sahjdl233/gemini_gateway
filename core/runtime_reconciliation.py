"""Runtime reconciliation lifecycle service (DB-RESOURCE-008, Part A).

Turns a :class:`core.resource_definition_repository.ResourceDefinitionRepository`
(the definition source) into a :class:`core.runtime_snapshot.RuntimeSnapshot`
of runtime ``Resource`` instances:

    ResourceDefinitionRepository
        ->  list_definitions()
        ->  runtime_builder(definitions)        (injected callable)
        ->  Resource instances
        ->  RuntimeSnapshot

Boundary rules:

* The service knows the definition-source Protocol, the injected
  ``runtime_builder`` callable and Resource instances as opaque results.
  It does NOT know FastAPI, app.state, Scheduler, YAML or PostgreSQL.
* No caching: every :meth:`reconcile` re-reads the source and re-builds
  — the source of truth is the repository, never the last snapshot.
* Source/build errors propagate unchanged; reconciliation never swallows
  or downgrades a failure.
* ``runtime_builder`` is a synchronous callable mapping definitions to a
  ``{provider_id: [Resource, ...]}`` mapping — typically
  :func:`core.runtime_resource_factory.registry_runtime_builder`.  The
  read is async (repository Protocol), the build is CPU-bound
  conversion, so one event-loop-free async method suffices.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, List

from core.runtime_snapshot import RuntimeSnapshot, utcnow

__all__ = ["RuntimeReconciliationService"]

#: ``runtime_builder`` contract: definitions -> {provider_id: [Resource]}.
RuntimeBuilder = Callable[[List[Any]], Dict[str, List[Any]]]


class RuntimeReconciliationService:
    """Rebuild runtime resources from the definition source.

    CONFIG/R-5 Part E: the preferred source is the new persistence-layer
    :class:`core.repositories.resource_definition.ResourceDefinitionRepository`
    (read via ``list_all()``).  The older read-only sources
    (``list_definitions()`` — ResourceRepositoryDefinitionSource, the
    config-backed seed, etc.) remain supported untouched: the service
    reads whichever surface the injected repository exposes.  No other
    logic changed — every reconcile is a full read+build.
    """

    def __init__(
        self,
        definition_repository: Any,
        runtime_builder: RuntimeBuilder,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._definition_repository = definition_repository
        self._runtime_builder = runtime_builder
        self._clock = clock

    async def _read_definitions(self) -> List[Any]:
        """Read the definitions from the injected repository.

        Prefers the persistence-layer ``list_all()``; falls back to the
        legacy read-only ``list_definitions()`` protocol.
        """
        repository = self._definition_repository
        list_all = getattr(repository, "list_all", None)
        if list_all is not None:
            return await list_all()
        return await repository.list_definitions()

    async def reconcile(self) -> RuntimeSnapshot:
        """Read the source, build resources, return a fresh snapshot.

        Every call performs a full read+build: the service holds no
        snapshot cache and never merges with previous state.  Any error
        from the repository or the builder propagates unchanged.
        """
        definitions = await self._read_definitions()
        resources_by_provider = self._runtime_builder(definitions) or {}
        resources: List[Any] = []
        for provider_id in sorted(resources_by_provider):
            resources.extend(resources_by_provider[provider_id])
        return RuntimeSnapshot(
            resources=resources,
            generated_at=self._clock(),
            source_count=len(definitions),
            resources_by_provider=resources_by_provider,
        )
