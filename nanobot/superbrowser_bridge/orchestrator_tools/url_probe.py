"""Pre-flight URL probe — runs before spawning a browser worker so the
orchestrator can warn-back hard-blocked / unreachable URLs without
paying the cost of a full session.
"""

from __future__ import annotations

import httpx

from superbrowser_bridge.routing import _looks_blocked

async def _probe_url(url: str) -> dict:
    """Lightweight HTTP probe — no browser, no JS rendering.

    Two-stage:
      1. Fast `httpx` GET. Cheap path for unprotected sites.
      2. On transport error (timeout / connect / H2 RST), fall back to
         `curl_cffi` with `impersonate='chrome124'` + residential proxy from
         `proxy_tiers.default()`. Datacenter IPs that Akamai/Cloudflare
         silently drop need the real-browser TLS+H2 fingerprint to even get
         a response.

    Returns a dict with:
      - classification: "ok" | "soft_blocked" | "hard_unreachable"
      - protection: "" | "akamai" | "akamai_suspected" | "cloudflare" |
                    "datadome" | "imperva" | "perimeterx" | "kasada" |
                    "generic" | "rate_limited" | "structural" | "empty"
      - unreachable, blocked, status, error, reason, title (back-compat)

    A "soft_blocked" result is a routing signal, NOT an abort: the worker
    (Tier-3 patchright + residential proxy) still has a real shot.
    """
    import re as _re_probe
    from urllib.parse import urlparse

    result: dict = {
        "classification": "",
        "protection": "",
        "unreachable": False,
        "blocked": False,
        "status": 0,
        "error": "",
        "reason": "",
        "title": "",
    }

    def _set_title(body: str) -> None:
        m = _re_probe.search(
            r"<title[^>]*>(.*?)</title>", body, _re_probe.IGNORECASE | _re_probe.DOTALL,
        )
        if m:
            result["title"] = m.group(1).strip()[:200]

    # --- Stage 1: httpx fast path -------------------------------------------
    httpx_transport_error: str | None = None
    try:
        async with httpx.AsyncClient(
            timeout=6.0,
            follow_redirects=True,
            verify=False,  # some sites have self-signed certs
        ) as client:
            r = await client.get(url, headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml",
            })
            result["status"] = r.status_code
            body = r.text or ""
            _set_title(body[:5000])
            if r.status_code >= 500:
                # Genuine origin death — bytes received, server is sick.
                result["classification"] = "hard_unreachable"
                result["unreachable"] = True
                result["error"] = f"HTTP {r.status_code}"
                return result
            blocked, reason = _looks_blocked(body[:5000])
            result["blocked"] = blocked
            result["reason"] = reason
            if blocked:
                result["classification"] = "soft_blocked"
                # Try typed verdict for protection class
                try:
                    from superbrowser_bridge.antibot import bot_detect as _bd
                    verdict = _bd.detect(body, r.status_code, dict(r.headers))
                    if verdict.blocked and verdict.klass:
                        result["protection"] = verdict.klass
                        if not result["reason"]:
                            result["reason"] = verdict.reason
                except Exception:
                    pass
                return result
            result["classification"] = "ok"
            return result
    except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError) as exc:
        # Transport-level failure. Don't conclude unreachable yet — fall
        # through to curl_cffi which has a real-browser TLS+H2 fingerprint
        # and can route through the residential proxy. The IP-block-on-bot
        # signature (silent TCP drop or H2 RST) is exactly what Akamai/CF
        # do at the edge from datacenter egress.
        httpx_transport_error = f"{type(exc).__name__}: {str(exc)[:160]}"
    except Exception as exc:
        # Unexpected — record and try the cffi fallback as well; if that
        # also fails we'll mark hard_unreachable.
        httpx_transport_error = f"{type(exc).__name__}: {str(exc)[:160]}"

    # --- Stage 2: curl_cffi fallback with proxy -----------------------------
    cffi_status = 0
    cffi_body = ""
    cffi_headers: dict = {}
    cffi_error: str | None = None
    try:
        from curl_cffi import requests as _curl_requests
        from superbrowser_bridge.antibot import bot_detect as _bd
        from superbrowser_bridge.antibot import proxy_tiers as _proxy_tiers
        from superbrowser_bridge.antibot.headers import for_profile as _for_profile

        host = urlparse(url).hostname or ""
        proxy_url = _proxy_tiers.default().pick(host)
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        headers = _for_profile("chrome125_mac")

        async with _curl_requests.AsyncSession(
            impersonate="chrome124",
            timeout=12.0,
            proxies=proxies,
        ) as client:
            resp = await client.get(url, headers=headers, allow_redirects=True)
            cffi_status = int(resp.status_code or 0)
            cffi_body = resp.text or ""
            try:
                cffi_headers = dict(resp.headers or {})
            except Exception:
                cffi_headers = {}
    except Exception as exc:
        cffi_error = f"{type(exc).__name__}: {str(exc)[:160]}"

    # If curl_cffi also failed at transport layer, the next question is
    # whether this looks like (a) a real dead URL — DNS failure, refused
    # connection, bad SSL cert — or (b) a WAF-fronted host actively dropping
    # our datacenter IP. (b) is a routing signal, not an abort.
    if cffi_status == 0:
        combined_err = (
            f"{httpx_transport_error or ''} | {cffi_error or ''}"
        ).lower()
        # Hard-unreachable signatures: DNS resolution failure, peer refused
        # connection, SSL cert problem. These are genuine dead URLs.
        hard_signatures = (
            "could not resolve", "name or service not known",
            "no address associated", "nodename nor servname",
            "getaddrinfo", "[errno -2]", "[errno -3]",
            "connection refused", "connectionrefused",
            "ssl", "certificate",
            "ssl: certificate_verify_failed",
        )
        is_hard = any(sig in combined_err for sig in hard_signatures)
        if is_hard:
            result["classification"] = "hard_unreachable"
            result["unreachable"] = True
            result["error"] = cffi_error or httpx_transport_error or "unknown"
            return result
        # Otherwise: silent TCP drop / read timeout on both stacks. That's
        # the signature of edge IP-blocking on a WAF-fronted host — the
        # worker on residential egress still has a shot.
        result["classification"] = "soft_blocked"
        result["protection"] = "akamai_suspected"
        result["error"] = f"httpx={httpx_transport_error}; curl_cffi={cffi_error}"
        result["reason"] = (
            "transport-level drop on both httpx and curl_cffi — "
            "consistent with edge IP-block on a WAF-fronted host"
        )
        return result

    # We have a curl_cffi response. Populate status + title.
    result["status"] = cffi_status
    _set_title(cffi_body[:5000])

    # Genuine origin death (5xx with bytes) → hard_unreachable.
    if cffi_status >= 500:
        result["classification"] = "hard_unreachable"
        result["unreachable"] = True
        result["error"] = f"HTTP {cffi_status}"
        return result

    # Typed verdict on what came back.
    try:
        from superbrowser_bridge.antibot import bot_detect as _bd  # noqa: F811
        verdict = _bd.detect(cffi_body, cffi_status, cffi_headers)
    except Exception:
        verdict = None

    if verdict is not None and verdict.blocked:
        result["classification"] = "soft_blocked"
        result["protection"] = verdict.klass or "generic"
        result["blocked"] = True
        result["reason"] = verdict.reason or ""
        return result

    # Some 4xx without WAF markers — report blocked but unknown protection,
    # still let the worker try.
    if cffi_status in (401, 403, 429):
        result["classification"] = "soft_blocked"
        result["protection"] = "generic"
        result["blocked"] = True
        result["reason"] = f"HTTP {cffi_status} from curl_cffi probe"
        return result

    # 2xx clean.
    result["classification"] = "ok"
    return result


from nanobot.agent.tools.schema import BooleanSchema

