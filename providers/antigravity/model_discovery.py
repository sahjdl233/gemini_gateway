from __future__ import annotations

from typing import Any, Mapping

from core.model_registry import ModelInfo

from .client import AntigravityClient
from .resource import AntigravityResource


class ModelDiscovery:
    def __init__(self, client: AntigravityClient) -> None:
        self.client = client

    def fetch_models(self, resource: AntigravityResource) -> list[ModelInfo]:
        data = self.client.fetch_available_models(resource, {})
        raw_models = (data or {}).get("models", {})
        if not isinstance(raw_models, dict):
            return []
        models: list[ModelInfo] = []
        for model_id, meta in raw_models.items():
            if not isinstance(model_id, str):
                continue
            meta = meta or {}
            if not isinstance(meta, Mapping):
                continue
            models.append(
                ModelInfo(
                    id=model_id,
                    provider="antigravity",
                    capabilities={"stream": False, "tools": True},
                )
            )
        return models