"""Anonymous Vertex Provider for Google AI Studio (Agent Platform) batchGraphql endpoint.

This provider implements the Anonymous Vertex / Agent Platform Studio reverse-engineered
protocol as documented in docs/anonymous-vertex-protocol.md.

Key characteristics:
- POST to cloudconsole-pa.clients6.google.com/v3/.../batchGraphql
- GraphQL envelope with fixed querySignature, dynamic recaptchaToken
- Chrome 150 browser fingerprint headers (TLS fingerprint required for production)
- reCAPTCHA Enterprise token fetched per request (anchor -> reload)
- NDJSON streaming (brace-counting), not SSE
- Fixed safetySettings (4x BLOCK_NONE)
- Model capabilities from config/models.json (text family only)
- thoughtSignature sentinel injected for history parts with functionCall/thought
"""
from .provider import AnonymousVertexProvider, AnonymousVertexResource
from .factory import AnonymousVertexProviderFactory, AnonymousVertexResourceFactory

__all__ = [
    "AnonymousVertexProvider",
    "AnonymousVertexResource",
    "AnonymousVertexProviderFactory",
    "AnonymousVertexResourceFactory",
]
