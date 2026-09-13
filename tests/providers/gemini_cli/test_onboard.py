"""Tests for onboarding functions (TASK-007 / onboard.py)."""
from __future__ import annotations

import pytest

from providers.gemini_cli.onboard import (
    discover_project,
    inspect_operation,
    load_code_assist,
    metadata_for,
    onboard_user,
    parse_load_code_assist,
    parse_operation,
    poll_operation,
)
from tests.providers._gemini_cli_fakes import FakeHttp, make_resource


# ---------------------------------------------------------------------------
# parse_load_code_assist
# ---------------------------------------------------------------------------

def test_parse_load_code_assist_existing_project():
    data = {
        "cloudaicompanionProject": {"id": "proj-123"},
        "paidTier": {"id": "g1-pro-tier", "availableCredits": [{"creditAmount": 100.0}]},
    }
    result = parse_load_code_assist(data)
    assert result["project_id"] == "proj-123"
    assert result["tier"] == "PRO"
    assert result["credits"] == 100.0
    assert result["needs_onboarding"] is False


def test_parse_load_code_assist_new_account_needs_onboarding():
    data = {
        "allowedTiers": [
            {"id": "g1-pro-tier", "isDefault": True},
            {"id": "free-tier"},
        ],
    }
    result = parse_load_code_assist(data)
    assert result["project_id"] is None
    assert result["needs_onboarding"] is True
    assert result["default_tier_id"] == "g1-pro-tier"


def test_parse_load_code_assist_ultra_tier():
    data = {
        "cloudaicompanionProject": {"id": "proj-ultra"},
        "paidTier": {"id": "ws-ai-ultra-business-tier"},
    }
    result = parse_load_code_assist(data)
    assert result["tier"] == "ULTRA"
    assert result["needs_onboarding"] is False


# ---------------------------------------------------------------------------
# parse_operation
# ---------------------------------------------------------------------------

def test_parse_operation_done():
    data = {
        "done": True,
        "response": {"cloudaicompanionProject": {"id": "proj-456"}},
    }
    done, project_id = parse_operation(data)
    assert done is True
    assert project_id == "proj-456"


def test_parse_operation_in_progress():
    data = {"done": False}
    done, project_id = parse_operation(data)
    assert done is False
    assert project_id is None


def test_parse_operation_invalid_data():
    done, project_id = parse_operation("not a dict")
    assert done is False
    assert project_id is None


# ---------------------------------------------------------------------------
# inspect_operation
# ---------------------------------------------------------------------------

def test_inspect_operation_raw_name():
    assert inspect_operation("operations/abc123") == "operations/abc123"


def test_inspect_operation_full_url():
    url = "https://cloudcode-pa.googleapis.com/v1internal:operations/abc123"
    assert inspect_operation(url) == "v1internal:operations/abc123"


def test_inspect_operation_empty():
    assert inspect_operation("") is None


# ---------------------------------------------------------------------------
# metadata_for
# ---------------------------------------------------------------------------

def test_metadata_for():
    resource = make_resource()
    meta = metadata_for(resource)
    assert meta["ideType"] == "GCLI"
    assert meta["pluginType"] == "GEMINI"
    assert meta["platform"] == "PLATFORM_UNSPECIFIED"


# ---------------------------------------------------------------------------
# TASK-009: end-to-end discovery / onboarding / LRO polling flows
# ---------------------------------------------------------------------------


class _AuthFakeClock:
    def __init__(self, start=1000.0):
        self.value = start

    def time(self):
        return self.value


def _client_for(http):
    from providers.gemini_cli.auth import GeminiCliAuth
    from providers.gemini_cli.client import GeminiCliClient

    auth = GeminiCliAuth(http, clock=_AuthFakeClock())
    return GeminiCliClient(http=http, auth=auth)


async def test_discover_existing_project_skips_onboarding():
    """Existing cloudaicompanionProject -> loadCodeAssist only, no onboardUser."""
    http = FakeHttp()
    # loadCodeAssist: token refresh + API response
    http.responses.append(http.token_ok("token-1", 3600))
    http.responses.append(
        http.ok(
            {
                "cloudaicompanionProject": {"id": "existing-proj"},
                "paidTier": {"id": "g1-pro-tier"},
            }
        )
    )
    client = _client_for(http)
    resource = make_resource(project_id=None)

    project_id = await discover_project(client, resource)

    assert project_id == "existing-proj"
    assert resource.tier == "PRO"
    # 1 token + 1 loadCodeAssist = 2 post calls
    assert len(http.post_calls) == 2
    assert "loadCodeAssist" in http.post_calls[-1]["url"]
    assert not any("onboardUser" in c["url"] for c in http.post_calls)


async def test_load_code_assist_returns_parsed():
    http = FakeHttp()
    http.responses.append(http.token_ok("token-1", 3600))
    http.responses.append(
        http.ok(
            {
                "cloudaicompanionProject": {"id": "proj-9"},
                "paidTier": {"id": "free-tier"},
            }
        )
    )
    client = _client_for(http)
    result = await load_code_assist(client, make_resource())
    assert result["project_id"] == "proj-9"
    assert result["tier"] == "FREE"
    assert result["needs_onboarding"] is False


async def test_onboard_full_flow_single_poll():
    """No project -> loadCodeAssist -> onboardUser -> poll done once -> project id."""
    http = FakeHttp()
    # loadCodeAssist: token refresh + API
    http.responses.append(http.token_ok("token-1", 3600))
    http.responses.append(http.ok({"allowedTiers": [{"id": "g1-pro-tier", "isDefault": True}]}))
    # onboardUser: cached token + API
    http.responses.append(http.ok({"name": "operations/op-1"}))
    # poll: cached token + API
    http.responses.append(
        http.ok(
            {
                "done": True,
                "response": {"cloudaicompanionProject": {"id": "onboarded-proj"}},
            }
        )
    )
    client = _client_for(http)
    resource = make_resource(project_id=None)

    project_id = await discover_project(client, resource)

    assert project_id == "onboarded-proj"
    urls = [c["url"] for c in http.post_calls]
    assert any("loadCodeAssist" in u for u in urls)
    assert any("onboardUser" in u for u in urls)
    assert any(u.endswith("v1internal:operations/op-1") for u in urls)
    onboard_call = next(c for c in http.post_calls if "onboardUser" in c["url"])
    assert onboard_call["json"]["tierId"] == "g1-pro-tier"
    assert onboard_call["json"]["metadata"]["ideType"] == "GCLI"


async def test_onboard_multi_poll_then_done():
    """LRO needs several poll rounds before done=true (3 polls)."""
    http = FakeHttp()
    # First call needs token refresh
    http.responses.append(http.token_ok("token-1", 3600))
    # poll_operation: 3 polls with cached token
    for done_val in (False, False, True):
        if done_val:
            http.responses.append(
                http.ok(
                    {
                        "done": True,
                        "response": {"cloudaicompanionProject": {"id": "proj-multi"}},
                    }
                )
            )
        else:
            http.responses.append(http.ok({"done": False}))
    client = _client_for(http)
    resource = make_resource(project_id=None)

    project_id = await poll_operation(
        client, resource, "operations/op-abc", max_attempts=5, interval_seconds=0.001
    )
    assert project_id == "proj-multi"
    polls = [c["url"] for c in http.post_calls if c["url"].endswith("operations/op-abc")]
    assert len(polls) == 3


async def test_onboard_operation_done_without_project_raises():
    http = FakeHttp()
    # onboardUser: cached token + API response with done but no project
    http.responses.append(
        http.ok({"name": "operations/op-err", "done": True, "response": {}})
    )
    client = _client_for(http)

    from providers.gemini_cli.errors import GeminiCliProtocolError

    with pytest.raises(GeminiCliProtocolError):
        await onboard_user(client, make_resource(project_id=None), "g1-pro-tier")


async def test_onboard_poll_timeout_raises():
    http = FakeHttp()
    # First call needs token refresh
    http.responses.append(http.token_ok("token-1", 3600))
    # 3 polls (max_attempts=3), each with cached token + in-progress
    for _ in range(3):
        http.responses.append(http.ok({"done": False}))
    client = _client_for(http)

    from providers.gemini_cli.errors import GeminiCliProtocolError

    with pytest.raises(GeminiCliProtocolError):
        await poll_operation(
            client,
            make_resource(project_id=None),
            "operations/op-slow",
            max_attempts=3,
            interval_seconds=0.001,
        )


async def test_discover_needs_onboarding_without_tier_raises():
    http = FakeHttp()
    # loadCodeAssist: token + response with no default tier
    http.responses.append(http.token_ok("token-1", 3600))
    http.responses.append(http.ok({"allowedTiers": []}))
    client = _client_for(http)

    from providers.gemini_cli.errors import GeminiCliProtocolError

    with pytest.raises(GeminiCliProtocolError):
        await discover_project(client, make_resource(project_id=None))
    assert not any("onboardUser" in c["url"] for c in http.post_calls)
