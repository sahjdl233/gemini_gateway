"""loadCodeAssist / onboardUser / LRO polling (TASK-008)."""
from __future__ import annotations

from typing import Any, Dict

from providers.gemini_cli.onboard import (
    _map_raw_tier,
    _extract_tier,
    _extract_credits,
    parse_load_code_assist,
    parse_operation,
    metadata_for,
    inspect_operation,
)
from providers.gemini_cli.resource import GeminiCliResource


def test_map_raw_tier():
    assert _map_raw_tier("g1-ultra-tier") == "ULTRA"
    assert _map_raw_tier("ws-ai-ultra-business-tier") == "ULTRA"
    assert _map_raw_tier("g1-pro-tier") == "PRO"
    assert _map_raw_tier("helium-tier") == "PRO"
    assert _map_raw_tier("standard-tier") == "PRO"
    assert _map_raw_tier("free-tier") == "FREE"
    assert _map_raw_tier("unknown-tier") == "PRO"
    assert _map_raw_tier("") == "PRO"


def test_extract_tier_prefers_paid_tier():
    data = {
        "paidTier": {"id": "g1-pro-tier"},
        "currentTier": {"id": "free-tier"},
    }
    assert _extract_tier(data) == "PRO"


def test_extract_tier_fallback_current():
    data = {"currentTier": {"id": "free-tier"}}
    assert _extract_tier(data) == "FREE"


def test_extract_credits_from_paid_tier():
    data = {
        "paidTier": {
            "availableCredits": [{"creditAmount": 100.5}],
        }
    }
    assert _extract_credits(data) == 100.5


def test_parse_load_code_assist_existing_project():
    data = {
        "cloudaicompanionProject": {"id": "gen-lang-client-123"},
        "currentTier": {"id": "g1-pro-tier"},
        "paidTier": {"id": "g1-pro-tier", "availableCredits": [{"creditAmount": 50}]},
    }
    result = parse_load_code_assist(data)
    assert result["project_id"] == "gen-lang-client-123"
    assert result["tier"] == "PRO"
    assert result["credits"] == 50
    assert result["needs_onboarding"] is False


def test_parse_load_code_assist_new_account():
    data = {
        "allowedTiers": [
            {"id": "free-tier", "isDefault": True},
            {"id": "g1-pro-tier", "isDefault": False},
        ]
    }
    result = parse_load_code_assist(data)
    assert result["project_id"] is None
    assert result["needs_onboarding"] is True
    assert result["default_tier_id"] == "free-tier"


def test_parse_operation_done_true():
    data = {"done": True, "response": {"cloudaicompanionProject": {"id": "gen-lang-999"}}}
    done, pid = parse_operation(data)
    assert done is True
    assert pid == "gen-lang-999"


def test_parse_operation_not_done():
    data = {"done": False}
    done, pid = parse_operation(data)
    assert done is False
    assert pid is None


def test_metadata_for():
    resource = GeminiCliResource(
        id="test-resource",
        ide_type="ANTIGRAVITY",
        platform="PLATFORM_UNSPECIFIED",
        plugin_type="GEMINI",
    )
    md = metadata_for(resource)
    assert md["ideType"] == "ANTIGRAVITY"
    assert md["platform"] == "PLATFORM_UNSPECIFIED"
    assert md["pluginType"] == "GEMINI"


def test_inspect_operation_url():
    path = inspect_operation(
        "https://cloudcode-pa.googleapis.com/v1internal/operations/abc123"
    )
    assert path == "v1internal/operations/abc123"


def test_inspect_operation_bare():
    path = inspect_operation("operations/abc123")
    assert path == "operations/abc123"
