"""YAML/JSON config loader with secret env substitution.

Real credentials must NEVER be committed: resources reference secrets
via ENV_VAR placeholders resolved from the environment at load time.
If an env var is missing, the placeholder is replaced with an empty
string (values are never written into logs).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

import yaml

# Dollar-brace markers built without literal '$'/'{' so the module stays
# safe inside tooling that treats these characters specially.
_MARK_BEGIN = chr(36) + chr(123)  # "$" "{"
_MARK_END = chr(125)  # "}"


def _substitute(text: str) -> str:
    while True:
        start = text.find(_MARK_BEGIN)
        if start == -1:
            return text
        end = text.find(_MARK_END, start + 2)
        if end == -1:
            return text
        name = text[start + 2:end]
        text = text[:start] + os.environ.get(name, "") + text[end + 1:]


def _resolve(node: Any) -> Any:
    if isinstance(node, dict):
        return {key: _resolve(val) for key, val in node.items()}
    if isinstance(node, list):
        return [_resolve(item) for item in node]
    if isinstance(node, str):
        return _substitute(node)
    return node


def load_config(
    path: Path | str = Path("config.yaml"),
    *,
    resolve_env: bool = True,
) -> Dict[str, Any]:
    """Load config.yaml (or a JSON file) into a plain dict.

    Section 14 of TASK-000: the first version uses files, not a database.
    """
    path = Path(path)
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(raw)
    else:
        data = yaml.safe_load(raw) or {}
    if resolve_env:
        data = _resolve(data)
    return data


def default_config() -> Dict[str, Any]:
    """Built-in fake-provider config used when no config.yaml exists."""
    return {
        "server": {"host": "127.0.0.1", "port": 8000},
        "scheduler": {
            "max_retries": 2,
            "cooldown": {
                "base_delay": 0.2,
                "factor": 2.0,
                "max_delay": 10.0,
                "jitter": 0.1,
            },
        },
        "providers": {
            "fake": {
                "enabled": True,
                "resources": [
                    {"id": "fake-01", "provider": "fake", "scenario": "success"},
                    {"id": "fake-02", "provider": "fake", "scenario": "rate_limit", "retry_after": 1.0},
                ],
            }
        },
    }
