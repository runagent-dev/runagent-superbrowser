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
_DEFAULT_LOCAL_AGENT_PORT = 8450


def _split_url(url: str) -> tuple[str, int]:
    """Split a ``http://host:port`` URL into ``(host, port)``.

    Defaults the port to 8450 (the ``runagent serve`` default) when absent.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url if "//" in url else f"//{url}", scheme="http")
    return parsed.hostname or "localhost", parsed.port or _DEFAULT_LOCAL_AGENT_PORT


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
        remote: bool = False,
        persistent: bool = False,
        agent_id: str | None = None,
        api_key: str | None = None,
        user_id: str | None = None,
        base_url: str | None = None,
        local_agent_url: str | None = None,
        local_agent_id: str | None = None,
    ) -> None:
        # Load .env FIRST so .env values are visible below and to the bridge.
        # Explicit kwargs still take precedence (they're `x or os.environ...`).
        _load_project_dotenv()

        # Remote (serverless) mode: execution is delegated to the RunAgent
        # serverless engine through the middleware, reusing the runagent SDK's
        # RunAgentClient (local=False + persistent_memory). See _run_remote and
        # docs/sdk.md "Remote (serverless) mode". When remote, the local engine /
        # ServerHandle below is never used.
        self.remote = remote or os.environ.get("SUPERBROWSER_REMOTE", "").lower() in ("1", "true", "yes")
        self.persistent = persistent
        self.api_key = api_key or os.environ.get("RUNAGENT_API_KEY")
        self.base_url = base_url or os.environ.get("RUNAGENT_BASE_URL")
        self.user_id = user_id
        self.agent_id = agent_id or os.environ.get("SUPERBROWSER_AGENT_ID") or os.environ.get("RUNAGENT_AGENT_ID")
        self._remote_client = None

        # Local-agent (Docker) mode: when NOT remote and a local agent server URL
        # is set, execution is delegated to a `runagent serve` agent server (the
        # all-in-one container) via RunAgentClient(local=True) — NO api key needed.
        # When no local URL is set, remote=False keeps the in-process path
        # (backward compatible). See _run_local_agent and docs/sdk.md.
        self.local_agent_url = local_agent_url or os.environ.get("SUPERBROWSER_LOCAL_AGENT_URL")
        self.local_agent = (not self.remote) and bool(self.local_agent_url)
        # The container's agent_id is the all-zeros UUID from
        # deploy/runagent.config.json; the user never has to type it.
        self.local_agent_id = (
            local_agent_id
            or os.environ.get("SUPERBROWSER_LOCAL_AGENT_ID")
            or "00000000-0000-0000-0000-000000000000"
        )
        self._local_client = None

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
        if self.remote:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return self._run_remote(
                    task, mode=mode, url=url, output_schema=output_schema, timeout=timeout
                )
            raise RuntimeError(
                "SuperBrowser.run() cannot be called from inside a running event "
                "loop; await SuperBrowser.arun(...) instead."
            )
        if self.local_agent:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return self._run_local_agent(
                    task, mode=mode, url=url, output_schema=output_schema, timeout=timeout
                )
            raise RuntimeError(
                "SuperBrowser.run() cannot be called from inside a running event "
                "loop; await SuperBrowser.arun(...) instead."
            )
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
        if self.remote:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                lambda: self._run_remote(
                    task, mode=mode, url=url, output_schema=output_schema, timeout=timeout
                ),
            )
        if self.local_agent:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                lambda: self._run_local_agent(
                    task, mode=mode, url=url, output_schema=output_schema, timeout=timeout
                ),
            )
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

    # ----- remote (serverless) execution -----

    def _run_remote(
        self,
        task: str,
        *,
        mode: Mode = "auto",
        url: str | None = None,
        output_schema: Any | None = None,
        timeout: float | None = None,
    ) -> RunResult:
        """Execute on the RunAgent serverless engine via the middleware, reusing
        the runagent SDK's ``RunAgentClient`` (``local=False`` + ``persistent_memory``).

        ``output_schema`` is not forwarded remotely in v1 (the engine returns
        text); pass it in local mode for typed parsing.
        """
        client = self._remote_runagent_client()
        input_kwargs: dict[str, Any] = {"task": task, "mode": mode}
        if url is not None:
            input_kwargs["url"] = url
        try:
            payload = client.run(**input_kwargs)
        except Exception as exc:  # noqa: BLE001 - surface in the result, don't crash
            return RunResult(
                text="",
                success=False,
                task_id="",
                mode=mode,
                data=None,
                error=f"{type(exc).__name__}: {exc}",
                raw_content="",
                classification=None,
            )
        return self._result_from_remote(payload, mode)

    def _remote_runagent_client(self):
        if self._remote_client is None:
            if not self.agent_id:
                raise ValueError(
                    "Remote mode requires an agent_id. Pass agent_id=... or set "
                    "SUPERBROWSER_AGENT_ID — find it on your Browser agent's page "
                    "in the RunAgent dashboard."
                )
            try:
                from runagent import RunAgentClient
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise ImportError(
                    "Remote mode needs the runagent SDK. Install it with "
                    "`pip install 'runagent-superbrowser[remote]'` (or `pip install runagent`)."
                ) from exc
            self._remote_client = RunAgentClient(
                agent_id=self.agent_id,
                entrypoint_tag="run",
                local=False,
                user_id=self.user_id,
                persistent_memory=self.persistent,
                api_key=self.api_key,
                base_url=self.base_url,
            )
        return self._remote_client

    # ----- local-agent (Docker) execution -----

    def _run_local_agent(
        self,
        task: str,
        *,
        mode: Mode = "auto",
        url: str | None = None,
        output_schema: Any | None = None,
        timeout: float | None = None,
    ) -> RunResult:
        """Execute against a local ``runagent serve`` agent server (the all-in-one
        Docker container) via ``RunAgentClient(local=True)``. No API key required.

        Unlike remote mode, ``output_schema`` IS parsed locally here — we own both
        ends of the round-trip and the engine returns the answer text.
        """
        client = self._local_runagent_client()
        input_kwargs: dict[str, Any] = {"task": task, "mode": mode}
        if url is not None:
            input_kwargs["url"] = url
        try:
            payload = client.run(**input_kwargs)
        except Exception as exc:  # noqa: BLE001 - surface in the result, don't crash
            return RunResult(
                text="",
                success=False,
                task_id="",
                mode=mode,
                data=None,
                error=f"{type(exc).__name__}: {exc}",
                raw_content="",
                classification=None,
            )
        result = self._result_from_remote(payload, mode)
        if output_schema is not None and result.success and result.data is None:
            result.data = parse_output(result.text, output_schema)
        return result

    def _local_runagent_client(self):
        if self._local_client is None:
            try:
                from runagent import RunAgentClient
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise ImportError(
                    "Local-agent mode needs the runagent SDK. Install it with "
                    "`pip install 'runagent-superbrowser[remote]'` (or `pip install runagent`)."
                ) from exc
            host, port = _split_url(self.local_agent_url or "")
            self._local_client = RunAgentClient(
                agent_id=self.local_agent_id,  # all-zeros UUID — matches the server route
                entrypoint_tag="run",
                local=True,
                host=host,
                port=port,
                user_id=self.user_id,
                persistent_memory=self.persistent,
                # NO api_key / base_url — a local agent server needs neither.
            )
        return self._local_client

    @staticmethod
    def _result_from_remote(payload: Any, mode: str) -> RunResult:
        """Wrap the in-VM ``main.py:run`` dict (already deserialized by
        RunAgentClient) into a RunResult."""
        if isinstance(payload, dict):
            text = payload.get("text", "") or ""
            return RunResult(
                text=text,
                success=bool(payload.get("success", bool(text))),
                task_id=payload.get("task_id") or "",
                mode=payload.get("mode") or mode,
                data=payload.get("data"),
                error=payload.get("error"),
                raw_content=text,
                classification=payload.get("classification"),
            )
        text = "" if payload is None else str(payload)
        return RunResult(
            text=text,
            success=bool(text),
            task_id="",
            mode=mode,
            data=None,
            error=None if text else "the agent returned no answer",
            raw_content=text,
            classification=None,
        )

    # ----- internals -----

    @staticmethod
    def _classify(task: str, url: str | None) -> dict[str, Any] | None:
        """Surface the routing classifier's verdict (does not change behaviour)."""
        try:
            from superbrowser_bridge.routing import _classify_task

            return _classify_task(task, url)
        except Exception:  # noqa: BLE001
            return None
