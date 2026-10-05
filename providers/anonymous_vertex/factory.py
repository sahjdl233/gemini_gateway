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
from providers.anonymous_vertex.nodes import CooldownPolicy, NodeSpec
from providers.anonymous_vertex.provider import AnonymousVertexProvider
from providers.anonymous_vertex.resource import AnonymousVertexResource


class AnonymousVertexProviderFactory:

    """Creates a fresh AnonymousVertexProvider instance.

    The optional config may carry:

    * ``models`` — explicit model list overriding the static text models;
    * ``node_pool`` — execution-node configuration (ANON-004)::

        node_pool:
          max_attempts: 3
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

      Absent node_pool => a single direct-connection default node, so
      existing single-transport deployments behave exactly as before
      (minus the shared transport: even the default is a pool node).
    """

    def create_provider(
        self, provider_id: str, config: Any = None
    ) -> AnonymousVertexProvider:
        models = None
        node_specs: Optional[List[NodeSpec]] = None
        policy: Optional[CooldownPolicy] = None
        max_attempts = None
        if config:
            models = config.get("models")
            np_cfg = config.get("node_pool") or {}
            if np_cfg:
                node_specs = [
                    NodeSpec(
                        node_id=str(entry.get("id") or f"node-{i + 1}"),
                        proxy=dict(entry.get("proxy") or {}),
                        weight=int(entry.get("weight", 1)),
                        max_concurrency=int(entry.get("max_concurrency", 8)),
                        enabled=bool(entry.get("enabled", True)),
                    )
                    for i, entry in enumerate(np_cfg.get("nodes") or [])
                ]
                rl = np_cfg.get("rate_limit_cooldown") or {}
                if rl:
                    policy = CooldownPolicy(
                        rate_limit_base=float(rl.get("base", 30.0)),
                        rate_limit_factor=float(rl.get("factor", 2.0)),
                        rate_limit_max=float(rl.get("max", 600.0)),
                    )
                max_attempts = np_cfg.get("max_attempts")
        kwargs: dict = {"models": models, "node_specs": node_specs,
                        "cooldown_policy": policy}
        if max_attempts is not None:
            kwargs["max_node_attempts"] = int(max_attempts)
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
