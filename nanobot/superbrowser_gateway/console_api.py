"""Console REST API + static SPA serving, mounted onto the gateway app.

Split from webhook_server.py to keep the channels-critical routes (health,
handoff sink, QR, pairing) separate from the console's config/sessions/doctor
surface. Everything here is behind the same auth middleware.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

from aiohttp import web
from loguru import logger

from .config import GatewaySettings

_STATIC_CACHE: dict[str, bytes] = {}
GW_SETTINGS_KEY = web.AppKey("gw_settings", GatewaySettings)


def _console_dist() -> Path | None:
    """Locate the packaged console build (console_dist/) if present."""
    try:
        base = resources.files("superbrowser_gateway") / "console_dist"
        index = base / "index.html"
        if index.is_file():
            return Path(str(base))
    except (ModuleNotFoundError, FileNotFoundError, TypeError):
        pass
    return None


# ----- config API -----


async def _config_get(request: web.Request) -> web.Response:
    from superbrowser_config import effective, load, redact_config, resolve_profile

    raw = load() or {}
    return web.json_response(
        {
            "config": redact_config(raw),
            "profile": resolve_profile(raw),
            "effective": [row for row in effective() if row["value"] is not None],
        }
    )


async def _config_put(request: web.Request) -> web.Response:
    from superbrowser_config import config_path, load, validate_config, write_config

    body = await _json_body(request)
    incoming = body.get("config")
    if not isinstance(incoming, dict):
        return web.json_response({"error": "body must be {config: {...}}"}, status=400)

    # Preserve secrets sent back as the redaction placeholder {set:true,...}.
    existing = load() or {}
    merged = _merge_preserving_secrets(existing, incoming)

    model, warnings, errors = validate_config(merged)
    if errors:
        return web.json_response({"ok": False, "errors": errors, "warnings": warnings}, status=400)
    write_config(config_path(), merged)
    return web.json_response(
        {
            "ok": True,
            "warnings": warnings,
            # engine-scoped keys need an engine restart to take effect
            "requiresRestart": ["engine"]
            if any(k in merged for k in ("engine", "vision", "t3", "antibot"))
            else [],
        }
    )


async def _config_validate(request: web.Request) -> web.Response:
    from superbrowser_config import validate_config

    body = await _json_body(request)
    candidate = body.get("config") if isinstance(body.get("config"), dict) else body
    _model, warnings, errors = validate_config(candidate)
    return web.json_response({"ok": not errors, "warnings": warnings, "errors": errors})


async def _profile_detect(_request: web.Request) -> web.Response:
    from superbrowser_config import detection_signals

    return web.json_response(detection_signals())


# ----- sessions / identities / doctor -----


async def _sessions(request: web.Request) -> web.Response:
    import os

    settings: GatewaySettings = request.app[GW_SETTINGS_KEY]
    engine = settings.engine_url.rstrip("/")
    # Live-view links must point at the ENGINE's viewer, honoring the public
    # host when one is set (Docker binds the viewer to loopback).
    view_base = (os.environ.get("SUPERBROWSER_PUBLIC_HOST") or engine).rstrip("/")
    sessions: list[dict[str, Any]] = []
    try:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{engine}/sessions")
        if resp.status_code == 200:
            data = resp.json()
            sessions = data.get("sessions", data) if isinstance(data, dict) else data
    except Exception:  # noqa: BLE001 - engine down / no endpoint
        pass
    for s in sessions:
        sid = s.get("id") or s.get("sessionId")
        if sid:
            s["viewUrl"] = f"{view_base}/session/{sid}/view"
    return web.json_response({"sessions": sessions})


async def _identities(request: web.Request) -> web.Response:
    settings: GatewaySettings = request.app[GW_SETTINGS_KEY]
    engine = settings.engine_url.rstrip("/")
    identities: list[dict[str, Any]] = []
    try:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{engine}/identity")
        if resp.status_code == 200:
            identities = resp.json().get("identities", [])
    except Exception:  # noqa: BLE001
        pass
    return web.json_response({"identities": identities})


async def _identity_forget(request: web.Request) -> web.Response:
    settings: GatewaySettings = request.app[GW_SETTINGS_KEY]
    engine = settings.engine_url.rstrip("/")
    domain = request.match_info["domain"]
    try:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.delete(f"{engine}/identity/{domain}")
        return web.json_response(resp.json() if resp.status_code == 200 else {"deleted": False}, status=resp.status_code)
    except Exception as exc:  # noqa: BLE001
        return web.json_response({"deleted": False, "error": str(exc)}, status=502)


async def _doctor(request: web.Request) -> web.Response:
    settings: GatewaySettings = request.app[GW_SETTINGS_KEY]
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "status": "ok" if ok else "warn", "detail": detail})

    from superbrowser_config import detect_profile, find_chrome, load, validate_config

    raw = load()
    if raw is None:
        add("config", True, "no config.json (zero-config mode)")
    else:
        _m, warnings, errors = validate_config(raw)
        add("config", not errors, "; ".join(errors) or "; ".join(warnings) or "valid")
    add("profile", True, f"detected {detect_profile()}")
    chrome = find_chrome()
    add("chrome", chrome is not None, chrome or "not found (using bundled Chromium)")

    engine_ok = False
    try:
        import httpx

        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{settings.engine_url.rstrip('/')}/health")
        engine_ok = resp.status_code == 200
    except Exception:  # noqa: BLE001
        pass
    add("engine", engine_ok, "reachable" if engine_ok else f"not reachable at {settings.engine_url}")

    # public-host warning for handoff links behind loopback-bound viewers
    import os

    public_host = os.environ.get("SUPERBROWSER_PUBLIC_HOST")
    if os.environ.get("HANDOFF_WEBHOOK_URL") and (not public_host or "127.0.0.1" in public_host or "localhost" in public_host):
        add(
            "handoff-links",
            False,
            "SUPERBROWSER_PUBLIC_HOST is unset/loopback — live-view links won't open from a phone; "
            "front the engine with a tunnel (cloudflared/tailscale) and set it.",
        )
    else:
        add("handoff-links", True, "public host configured" if public_host else "n/a")

    return web.json_response({"checks": checks})


# ----- static SPA -----


def _static_handler(dist: Path):
    index = dist / "index.html"

    async def handler(request: web.Request) -> web.StreamResponse:
        rel = request.match_info.get("path", "") or "index.html"
        candidate = (dist / rel).resolve()
        if not str(candidate).startswith(str(dist.resolve())) or not candidate.is_file():
            candidate = index  # SPA fallback
        body = _STATIC_CACHE.get(str(candidate))
        if body is None:
            body = candidate.read_bytes()
            _STATIC_CACHE[str(candidate)] = body
        content_type = _content_type(candidate.suffix)
        return web.Response(body=body, content_type=content_type)

    return handler


def _content_type(suffix: str) -> str:
    return {
        ".html": "text/html",
        ".js": "application/javascript",
        ".css": "text/css",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".ico": "image/x-icon",
        ".json": "application/json",
        ".woff2": "font/woff2",
    }.get(suffix, "application/octet-stream")


def mount_console(app: web.Application, settings: GatewaySettings) -> None:
    app[GW_SETTINGS_KEY] = settings

    app.router.add_get("/api/config", _config_get)
    app.router.add_put("/api/config", _config_put)
    app.router.add_post("/api/config/validate", _config_validate)
    app.router.add_get("/api/profile/detect", _profile_detect)
    app.router.add_get("/api/sessions", _sessions)
    app.router.add_get("/api/identities", _identities)
    app.router.add_delete("/api/identities/{domain}", _identity_forget)
    app.router.add_get("/api/doctor", _doctor)

    dist = _console_dist()
    if dist is not None:
        handler = _static_handler(dist)
        app.router.add_get("/", handler)
        app.router.add_get("/{path:.*}", handler)
        logger.info("console SPA served from {}", dist)
    else:
        logger.info("console dist not packaged — API-only (build console/ to enable the UI)")


async def _json_body(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _merge_preserving_secrets(existing: dict, incoming: dict) -> dict:
    """Deep-merge incoming over existing, but keep the existing secret when the
    incoming value is the redaction placeholder {"set": true, ...}."""
    out = dict(existing)
    for key, value in incoming.items():
        if isinstance(value, dict) and "set" in value and "last4" in value and len(value) <= 2:
            continue  # unchanged secret placeholder — keep existing
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge_preserving_secrets(out[key], value)
        else:
            out[key] = value
    return out
