"""Python-side live viewer for Tier-3 (patchright) sessions.

The TS server at :3100 has its own `/session/:id/view` page that serves a
2-FPS screenshot stream + click-dispatch for human handoff on Tier-1 sessions.
t3 sessions are owned by the Python process, so we run a parallel tiny
aiohttp server on SUPERBROWSER_T3_VIEWER_PORT (default 3101) that:

  GET /t3/session/<sid>/view        → HTML page with <img> auto-refresh
  GET /t3/session/<sid>/screenshot  → fresh JPEG (cached 500ms)
  POST /t3/session/<sid>/click      → {x,y} → dispatches to patchright

Lifetime: started on first call to `ensure_started()`, lives as long as
the Python worker process.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from aiohttp import web

from . import interactive_session as _t3

logger = logging.getLogger(__name__)

_HTML_TEMPLATE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><title>T3 viewer: {sid}</title>
<style>
  body {{ margin:0; font-family: system-ui, sans-serif; background:#111; color:#eee; }}
  .banner {{ padding: 8px 14px; background:#223; border-bottom:1px solid #344;
             font-size: 13px; }}
  .wrap {{ position:relative; display:inline-block; }}
  img {{ display:block; max-width:100%; cursor: crosshair; }}
  .status {{ padding: 4px 14px; font-size: 12px; color:#8af; }}
  button {{ padding:4px 10px; background:#334; color:#eee; border:1px solid #556;
            border-radius:3px; cursor:pointer; font-size:12px; }}
</style>
</head><body>
<div class="banner">
  Tier-3 live viewer · session <code>{sid}</code>
  · <button id="pause">Pause</button>
  · <span class="status" id="status">streaming</span>
</div>
<div class="wrap">
  <img id="frame" src="/t3/session/{sid}/screenshot?t=0">
</div>
<script>
  const sid = {sid_json};
  const img = document.getElementById('frame');
  const statusEl = document.getElementById('status');
  const pauseBtn = document.getElementById('pause');
  let paused = false;
  let tick = 0;

  pauseBtn.addEventListener('click', () => {{
    paused = !paused;
    pauseBtn.textContent = paused ? 'Resume' : 'Pause';
    statusEl.textContent = paused ? 'paused' : 'streaming';
  }});

  async function refresh() {{
    if (paused) return;
    tick += 1;
    img.src = `/t3/session/${{sid}}/screenshot?t=${{tick}}`;
  }}
  setInterval(refresh, 500);

  img.addEventListener('click', async (e) => {{
    const rect = img.getBoundingClientRect();
    const scaleX = img.naturalWidth / rect.width;
    const scaleY = img.naturalHeight / rect.height;
    const x = (e.clientX - rect.left) * scaleX;
    const y = (e.clientY - rect.top) * scaleY;
    statusEl.textContent = `click @ (${{Math.round(x)}}, ${{Math.round(y)}})`;
    try {{
      await fetch(`/t3/session/${{sid}}/click`, {{
        method: 'POST',
        headers: {{'content-type': 'application/json'}},
        body: JSON.stringify({{x, y}}),
      }});
    }} catch (err) {{
      statusEl.textContent = 'click failed: ' + err;
    }}
  }});
</script>
</body></html>
"""


class _Server:
    def __init__(self) -> None:
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._lock = asyncio.Lock()
        self._port = int(os.environ.get("SUPERBROWSER_T3_VIEWER_PORT", "3101"))

    @property
    def port(self) -> int:
        return self._port

    async def ensure_started(self) -> None:
        if self._site is not None:
            return
        async with self._lock:
            if self._site is not None:
                return
            app = web.Application()
            app.router.add_get("/t3/session/{sid}/view", self._view)
            app.router.add_get("/t3/session/{sid}/screenshot", self._screenshot)
            app.router.add_post("/t3/session/{sid}/click", self._click)
            self._runner = web.AppRunner(app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, "0.0.0.0", self._port)
            try:
                await self._site.start()
                logger.info("t3 viewer listening on :%d", self._port)
            except OSError as exc:
                # Port in use — fall back to a random port.
                logger.warning(
                    "t3 viewer port %d busy (%s); falling back to ephemeral",
                    self._port, exc,
                )
                self._site = web.TCPSite(self._runner, "0.0.0.0", 0)
                await self._site.start()
                # aiohttp doesn't expose the bound port on TCPSite directly.
                # Leave the env default as a best-guess advertisement.

    async def _view(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        import json as _json
        html = _HTML_TEMPLATE.format(sid=sid, sid_json=_json.dumps(sid))
        return web.Response(text=html, content_type="text/html")

    async def _screenshot(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        try:
            png = await _t3.default().screenshot(sid)
        except KeyError:
            return web.Response(status=404, text="session not found")
        except Exception as exc:
            return web.Response(status=500, text=f"{type(exc).__name__}: {exc}")
        return web.Response(
            body=png,
            content_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    async def _click(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        x = float(payload.get("x", 0))
        y = float(payload.get("y", 0))
        try:
            await _t3.default().click_at(sid, x, y)
        except KeyError:
            return web.json_response({"ok": False, "error": "session not found"}, status=404)
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500,
            )
        return web.json_response({"ok": True, "x": x, "y": y})


_SERVER: Optional[_Server] = None


def default() -> _Server:
    global _SERVER
    if _SERVER is None:
        _SERVER = _Server()
    return _SERVER


async def ensure_started() -> str:
    """Start the viewer if not already running; return the base URL."""
    srv = default()
    await srv.ensure_started()
    host = os.environ.get("SUPERBROWSER_PUBLIC_HOST", "http://localhost")
    if "://" not in host:
        host = f"http://{host}"
    return f"{host.rstrip('/')}:{srv.port}"


def view_url(session_id: str) -> str:
    srv = default()
    host = os.environ.get("SUPERBROWSER_PUBLIC_HOST", "http://localhost")
    if "://" not in host:
        host = f"http://{host}"
    return f"{host.rstrip('/')}:{srv.port}/t3/session/{session_id}/view"
