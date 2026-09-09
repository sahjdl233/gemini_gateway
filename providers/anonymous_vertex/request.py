"""Build the Anonymous Vertex GraphQL envelope (request body).

Matches BatchGraphQLPayload from Go payload.go.
"""
from __future__ import annotations

import random
import uuid
from typing import Any, Dict

from providers.anonymous_vertex.signature import (
    ANON_BASE_URL,
    BATCH_GRAPHQL_PATH,
    OPERATION_NAME,
    QUERY_SIGNATURE,
)


def random_page_view_id() -> int:
    return random.randint(1000000000000000, 9000000000000000)


def random_tracking_id() -> str:
    return "d" + "".join(random.choice("0123456789") for _ in range(16))


def random_uuid() -> str:
    return str(uuid.uuid4())


def build_batch_graphql_url(api_key: str) -> str:
    return f"{ANON_BASE_URL}{BATCH_GRAPHQL_PATH}?key={api_key}&prettyPrint=false"


def build_request_context() -> dict:
    return {
        "clientVersion": "boq_cloud-boq-clientweb-vertexaistudio_20260630.00_p0",
        "pagePath": "/agent-platform/studio/multimodal",
        "pageViewId": random_page_view_id(),
        "trackingId": random_tracking_id(),
        "backendOverrides": {},
        "clientSessionId": random_uuid(),
        "selectedPurview": {},
        "jurisdiction": "global",
        "localizationData": {"locale": "zh_CN", "timezone": "Asia/Hong_Kong"},
    }


def build_envelope(
    model: str,
    gemini_request: Dict[str, Any],
    recaptcha_token: str,
) -> Dict[str, Any]:
    """Build the full GraphQL envelope for the batchGraphql POST body."""
    variables = dict(gemini_request)
    variables["model"] = model
    variables["region"] = "global"
    variables["recaptchaToken"] = recaptcha_token

    return {
        "requestContext": build_request_context(),
        "querySignature": QUERY_SIGNATURE,
        "operationName": OPERATION_NAME,
        "variables": variables,
    }
