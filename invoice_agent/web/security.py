"""Request guards for the review screen: only the screen itself may change state (approve, reject, upload).
Blocks cross-site form posts (CSRF) and, together with the trusted-host check, DNS-rebinding attacks on the local server.
"""
from urllib.parse import urlsplit

from starlette.requests import Request

# Host names the local server answers to; "testserver" is FastAPI TestClient's host.
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]

# Methods that only read: everything else changes state and must come from our own pages.
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _origin_of(url: str) -> str | None:
    """The scheme://host[:port] part of a URL, or None if it has none."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower() if parts.scheme and parts.netloc else None


def is_same_origin(request: Request) -> bool:
    """True when a request comes from one of our own pages (its Origin, or else Referer, matches this server)."""
    own = f"{request.url.scheme}://{request.headers.get('host', '')}".lower()
    origin = request.headers.get("origin")
    # Browsers send Origin on form posts; Referer is the fallback for those that strip it.
    source = origin if origin and origin != "null" else request.headers.get("referer")
    # Fail closed: a state-changing request that says nothing about where it came from is refused.
    return bool(source) and _origin_of(source) == own


def needs_origin_check(request: Request) -> bool:
    """True for requests that change state (POST, PUT, DELETE, ...)."""
    return request.method.upper() not in SAFE_METHODS
