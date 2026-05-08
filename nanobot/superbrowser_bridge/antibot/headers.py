"""Hand-curated realistic HTTP header bundles per browser profile.

Pattern ported from crawlee-python's `HeaderGenerator`
(reference: /root/agentic-browser/crawlee-python/src/crawlee/fingerprint_suite/_header_generator.py:25-85).
Crawlee wraps browserforge's 50MB fingerprint dataset. We ship a small
static table captured from real DevTools instead.

curl_cffi already emits matching TLS + client-hint headers for the UA we
pick when `impersonate='chrome124'` is set — this table supplies the
additional high-level headers (Accept, Accept-Language, sec-fetch-*).
"""

from __future__ import annotations

import random
from typing import Literal

Profile = Literal[
    "chrome124_mac", "chrome124_linux",
    "chrome125_mac", "chrome125_linux",
    "chrome126_mac", "chrome126_linux",
]

_BASE_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,image/apng,*/*;q=0.8,"
    "application/signed-exchange;v=b3;q=0.7"
)

_CHROME_PROFILES: dict[str, dict[str, str]] = {
    "chrome124_mac": {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
    },
    "chrome124_linux": {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Linux"',
    },
    "chrome125_mac": {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/125.0.0.0 Safari/537.36",
        "sec-ch-ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
    },
    "chrome125_linux": {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/125.0.0.0 Safari/537.36",
        "sec-ch-ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Linux"',
    },
    "chrome126_mac": {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/126.0.0.0 Safari/537.36",
        "sec-ch-ua": '"Google Chrome";v="126", "Chromium";v="126", "Not.A/Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
    },
    "chrome126_linux": {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/126.0.0.0 Safari/537.36",
        "sec-ch-ua": '"Google Chrome";v="126", "Chromium";v="126", "Not.A/Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Linux"',
    },
}

_ACCEPT_LANGUAGES = [
    "en-US,en;q=0.9",
    "en-US,en;q=0.9,es;q=0.8",
    "en-GB,en;q=0.9,en-US;q=0.8",
]


def for_profile(
    name: Profile = "chrome124_mac",
    *,
    referer: str | None = None,
    document: bool = True,
) -> dict[str, str]:
    """Return a complete header bundle matching the named profile.

    `document=True` sets `Sec-Fetch-Dest: document` (navigation); set to
    False when the caller is doing a subresource request.
    `referer` is applied only when provided — some sites reject missing
    Referer on deep links, others flag incorrect Referer as a bot signal.
    """
    if name not in _CHROME_PROFILES:
        raise ValueError(f"unknown header profile: {name}")
    base = _CHROME_PROFILES[name]
    h: dict[str, str] = {
        "User-Agent": base["User-Agent"],
        "Accept": _BASE_ACCEPT,
        "Accept-Language": random.choice(_ACCEPT_LANGUAGES),
        "sec-ch-ua": base["sec-ch-ua"],
        "sec-ch-ua-mobile": base["sec-ch-ua-mobile"],
        "sec-ch-ua-platform": base["sec-ch-ua-platform"],
        "Sec-Fetch-Dest": "document" if document else "empty",
        "Sec-Fetch-Mode": "navigate" if document else "cors",
        "Sec-Fetch-Site": "none" if not referer else "same-origin",
        "Sec-Fetch-User": "?1" if document else "?0",
        "Upgrade-Insecure-Requests": "1" if document else "",
        "Cache-Control": "max-age=0" if document else "",
    }
    if referer:
        h["Referer"] = referer
    return {k: v for k, v in h.items() if v}


def random_profile() -> Profile:
    return random.choice(list(_CHROME_PROFILES.keys()))  # type: ignore[return-value]


def all_profiles() -> list[Profile]:
    return list(_CHROME_PROFILES.keys())  # type: ignore[return-value]
