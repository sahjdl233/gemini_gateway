"""Transport layer: HTTP / proxy / streaming seams.

Proxy MUST stay independent of providers: Provider -> Transport ->
Proxy/Egress.  Providers never know about sing-box or IP rotation.
"""
