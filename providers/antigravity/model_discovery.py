from __future__ import annotations

from typing import Any, Mapping

from core.model_registry import ModelInfo

from .client import AntigravityClient


class ModelDiscovery:
    def __init__(self, client: AntigravityClient) -> None:
        self.client = client

    def fetch_models(self) -> list[ModelInfo]:
        data = self.client.fetch_available_models({})
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
                    name=str(meta.get("displayName") or meta.get("model") or model_id),
                    description="",
                    context_window=0,
                    temperature=0.0,
                    top_p=0.0,
                    top_k=0,
                    is_chat=False,
                    is_streaming=False,
                )
            )
        return models
