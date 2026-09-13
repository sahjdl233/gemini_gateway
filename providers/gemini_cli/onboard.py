"""Code Assist project discovery & onboarding (TASK-007 §6, §7).
Flow:
  1. POST /v1internal:loadCodeAssist  {metadata:{ideType}}
      - existing account  -> read cloudaicompanionProject + tier directly
      - new account       -> pick default tier and run onboardUser
  2. POST /v1internal:onboardUser  {tierId, metadata}
      - long-running operation (LRO): poll until done (max 5 x 2s = 10s)
      - done=true -> response.cloudaicompanionProject.id
Interactive browser OAuth is intentionally NOT here (one-shot setup script).
Runtime onboarding re-uses the resource's stored OAuth token.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional, Tuple

from providers.gemini_cli.client import GeminiCliClient, DEFAULT_BASE_URL
from providers.gemini_cli.errors import GeminiCliProtocolError
from providers.gemini_cli.resource import GeminiCliResource

logger = logging.getLogger(__name__)

TierInfo = Dict[str, Any]


def _map_raw_tier(raw: str) -> str:
    """Tier mapping (TASK-007 §7.3)."""
    value = str(raw or "").lower()
    if value in ("g1-ultra-tier", "ws-ai-ultra-business-tier"):
        return "ULTRA"
    if value in ("g1-pro-tier", "helium-tier", "standard-tier"):
        return "PRO"
    if value == "free-tier":
        return "FREE"
    return "PRO"  # unknown defaults to pro


def _extract_tier(data: Dict[str, Any]) -> str:
    raw = ""
    for key in ("paidTier", "currentTier"):
        tier = data.get(key)
        if isinstance(tier, dict) and tier.get("id"):
            raw = tier["id"]
            break
    return _map_raw_tier(raw)


def _extract_credits(data: Dict[str, Any]) -> Optional[float]:
    for key in ("paidTier", "currentTier"):
        tier = data.get(key)
        if not isinstance(tier, dict):
            continue
        credits = tier.get("availableCredits") or []
        if credits and isinstance(credits[0], dict):
            return float(credits[0].get("creditAmount", 0))
    return None


def parse_load_code_assist(data: Dict[str, Any]) -> Dict[str, Any]:
    """Interpret a loadCodeAssist response.

    Returns {project_id, tier, credits, needs_onboarding, default_tier_id}.
    """
    project = data.get("cloudaicompanionProject")
    project_id = None
    if isinstance(project, dict):
        project_id = project.get("id") or project.get("projectId")
    needs_onboarding = not project_id and not data.get("currentTier")

    default_tier_id = None
    allowed = data.get("allowedTiers") or []
    if isinstance(allowed, list):
        for tier in allowed:
            if isinstance(tier, dict) and tier.get("isDefault"):
                default_tier_id = tier.get("id") or default_tier_id
        if default_tier_id is None and allowed and isinstance(allowed[0], dict):
            default_tier_id = allowed[0].get("id")

    return {
        "project_id": project_id,
        "tier": _extract_tier(data),
        "credits": _extract_credits(data),
        "needs_onboarding": bool(needs_onboarding),
        "default_tier_id": default_tier_id,
    }


def parse_operation(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """Parse an LRO poll response -> (done, project_id)."""
    if not isinstance(data, dict):
        return False, None
    done = bool(data.get("done"))
    project_id = None
    response = data.get("response") or {}
    if isinstance(response, dict):
        project = response.get("cloudaicompanionProject") or {}
        if isinstance(project, dict):
            project_id = project.get("id") or project.get("projectId")
    return done, project_id


def metadata_for(resource: Any, *, platform: Optional[str] = None) -> Dict[str, str]:
    return {
        "ideType": resource.ide_type,
        "platform": platform or resource.platform,
        "pluginType": resource.plugin_type,
    }


def inspect_operation(
    operation_name: str,
) -> Optional[str]:
    """Extract the operation path from a full operation-name URL (best effort)."""
    if not operation_name:
        return None
    if operation_name.startswith("http"):
        from urllib.parse import urlsplit

        return urlsplit(operation_name).path.lstrip("/")
    return operation_name


async def load_code_assist(
    client: GeminiCliClient,
    resource: GeminiCliResource,
) -> Dict[str, Any]:
    """POST /v1internal:loadCodeAssist with ide metadata.
    Returns parsed response dict from parse_load_code_assist.
    """
    payload = {"metadata": metadata_for(resource)}
    resp = await client.post(
        resource,
        DEFAULT_BASE_URL,
        payload,
        operation="loadCodeAssist",
    )
    return parse_load_code_assist(resp.json())


async def onboard_user(
    client: GeminiCliClient,
    resource: GeminiCliResource,
    tier_id: str,
) -> str:
    """POST /v1internal:onboardUser with tierId and metadata.
    Starts LRO, polls until done, returns cloudaicompanionProject.id.
    """
    payload = {"tierId": tier_id, "metadata": metadata_for(resource)}
    resp = await client.post(
        resource,
        DEFAULT_BASE_URL,
        payload,
        operation="onboardUser",
    )
    data = resp.json()
    operation_name = data.get("name")
    if not operation_name:
        raise GeminiCliProtocolError(
            "onboardUser: no operation name returned",
            provider="gemini_cli",
            resource_id=resource.id,
        )
    op_path = inspect_operation(operation_name)
    return await poll_operation(client, resource, op_path)


async def poll_operation(
    client: GeminiCliClient,
    resource: GeminiCliResource,
    operation_path: str,
    *,
    max_attempts: int = 5,
    interval_seconds: float = 2.0,
) -> str:
    """Poll LRO operation until done (max 5 x 2s = 10s).
    Returns cloudaicompanionProject.id on success.
    """
    for _attempt in range(max_attempts):
        resp = await client.post(
            resource,
            DEFAULT_BASE_URL,
            {},
            operation=operation_path,
        )
        data = resp.json()
        done, project_id = parse_operation(data)
        if done:
            if project_id:
                return project_id
            raise GeminiCliProtocolError(
                "LRO completed but no cloudaicompanionProject.id",
                provider="gemini_cli",
                resource_id=resource.id,
            )
        await asyncio.sleep(interval_seconds)

    raise GeminiCliProtocolError(
        f"LRO polling timeout after {max_attempts * interval_seconds}s",
        provider="gemini_cli",
        resource_id=resource.id,
    )


async def discover_project(
    client: GeminiCliClient,
    resource: GeminiCliResource,
) -> str:
    """Full project discovery flow.
    1. loadCodeAssist -> if project_id exists, return it
    2. If needs_onboarding -> onboardUser -> poll -> return project_id
    Raises GeminiCliProtocolError on any failure.
    """
    result = await load_code_assist(client, resource)
    project_id = result.get("project_id")
    if project_id:
        resource.tier = result.get("tier", "unknown")
        return project_id

    tier_id = result.get("default_tier_id")
    if not tier_id:
        raise GeminiCliProtocolError(
            "loadCodeAssist: needs onboarding but no default_tier_id",
            provider="gemini_cli",
            resource_id=resource.id,
        )

    project_id = await onboard_user(client, resource, tier_id)
    resource.tier = result.get("tier", "unknown")
    return project_id
