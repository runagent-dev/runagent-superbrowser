"""TS browser-engine lifecycle: health-check, and opt-in spawn/teardown.

Full *browser* mode needs the TypeScript Puppeteer engine listening on
``SUPERBROWSER_URL`` (default ``http://localhost:3100``). *Fetch* mode is fully
in-process and never touches this.

Behaviour is opt-in (per the SDK's design): we always health-check, but we only
*spawn* the engine when ``auto_start=True``. We only ever tear down a process
*we* started — a pre-existing server the user launched is left alone.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx


class ServerUnavailable(RuntimeError):
    """The browser engine isn't reachable and auto-start wasn't requested."""


class ServerStartError(RuntimeError):
    """We tried to start the engine but it never became healthy."""


_HINT = (
    "The SuperBrowser TS engine isn't running at {url}. Start it with "
    "`superbrowser http` (or `npm start` in a checkout), or construct "
    "SuperBrowser(auto_start_server=True). If you installed via pip only, the "
    "engine is the separate npm package: `npm i -g runagent-superbrowser`."
)


class ServerHandle:
    def __init__(
        self,
        url: str,
        *,
        cmd: list[str] | None = None,
        start_timeout: float = 30.0,
    ) -> None:
        self.url = url.rstrip("/")
        self.cmd = cmd
        self.start_timeout = start_timeout
        self._proc: subprocess.Popen[bytes] | None = None
        self._spawned = False

    async def is_healthy(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.get(f"{self.url}/health")
                return r.status_code == 200
        except Exception:  # noqa: BLE001 - connection refused / timeout / etc.
            return False

    async def ensure(self, auto_start: bool) -> bool:
        """Guarantee the engine is reachable, starting it if allowed.

        Returns ``True`` on success. Raises ``ServerUnavailable`` when it's down
        and ``auto_start`` is False, or ``ServerStartError`` if a spawn never
        goes healthy.
        """
        if await self.is_healthy():
            return True
        if not auto_start:
            raise ServerUnavailable(_HINT.format(url=self.url))
        self._spawn()
        deadline = time.monotonic() + self.start_timeout
        while time.monotonic() < deadline:
            if await self.is_healthy():
                return True
            # Surface an early crash instead of waiting out the whole timeout.
            if self._proc is not None and self._proc.poll() is not None:
                raise ServerStartError(
                    f"browser engine exited (code {self._proc.returncode}) before "
                    f"becoming healthy; see {self._log_path()}"
                )
            time.sleep(0.3)
        raise ServerStartError(
            f"browser engine did not become healthy within {self.start_timeout}s "
            f"at {self.url}; see {self._log_path()}"
        )

    # ----- internals -----

    def _resolve_cmd(self) -> tuple[list[str], str | None]:
        """Return ``(argv, cwd)`` for spawning the engine."""
        if self.cmd:
            return self.cmd, None
        if shutil.which("superbrowser"):
            return ["superbrowser", "http"], None
        # Fall back to `npm start` from the nearest package.json (a checkout).
        pkg_root = _nearest_package_json()
        if pkg_root is not None and shutil.which("npm"):
            return ["npm", "start"], str(pkg_root)
        raise ServerUnavailable(_HINT.format(url=self.url))

    def _log_path(self) -> Path:
        try:
            from superbrowser_bridge.workspaces import workspace_root

            root = workspace_root()
        except Exception:  # noqa: BLE001
            root = Path.cwd()
        root.mkdir(parents=True, exist_ok=True)
        return root / "server.log"

    def _spawn(self) -> None:
        argv, cwd = self._resolve_cmd()
        log = self._log_path()
        self._logf = open(log, "ab")  # noqa: SIM115 - kept open for the proc's life
        self._proc = subprocess.Popen(  # noqa: S603 - argv is resolved, not shell
            argv,
            cwd=cwd,
            stdout=self._logf,
            stderr=subprocess.STDOUT,
        )
        self._spawned = True

    def stop(self) -> None:
        """Terminate the engine — only if we started it. Idempotent. Sync (all
        subprocess ops) so it's callable from ``close()`` and ``__exit__``."""
        if not self._spawned or self._proc is None:
            return
        proc = self._proc
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
        self._proc = None
        self._spawned = False
        logf = getattr(self, "_logf", None)
        if logf is not None:
            try:
                logf.close()
            except Exception:  # noqa: BLE001
                pass


def _nearest_package_json(start: Path | None = None) -> Path | None:
    here = (start or Path.cwd()).resolve()
    for parent in [here, *here.parents]:
        if (parent / "package.json").is_file():
            return parent
    return None
