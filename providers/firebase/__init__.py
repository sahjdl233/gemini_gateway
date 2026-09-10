"""Firebase AI Logic Provider (TASK-004).

Uses firebasevertexai.googleapis.com/v1beta with App Check auth.
One Firebase Project = one Resource.
"""

from .provider import FirebaseProvider
from .resource import FirebaseResource
from .factory import FirebaseProviderFactory, FirebaseResourceFactory

__all__ = [
    "FirebaseProvider",
    "FirebaseResource",
    "FirebaseProviderFactory",
    "FirebaseResourceFactory",
]
