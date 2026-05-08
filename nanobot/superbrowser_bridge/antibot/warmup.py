"""Homepage warmup policy.

Actual warmup is tier-specific (Tier 2 = curl_cffi GET, Tier 3 = patchright
navigate). This module holds the shared decision logic: given a hostname,
should we pre-visit the root this call, or do we already have fresh
clearance cookies cached?
"""

from __future__ import annotations

from urllib.parse import urlparse

from . import cookie_jar


def root_url_for(url: str) -> str:
    u = urlparse(url)
    if not u.scheme or not u.netloc:
        return url
    return f"{u.scheme}://{u.netloc}/"


def hostname_of(url: str) -> str:
    return urlparse(url).hostname or ""


def should_warmup(url: str, *, force: bool = False) -> bool:
    """True if the caller should do a homepage pre-visit.

    Skips warmup when the cookie jar already has a fresh entry for the
    hostname (saved clearance cookies are replayable directly on the
    target URL without the extra round-trip).
    """
    if force:
        return True
    host = hostname_of(url)
    if not host:
        return False
    return not cookie_jar.has_fresh(host)
