"""HTTP for public feeds that must not inherit the app proxy.

The deployment ``.env`` often points ``HTTP(S)_PROXY`` at a Docker-only
gateway. Inside a container that is correct; a host-run backend cannot resolve
it, and every public outbound request then fails before reaching the upstream.
These feeds are public, so they deliberately bypass the process proxy instead
of taking the caller down with a deployment detail.
"""

from __future__ import annotations

import urllib.request

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def open_url(request: urllib.request.Request, *, timeout: float):
    return _OPENER.open(request, timeout=timeout)
