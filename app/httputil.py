"""HTTP helpers that never send local traffic through a system proxy.

Windows users often run a system proxy (e.g. 127.0.0.1:7897). httpx honours
HTTP_PROXY / HTTPS_PROXY / ALL_PROXY via trust_env=True by default, which
breaks Ollama / ComfyUI / Confucius4 on localhost with HTTP 502. Every call
to a loopback host therefore uses trust_env=False.
"""

from __future__ import annotations

from urllib.parse import urlparse

import httpx

_LOOPBACK = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def is_loopback(url: str) -> bool:
    host = (urlparse(url if "://" in url else "http://" + url).hostname or "").lower()
    return host in _LOOPBACK or host.startswith("127.")


def client(url: str = "", *, timeout: float | httpx.Timeout = 30.0, **kwargs) -> httpx.AsyncClient:
    """AsyncClient that disables env proxies for loopback URLs."""
    trust = kwargs.pop("trust_env", None)
    if trust is None:
        trust = not is_loopback(url) if url else False
    # Prefer never trusting env for our local-service clients; callers that
    # need outbound proxied traffic can pass trust_env=True explicitly.
    return httpx.AsyncClient(timeout=timeout, trust_env=trust, **kwargs)
