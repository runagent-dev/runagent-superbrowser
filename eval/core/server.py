"""Lifecycle of the TypeScript browser server for the harness.

Python-side arms run against whatever server is up (default state). TS-side
arms need a server started with their env baked in, because the server reads
its toggles at startup. When the harness manages servers it starts its OWN
instance on a free port (``--server-port`` or automatic) and points runs at
it via ``SUPERBROWSER_URL`` — it never restarts or kills a server it did not
start (on this box :3100 is typically the user's Docker container).
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eval._bootstrap import REPO_ROOT

PORT = int(os.environ.get("PORT", "3100"))
BASE_URL = os.environ.get("SUPERBROWSER_URL", f"http://localhost:{PORT}")


def health_url(base: str = BASE_URL) -> str:
    return f"{base.rstrip('/')}/health"


def http_ok(url: str | None = None, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url or health_url(), timeout=timeout) as resp:  # noqa: S310 - localhost
            return 200 <= resp.status < 300
    except Exception:
        return False


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except Exception:
        return ""


def pids_on_port(port: int) -> list[int]:
    try:
        out = subprocess.run(["lsof", f"-ti:{port}"], capture_output=True, text=True, check=False)
        return [int(x) for x in out.stdout.split()]
    except Exception:
        return []


def kill_our_server_on_port(port: int, *, wait_s: float = 15.0) -> list[int]:
    """Terminate ONLY harness-style servers (``node build/index.js``) on the port."""
    victims = [pid for pid in pids_on_port(port) if "build/index.js" in _cmdline(pid)]
    for pid in victims:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    deadline = time.time() + wait_s
    while victims and time.time() < deadline and any(_cmdline(p) for p in victims):
        time.sleep(0.5)
    for pid in victims:
        if _cmdline(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
    return victims


@dataclass
class ServerHandle:
    proc: subprocess.Popen | None
    env: dict[str, str]
    port: int
    base_url: str
    log_path: Path | None


def start(env: dict[str, str] | None = None, *, port: int | None = None, log_dir: Path | None = None,
          timeout_s: float = 90.0) -> ServerHandle:
    """Start ``node build/index.js`` on ``port`` (free port when None) with ``env`` merged over os.environ."""
    if not (REPO_ROOT / "build" / "index.js").exists():
        raise RuntimeError("build/index.js missing — run `npm run build` first")
    port = port or free_port()
    if pids_on_port(port):
        kill_our_server_on_port(port)
        if pids_on_port(port):
            raise RuntimeError(f"port {port} is held by a process the harness did not start "
                               f"({_cmdline(pids_on_port(port)[0])[:80]!r}); choose another --server-port")
    merged = {**os.environ, **(env or {}), "PORT": str(port)}
    merged.pop("TOKEN", None)  # loopback harness server: no auth token needed
    log_path = None
    stdout: Any = subprocess.DEVNULL
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"server-{port}-{int(time.time())}.log"
        stdout = open(log_path, "ab")  # noqa: SIM115 - handed to Popen
    proc = subprocess.Popen(  # noqa: S603
        ["node", "build/index.js"], cwd=str(REPO_ROOT), env=merged,
        stdout=stdout, stderr=subprocess.STDOUT, start_new_session=True,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if http_ok(health_url(base)):
            return ServerHandle(proc, dict(env or {}), port, base, log_path)
        if proc.poll() is not None:
            raise RuntimeError(f"browser server exited early (code {proc.returncode}); see {log_path}")
        time.sleep(0.5)
    stop(ServerHandle(proc, dict(env or {}), port, base, log_path))
    raise RuntimeError(f"browser server not healthy after {timeout_s:.0f}s; see {log_path}")


def stop(handle: ServerHandle | None) -> None:
    if handle is None or handle.proc is None:
        return
    try:
        os.killpg(handle.proc.pid, signal.SIGTERM)
        handle.proc.wait(timeout=15)
    except Exception:
        try:
            os.killpg(handle.proc.pid, signal.SIGKILL)
        except Exception:
            pass


class ServerManager:
    """Keeps exactly one harness server up whose TS env matches the current arm.

    ``manage=False`` (default): never touches any server; verifies the target
    is healthy and, for TS-side arms, refuses to run unless ``assumed_env``
    says the operator started it with that env.
    ``manage=True``: starts its own server on ``port`` (or a free port) and
    restarts it whenever the required TS env changes. ``base_url`` is the URL
    runs must use (the runner exports it as SUPERBROWSER_URL).
    """

    def __init__(self, *, manage: bool, log_dir: Path | None = None, assumed_env: dict[str, str] | None = None,
                 port: int | None = None, base_url: str = BASE_URL):
        self.manage = manage
        self.log_dir = log_dir
        self.port = port
        self.handle: ServerHandle | None = None
        self.current_env: dict[str, str] | None = dict(assumed_env) if assumed_env else None
        self._external_base = base_url

    @property
    def base_url(self) -> str:
        if self.manage and self.handle is not None:
            return self.handle.base_url
        return self._external_base

    def ensure(self, ts_env: dict[str, str]) -> str:
        want = dict(ts_env)
        if self.manage:
            if self.handle is not None and self.current_env == want and http_ok(health_url(self.handle.base_url)):
                return self.handle.base_url
            stop(self.handle)
            self.handle = start(want, port=self.port, log_dir=self.log_dir)
            self.current_env = want
            return self.handle.base_url
        if not http_ok(health_url(self._external_base)):
            raise RuntimeError(
                "browser server is not running on %s. Start it (`npm start`) or pass --manage-server."
                % self._external_base)
        have = self.current_env if self.current_env is not None else {}
        if want != have:
            raise RuntimeError(
                "this arm needs the browser server started with %r but the harness was told it runs "
                "with %r. Restart the server with that env, pass --assume-server-env, or use "
                "--manage-server." % (want, have))
        return self._external_base

    def close(self) -> None:
        if self.manage:
            stop(self.handle)
            self.handle = None
