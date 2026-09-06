"""Lifecycle of the TypeScript browser server for the harness.

Python-side arms run against whatever server is up (default state). TS-side
arms need the server (re)started with their env baked in, because the server
reads its toggles at startup. This module is the only place that starts or
stops a server on behalf of the harness, and it only ever stops processes it
can prove hold the harness port.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eval._bootstrap import REPO_ROOT

PORT = int(os.environ.get("PORT", "3100"))
BASE_URL = os.environ.get("SUPERBROWSER_URL", f"http://localhost:{PORT}")
HEALTH_URL = f"{BASE_URL.rstrip('/')}/health"


def http_ok(url: str = HEALTH_URL, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - localhost
            return 200 <= resp.status < 300
    except Exception:
        return False


def pids_on_port(port: int = PORT) -> list[int]:
    try:
        out = subprocess.run(["lsof", f"-ti:{port}"], capture_output=True, text=True, check=False)
        return [int(x) for x in out.stdout.split()]
    except Exception:
        return []


def kill_port(port: int = PORT, *, wait_s: float = 15.0) -> None:
    for pid in pids_on_port(port):
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if not http_ok():
            return
        time.sleep(0.5)
    for pid in pids_on_port(port):
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass


@dataclass
class ServerHandle:
    proc: subprocess.Popen | None
    env: dict[str, str]
    log_path: Path | None
    started_by_us: bool


def start(env: dict[str, str] | None = None, *, log_dir: Path | None = None,
          timeout_s: float = 90.0) -> ServerHandle:
    """Start ``node build/index.js`` with ``env`` merged over os.environ."""
    if not (REPO_ROOT / "build" / "index.js").exists():
        raise RuntimeError("build/index.js missing — run `npm run build` first")
    kill_port()
    merged = {**os.environ, **(env or {})}
    log_path = None
    stdout: Any = subprocess.DEVNULL
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"server-{int(time.time())}.log"
        stdout = open(log_path, "ab")  # noqa: SIM115 - handed to Popen
    proc = subprocess.Popen(  # noqa: S603
        ["node", "build/index.js"], cwd=str(REPO_ROOT), env=merged,
        stdout=stdout, stderr=subprocess.STDOUT, start_new_session=True,
    )
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if http_ok():
            return ServerHandle(proc, dict(env or {}), log_path, True)
        if proc.poll() is not None:
            raise RuntimeError(f"browser server exited early (code {proc.returncode}); see {log_path}")
        time.sleep(0.5)
    raise RuntimeError(f"browser server not healthy after {timeout_s:.0f}s; see {log_path}")


def stop(handle: ServerHandle | None) -> None:
    if handle is None or handle.proc is None:
        return
    try:
        os.killpg(handle.proc.pid, signal.SIGTERM)
        handle.proc.wait(timeout=10)
    except Exception:
        try:
            os.killpg(handle.proc.pid, signal.SIGKILL)
        except Exception:
            pass
    kill_port()


class ServerManager:
    """Keeps exactly one server up whose TS env matches the current arm.

    ``manage=False`` (default) never touches the server: it verifies health
    and, for TS-side arms, refuses to run unless ``expect_env`` matches the
    signature the operator declared they started the server with
    (``--assume-server-env``). ``manage=True`` restarts as needed.
    """

    def __init__(self, *, manage: bool, log_dir: Path | None = None,
                 assumed_env: dict[str, str] | None = None):
        self.manage = manage
        self.log_dir = log_dir
        self.handle: ServerHandle | None = None
        self.current_env: dict[str, str] | None = dict(assumed_env) if assumed_env else None

    def ensure(self, ts_env: dict[str, str]) -> None:
        want = dict(ts_env)
        if self.manage:
            if self.handle is not None and self.current_env == want and http_ok():
                return
            stop(self.handle)
            self.handle = start(want, log_dir=self.log_dir)
            self.current_env = want
            return
        if not http_ok():
            raise RuntimeError(
                "browser server is not running on %s. Start it (`npm start`) or pass --manage-server."
                % BASE_URL)
        have = self.current_env if self.current_env is not None else {}
        if want != have:
            raise RuntimeError(
                "this arm needs the browser server started with %r but the harness was told it runs "
                "with %r. Restart the server with that env, pass --assume-server-env, or use "
                "--manage-server." % (want, have))

    def close(self) -> None:
        if self.manage:
            stop(self.handle)
            self.handle = None
