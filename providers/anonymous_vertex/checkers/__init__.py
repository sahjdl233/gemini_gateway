"""Real AdmissionChecker implementations for Anonymous Vertex (ANON-009).

Each checker implements the :class:`~providers.anonymous_vertex.admission.
AdmissionChecker` Protocol against a NodeDefinition and produces a
:class:`NodeAdmissionResult` whose ``reason`` is a stable classification
token (``timeout`` / ``network_error`` / ``http_status_500`` / ...) for the
ANON-008-C FailureClassifier — checkers never decide quarantine themselves.
"""

from providers.anonymous_vertex.checkers.capability import (
    AnonymousVertexCapabilityChecker,
)
from providers.anonymous_vertex.checkers.connectivity import (
    AnonymousVertexConnectivityChecker,
)

__all__ = [
    "AnonymousVertexConnectivityChecker",
    "AnonymousVertexCapabilityChecker",
]
