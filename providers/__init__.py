"""Provider packages live under providers/. Each provider is an
independent package and must never import another provider: they only
depend on core, protocol and transport.
"""
