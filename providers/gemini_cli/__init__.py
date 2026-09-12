"""Gemini CLI Provider (TASK-008).

Implements the Google Code Assist protocol (cloudcode-pa.googleapis.com)
as a Gateway Provider. Uses OAuth Bearer authentication and the
loadCodeAssist/onboardUser flow for project discovery.
"""
from providers.gemini_cli.provider import GeminiCliProvider
from providers.gemini_cli.resource import GeminiCliResource
from providers.gemini_cli.factory import (
    GeminiCliProviderFactory,
    GeminiCliResourceFactory,
)

__all__ = [
    "GeminiCliProvider",
    "GeminiCliResource",
    "GeminiCliProviderFactory",
    "GeminiCliResourceFactory",
]
