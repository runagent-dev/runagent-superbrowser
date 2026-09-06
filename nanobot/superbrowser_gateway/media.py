"""Screenshot plumbing for chat replies.

Two sources, in preference order for on-demand peeks:
1. live engine frame — ``GET {engine}/session/{sid}/screenshot`` (raw JPEG),
   session id from the bridge's resumption marker (/tmp/superbrowser/resumption.json);
2. newest file in SUPERBROWSER_SCREENSHOT_DIR (the workers' vision screenshots,
   already ≤1568px — WhatsApp-safe).

Files are copied out of the shared tmp dir before sending: the next task may
overwrite the directory while a channel upload is still in flight.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path

from loguru import logger

DEFAULT_SCREENSHOT_DIR = "/tmp/superbrowser/screenshots"


def screenshot_dir() -> Path:
    return Path(os.environ.get("SUPERBROWSER_SCREENSHOT_DIR", DEFAULT_SCREENSHOT_DIR))


def resumption_session_id(ttl_s: float = 300.0) -> str | None:
    """The engine session id of the most recent browser task, if still fresh."""
    try:
        from superbrowser_bridge.session_tools.resumption import RESUMPTION_PATH
    except Exception:  # noqa: BLE001
        RESUMPTION_PATH = "/tmp/superbrowser/resumption.json"
    try:
        with open(RESUMPTION_PATH) as f:
            data = json.load(f)
        if time.time() - float(data.get("written_at", 0) or 0) > ttl_s:
            return None
        session_id = data.get("session_id")
        return str(session_id) if session_id else None
    except Exception:  # noqa: BLE001 - absent/expired marker is the normal case
        return None


class MediaService:
    def __init__(self, dest_dir: Path, *, max_side: int = 1600, jpeg_quality: int = 80):
        self.dest_dir = dest_dir
        self.max_side = max_side
        self.jpeg_quality = jpeg_quality
        dest_dir.mkdir(parents=True, exist_ok=True)

    def newest_screenshot(self, since_ts: float | None = None) -> Path | None:
        """Most recent worker screenshot, optionally no older than ``since_ts``."""
        directory = screenshot_dir()
        try:
            candidates = [p for p in directory.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png")]
        except OSError:
            return None
        if not candidates:
            return None
        newest = max(candidates, key=lambda p: p.stat().st_mtime)
        if since_ts is not None and newest.stat().st_mtime < since_ts:
            return None
        return newest

    def copy_and_fit(self, src: Path) -> Path | None:
        """Copy ``src`` into the gateway media dir, downscaling/re-encoding via
        PIL when possible (plain copy as the fallback)."""
        dest = self.dest_dir / f"{int(time.time())}-{uuid.uuid4().hex[:6]}{src.suffix or '.jpg'}"
        try:
            from PIL import Image

            with Image.open(src) as img:
                img = img.convert("RGB")
                width, height = img.size
                scale = self.max_side / max(width, height)
                if scale < 1.0:
                    img = img.resize((int(width * scale), int(height * scale)))
                dest = dest.with_suffix(".jpg")
                img.save(dest, "JPEG", quality=self.jpeg_quality)
            return dest
        except Exception:  # noqa: BLE001 - PIL missing or unreadable image
            try:
                shutil.copyfile(src, dest)
                return dest
            except OSError:
                logger.warning("could not copy screenshot {}", src)
                return None

    def save_bytes(self, data: bytes, suffix: str = ".jpg") -> Path:
        dest = self.dest_dir / f"{int(time.time())}-{uuid.uuid4().hex[:6]}{suffix}"
        dest.write_bytes(data)
        return dest

    async def live_peek(self, engine_url: str, session_id: str | None = None) -> Path | None:
        """Fetch the CURRENT page frame from the engine (falls back to None)."""
        sid = session_id or resumption_session_id()
        if not sid:
            return None
        try:
            import httpx

            headers = {}
            token = os.environ.get("SUPERBROWSER_TOKEN") or os.environ.get("TOKEN")
            if token:
                headers["Authorization"] = f"Bearer {token}"
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{engine_url.rstrip('/')}/session/{sid}/screenshot", headers=headers)
            if resp.status_code != 200 or not resp.content:
                return None
            return self.save_bytes(resp.content)
        except Exception:  # noqa: BLE001 - engine down / session gone
            return None

    async def current_view(self, engine_url: str) -> Path | None:
        """Best available 'what is the browser looking at right now' image."""
        live = await self.live_peek(engine_url)
        if live is not None:
            return live
        newest = self.newest_screenshot()
        if newest is not None:
            return self.copy_and_fit(newest)
        return None
