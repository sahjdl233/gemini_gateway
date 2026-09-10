"""Anonymous Vertex GraphQL protocol layer.

This module is the ONLY place that knows the Google batchGraphql envelope
structure: the ``requestContext``, ``querySignature``, ``operationName``
and ``variables`` members.

Responsibility flow::

    AnonymousVertexRequest (internal model, produced by request.py)
              |  protocol.build_graphql_payload()
              v
    GraphQLPayload (this module) -> JSON body (client.py)

The Provider and the Client never assemble Google dictionaries directly;
they call into this module.  querySignature / operationName / endpoint
live in signature.py (single source of truth) and are imported here.
"""

from __future__ import annotations

import random
import uuid

from providers.anonymous_vertex.models import (
    AnonymousVertexRequest,
    AnonymousVertexRequestContext,
    GraphQLPayload,
    GraphQLVariables,
)
from providers.anonymous_vertex.signature import (
    OPERATION_NAME,
    QUERY_SIGNATURE,
)


def random_page_view_id() -> int:
    """Random 16-digit page view id (mirrors payload.go randomPageViewID)."""
    return random.randint(1000000000000000, 9000000000000000)


def random_tracking_id() -> str:
    """Random "d<16 digits>" tracking id (mirrors payload.go randomTrackingID)."""
    return "d" + "".join(random.choice("0123456789") for _ in range(16))


def random_uuid() -> str:
    """Random UUID v4 (mirrors payload.go randomUUID)."""
    return str(uuid.uuid4())


def build_request_context() -> AnonymousVertexRequestContext:
    """Construct the per-request GraphQL requestContext."""
    return AnonymousVertexRequestContext(
        page_view_id=random_page_view_id(),
        tracking_id=random_tracking_id(),
        client_session_id=random_uuid(),
    )


def build_graphql_payload(
    request: AnonymousVertexRequest,
    recaptcha_token: str,
    request_context: AnonymousVertexRequestContext | None = None,
) -> GraphQLPayload:
    """Build the full GraphQL envelope from an internal request.

    Parameters
    ----------
    request:
        Internal request model produced by request.py conversion.
    recaptcha_token:
        Fresh reCAPTCHA Enterprise token (see recaptcha.py).
    request_context:
        Optional pre-built context; when None a fresh one is generated
        (tests may pass a deterministic context for snapshot stability).
    """
    context = request_context or build_request_context()
    variables = GraphQLVariables(
        request=request,
        region="global",
        recaptcha_token=recaptcha_token,
    )
    return GraphQLPayload(
        request_context=context,
        query_signature=QUERY_SIGNATURE,
        operation_name=OPERATION_NAME,
        variables=variables,
    )


def serialize_payload(payload: GraphQLPayload) -> str:
    """Serialize a GraphQLPayload to the JSON string sent upstream."""
    import json

    return json.dumps(payload.to_dict())

