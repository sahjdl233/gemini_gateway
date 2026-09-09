"""Protocol layer: OpenAI-compatible HTTP <-> internal models <-> provider.

Providers NEVER operate on raw OpenAI JSON directly; this layer stays
between the HTTP API and the Provider adapters.
"""
