"""The public ``SuperBrowser`` facade.

A turnkey wrapper over the Orchestrator topology: terse goal in, rich result
out. The heavy prompting comes from the bundled SOUL files (provisioned
automatically), the orchestrator decides fetch-vs-browser in ``mode="auto"``,
and the messy bits (bus capture, server lifecycle, structured-output parsing)
are hidden.

Example::

    from runagent_superbrowser import SuperBrowser

    sb = SuperBrowser()
    res = sb.run("summarize the top story on hacker news", mode="fetch")
    print(res.text)

    # Full browser, auto-start the engine, get typed data back:
    from pydantic import BaseModel
    class Hotel(BaseModel):
        name: str; price_usd: float
    with SuperBrowser(auto_start_server=True) as sb:
        res = sb.run("4-5 star hotels in Sylhet, Sun-Thu, with nightly price",
                     url="https://gozayaan.com", mode="browser",
                     output_schema=list[Hotel])
        print(res.data)
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from ._capture import run_and_capture
from ._runtime import build_orchestrator
from .framing import frame_task, parse_output
from .modes import Mode
from .result import RunResult
from .server import ServerHandle, ServerStartError, ServerUnavailable

_DEFAULT_URL = "http://localhost:3100"


def _load_project_dotenv() -> None:
    """Load a ``.env`` (walking up from the cwd) into ``os.environ``.

    Mirrors ``superbrowser_bridge/cli.py`` and the bridge package itself, but
    runs at ``SuperBrowser`` construction — *before* we resolve server_url /
    vision / model — so values you put in ``.env`` (SUPERBROWSER_URL, LLM_MODEL,
    VISION_API_KEY, …) are visible to the SDK. ``override=False`` (python-dotenv
    default) means a real shell env var still wins, and explicit constructor
    arguments win over both.
    """
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:  # dotenv optional — env can still come from the shell
        return
    path = find_dotenv(usecwd=True)
    if path:
        load_dotenv(path)


class SuperBrowser:
    def __init__(
        self,
        *,
        model: str | None = None,
        workspace_root: str | Path | None = None,
        server_url: str | None = None,
        vision: bool | None = None,
        vision_api_key: str | None = None,
        auto_start_server: bool = False,
        server_cmd: list[str] | None = None,
        server_start_timeout: float = 30.0,
        provision_force: bool = False,
        env: dict[str, str] | None = None,
    ) -> None:
        # Load .env FIRST so .env values are visible below and to the bridge.
        # Explicit kwargs still take precedence (they're `x or os.environ...`).
        _load_project_dotenv()

        self.model = model
        self.auto_start_server = auto_start_server
        self.provision_force = provision_force
        self.server_url = (server_url or os.environ.get("SUPERBROWSER_URL") or _DEFAULT_URL).rstrip("/")

        # Set env BEFORE any superbrowser_bridge import — several bridge modules
        # freeze module-level constants (SUPERBROWSER_URL, the workspace paths)
        # at import time. Nothing above imports the bridge, and the bridge is
        # only imported later inside arun(), so this lands first.
        os.environ["SUPERBROWSER_URL"] = self.server_url
        if workspace_root:
            os.environ["SUPERBROWSER_WORKSPACE_ROOT"] = str(Path(workspace_root).expanduser().resolve())
        if vision is not None:
            os.environ["VISION_ENABLED"] = "1" if vision else "0"
        if vision_api_key:
            os.environ["VISION_API_KEY"] = vision_api_key
        if env:
            os.environ.update({k: str(v) for k, v in env.items()})

        self._server = ServerHandle(self.server_url, cmd=server_cmd, start_timeout=server_start_timeout)

    # ----- public API -----

    def run(
        self,
        task: str,
        *,
        mode: Mode = "auto",
        url: str | None = None,
        output_schema: Any | None = None,
        force_browser: bool = False,
        enable_human_handoff: bool = True,
        timeout: float | None = None,
    ) -> RunResult:
        """Synchronous entry point. Raises if called from a running event loop —
        use :meth:`arun` there."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.arun(
                    task,
                    mode=mode,
                    url=url,
                    output_schema=output_schema,
                    force_browser=force_browser,
                    enable_human_handoff=enable_human_handoff,
                    timeout=timeout,
                )
            )
        raise RuntimeError(
            "SuperBrowser.run() cannot be called from inside a running event "
            "loop; await SuperBrowser.arun(...) instead."
        )

    async def arun(
        self,
        task: str,
        *,
        mode: Mode = "auto",
        url: str | None = None,
        output_schema: Any | None = None,
        force_browser: bool = False,
        enable_human_handoff: bool = True,
        timeout: float | None = None,
    ) -> RunResult:
        classification = self._classify(task, url) if mode == "auto" else None

        # Server lifecycle: browser mode requires the engine (raise if missing
        # and auto-start is off). Auto mode only *pre-warms* it when the
        # classifier leans browser AND auto-start is on — never hard-fails,
        # since the agent may well choose fetch/search.
        if mode == "browser":
            await self._server.ensure(auto_start=self.auto_start_server)
        elif (
            mode == "auto"
            and self.auto_start_server
            and classification
            and classification.get("approach") in ("browser", "hybrid")
        ):
            try:
                await self._server.ensure(auto_start=True)
            except (ServerUnavailable, ServerStartError):
                pass

        orch = build_orchestrator(
            mode=mode, task=task, model=self.model, provision_force=self.provision_force
        )

        directive = orch.directive
        if not enable_human_handoff:
            note = (
                "Unattended run: when delegating to the browser, pass "
                "enable_human_handoff=False — no human is available to solve captchas."
            )
            directive = f"{directive}\n\n{note}" if directive else note

        framed = frame_task(
            task,
            mode_directive=directive,
            url=url,
            output_schema=output_schema,
            force_browser=force_browser or mode == "browser",
        )

        text, raw, error, success = "", "", None, False
        try:
            text, raw = await run_and_capture(
                orch.bot, framed, orch.session_key, hooks=[orch.hook], timeout=timeout
            )
            success = bool(text)
            if not success and error is None:
                error = "the agent returned no answer"
        except asyncio.TimeoutError:
            error = f"task timed out after {timeout}s"
        except Exception as exc:  # noqa: BLE001 - surface in the result, don't crash
            error = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                orch.memory.write_task_summary(success=success)
            except Exception:  # noqa: BLE001 - best-effort summary
                pass

        data = parse_output(text, output_schema) if success else None
        return RunResult(
            text=text,
            success=success,
            task_id=orch.task_id,
            mode=mode,
            data=data,
            error=error,
            raw_content=raw,
            classification=classification,
        )

    # ----- lifecycle -----

    def close(self) -> None:
        """Tear down an SDK-started engine (no-op for a pre-existing one)."""
        if self._server is not None:
            self._server.stop()

    def __enter__(self) -> SuperBrowser:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    async def __aenter__(self) -> SuperBrowser:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.close()

    # ----- internals -----

    @staticmethod
    def _classify(task: str, url: str | None) -> dict[str, Any] | None:
        """Surface the routing classifier's verdict (does not change behaviour)."""
        try:
            from superbrowser_bridge.routing import _classify_task

            return _classify_task(task, url)
        except Exception:  # noqa: BLE001
            return None
