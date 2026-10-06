"""Factory classes for Anonymous Vertex Provider (TASK-002, ANON-004).

The Application layer never constructs AnonymousVertexProvider or
AnonymousVertexResource directly. It registers this packages ProviderDefinition
with the ProviderRegistry, which creates both provider and resources on demand.
"""
from __future__ import annotations

from typing import Any, List, Optional

from core.provider_registry import ProviderFactory
from core.resource import Resource
from core.resource_factory import ResourceFactory
from providers.anonymous_vertex.admission import NodeAdmissionState  # noqa: F401
from providers.anonymous_vertex.node_definitions import NodeDefinition
from providers.anonymous_vertex.nodes import CooldownPolicy, NodeSpec
from providers.anonymous_vertex.provider import AnonymousVertexProvider
from providers.anonymous_vertex.resource import AnonymousVertexResource

#: source_id stamped onto definitions parsed from the static YAML config.
CONFIG_SOURCE_ID = "config"


class AnonymousVertexProviderFactory:

    """Creates a fresh AnonymousVertexProvider instance.

    The optional config may carry:

    * ``models`` — explicit model list overriding the static text models;
    * ``node_pool`` — execution-node configuration (ANON-004, admission
      wiring ANON-012)::

        node_pool:
          max_attempts: 3
          admission_interval_seconds: 300.0
          admission_max_concurrency: 5
          rate_limit_cooldown:      # seconds; 429 ladder (all optional)
            base: 30.0
            factor: 2.0
            max: 600.0
          nodes:
            - id: node-a
              weight: 1
              max_concurrency: 8
              proxy: {scheme: socks5, host: ..., port: ...}
            - id: node-b

      The parsed nodes become persistable NodeDefinition objects (the
      single construction source of record): the provider builds its
      ExecutionNodes AND its AdmissionScheduler from the SAME definitions,
      so an explicitly configured node pool always runs admission-aware.

      Absent node_pool => the LEGACY default-direct path (single direct
      node, no admission projection): the config.yaml.example default
      "just works" behaviour must never be killed by admission gating.
      Admission-aware operation requires an explicitly configured node
      pool with real endpoints.
    """

    def create_provider(
        self, provider_id: str, config: Any = None
    ) -> AnonymousVertexProvider:
        models = None
        node_definitions: Optional[List[NodeDefinition]] = None
        policy: Optional[CooldownPolicy] = None
        max_attempts = None
        admission_interval = None
        admission_max_concurrency = None
        if config:
            models = config.get("models")
            np_cfg = config.get("node_pool") or {}
            entries = list(np_cfg.get("nodes") or [])
            if entries:
                node_definitions = [
                    NodeDefinition(
                        node_id=str(entry.get("id") or f"node-{i + 1}"),
                        proxy=dict(entry.get("proxy") or {}),
                        source_id=CONFIG_SOURCE_ID,
                        enabled=bool(entry.get("enabled", True)),
                        weight=int(entry.get("weight", 1)),
                        max_concurrency=int(entry.get("max_concurrency", 8)),
                    )
                    for i, entry in enumerate(entries)
                ]
            # no explicit nodes -> stay on the legacy default-direct path
            # (node_definitions stays None; the provider builds a plain
            # direct node WITHOUT an admission projection)
            rl = np_cfg.get("rate_limit_cooldown") or {}
            if rl:
                policy = CooldownPolicy(
                    rate_limit_base=float(rl.get("base", 30.0)),
                    rate_limit_factor=float(rl.get("factor", 2.0)),
                    rate_limit_max=float(rl.get("max", 600.0)),
                )
            max_attempts = np_cfg.get("max_attempts")
            if np_cfg.get("admission_interval_seconds") is not None:
                admission_interval = float(np_cfg["admission_interval_seconds"])
            if np_cfg.get("admission_max_concurrency") is not None:
                admission_max_concurrency = int(
                    np_cfg["admission_max_concurrency"]
                )
        kwargs: dict = {
            "models": models,
            "node_definitions": node_definitions,
            "cooldown_policy": policy,
        }
        if max_attempts is not None:
            kwargs["max_node_attempts"] = int(max_attempts)
        if admission_interval is not None:
            kwargs["admission_interval_seconds"] = admission_interval
        if admission_max_concurrency is not None:
            kwargs["admission_max_concurrency"] = admission_max_concurrency
        return AnonymousVertexProvider(**kwargs)


class AnonymousVertexResourceFactory:
    """Builds AnonymousVertexResource objects from config dicts."""

    def create_resources(
        self, provider_id: str, config: List[dict]
    ) -> List[AnonymousVertexResource]:
        resources = []
        for item in config:
            payload = dict(item)
            payload.setdefault("provider", provider_id)
            resources.append(AnonymousVertexResource.model_validate(payload))
        return resources
