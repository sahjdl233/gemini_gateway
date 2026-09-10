"""Anonymous Vertex Provider for Google AI Studio (Agent Platform) batchGraphql endpoint.

This provider implements the Anonymous Vertex / Agent Platform Studio reverse-engineered
protocol as documented in docs/anonymous-vertex-protocol.md.

Layered protocol stack (TASK-002-A):

    AnonymousVertexProvider (lifecycle / resource wiring)
            |
            v
    AnonymousVertexClient (client.py: wire / URL / headers / error mapping)
            |
            v
    AnonymousVertexProtocol (protocol.py: GraphQL envelope)
            |
            v
    HTTP Transport (transport.py / injected httpx-compatible client)

Key characteristics (fixed by the reference source vertex-singbox):
- POST to cloudconsole-pa.clients6.google.com/.../batchGraphql
- GraphQL envelope with fixed querySignature, dynamic recaptchaToken
- Chrome 150 browser fingerprint headers (TLS fingerprint required for production)
- reCAPTCHA Enterprise token fetched per request (anchor -> reload)
- NDJSON streaming (brace-counting), not SSE
- Fixed safetySettings (4x BLOCK_NONE)
- Model capabilities from config/models.json (text family only)
"""

from .provider import AnonymousVertexProvider, AnonymousVertexResource
from .factory import AnonymousVertexProviderFactory, AnonymousVertexResourceFactory
from .client import AnonymousVertexClient
from .models import AnonymousVertexRequest
from .transport import HttpxTransport
from .recaptcha import FakeRecaptchaTokenProvider, RecaptchaTokenProvider

__all__ = [
    "AnonymousVertexProvider",
    "AnonymousVertexResource",
    "AnonymousVertexProviderFactory",
    "AnonymousVertexResourceFactory",
    "AnonymousVertexClient",
    "AnonymousVertexRequest",
    "HttpxTransport",
    "RecaptchaTokenProvider",
    "FakeRecaptchaTokenProvider",
]

