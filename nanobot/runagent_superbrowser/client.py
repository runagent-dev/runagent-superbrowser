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
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterator

from . import lifecycle
from ._capture import run_and_capture, stream_and_capture
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
        pass
    else:
        path = find_dotenv(usecwd=True)
        if path:
            load_dotenv(path)
    # Product config (~/.superbrowser/config.json) projects into env AFTER
    # dotenv and fills only still-unset keys, so shell env and .env keep
    # precedence. Fail-open: a missing/broken superbrowser_config never
    # blocks the SDK.
    try:
        from superbrowser_config import apply_to_env

        apply_to_env()
    except Exception:  # noqa: BLE001
        pass


async def _watch_cancel(handle: str, run_task: "asyncio.Future") -> None:
    """Cancel ``run_task`` when the lifecycle registry flags ``handle``.

    Polling (0.5s) keeps this decoupled from the run internals; the poll cost
    is negligible next to LLM steps.
    """
    try:
        while not run_task.done():
            if lifecycle.cancelled(handle):
                run_task.cancel()
                return
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        pass


def _drive_async_gen(factory):
    """Iterate an async generator from synchronous code on a private loop.

    Used by :meth:`SuperBrowser.stream` for the in-process path. ``factory`` must
    return a fresh async generator each call.
    """
    loop = asyncio.new_event_loop()
    agen = None
    try:
        agen = factory()
        while True:
            try:
                yield loop.run_until_complete(agen.__anext__())
            except StopAsyncIteration:
                break
    finally:
        try:
            if agen is not None:
                loop.run_until_complete(agen.aclose())
        finally:
            loop.close()


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
        audit_dir: str | Path | None = None,
        audit_experiment: str = "sdk",
        audit_arm: str = "ledger",
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
        self._remote_stream_client_obj = None

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
        self._local_stream_client_obj = None
        self._aux_clients: dict[str, Any] = {}
        self._entrypoints_cache: set[str] | None = None

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

        # Audit trail (opt-in): every in-process run leaves the evaluation
        # harness's run directory under <audit_dir>/<experiment>/<arm>/<task>/seedN/.
        # The screenshot dir is frozen when the bridge is imported, so it is
        # pointed at a per-process inbox HERE, before any bridge import; the
        # recorder moves the frames into the run dir when the run finishes.
        self._audit = None
        audit_dir = audit_dir or os.environ.get("SUPERBROWSER_AUDIT_DIR")
        if audit_dir:
            from superbrowser_bridge.audit import AuditRecorder, repoint_screenshot_dir

            self._audit = AuditRecorder(audit_dir, experiment=audit_experiment, arm=audit_arm)
            inbox = self._audit.inbox_screenshot_dir()
            os.environ["SUPERBROWSER_SCREENSHOT_DIR"] = str(inbox)
            repoint_screenshot_dir(inbox)

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
        task_handle: str | None = None,
        on_event: Callable[[dict], None] | None = None,
        audit_task: dict[str, Any] | None = None,
        audit_seed: int = 0,
    ) -> RunResult:
        """Synchronous entry point. Raises if called from a running event loop —
        use :meth:`arun` there.

        ``audit_task`` (with ``audit_dir`` on the client) labels the recorded run:
        a dict with ``task_id`` / ``benchmark`` / ``level`` / ``start_url`` /
        ``instruction`` (e.g. ``eval.core.tasks.Task.to_dict()``); without it the
        task id is derived from the instruction and URL. ``audit_seed`` names the
        attempt; a seed whose run was already judged is never overwritten.

        ``task_handle`` names the run in the lifecycle registry so another
        thread (or, in local-agent mode, another process) can ``cancel()`` it;
        one is generated when omitted and returned on ``RunResult.task_handle``.
        ``on_event`` receives progress events on the local-agent streaming
        transport (ignored elsewhere — use :meth:`stream`/:meth:`astream` for
        full event streams).
        """
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
                    task,
                    mode=mode,
                    url=url,
                    output_schema=output_schema,
                    timeout=timeout,
                    task_handle=task_handle,
                    on_event=on_event,
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
                    task_handle=task_handle,
                    audit_task=audit_task,
                    audit_seed=audit_seed,
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
        task_handle: str | None = None,
        on_event: Callable[[dict], None] | None = None,
        audit_task: dict[str, Any] | None = None,
        audit_seed: int = 0,
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
                    task,
                    mode=mode,
                    url=url,
                    output_schema=output_schema,
                    timeout=timeout,
                    task_handle=task_handle,
                    on_event=on_event,
                ),
            )
        # The audit recorder sets its env (trace/capture writers) BEFORE the
        # orchestrator is built: Memory.attach reads the context-dump flag.
        ar = self._audit_begin(task, url=url, mode=mode, timeout=timeout, audit_task=audit_task, seed=audit_seed)
        try:
            orch, framed, classification = await self._build_inprocess(
                task,
                mode=mode,
                url=url,
                output_schema=output_schema,
                force_browser=force_browser,
                enable_human_handoff=enable_human_handoff,
            )
        except BaseException as exc:
            self._audit_abort(ar, exc)
            raise

        from superbrowser_bridge.usage import (
            UsageHook,
            pop,
            snapshot,
            track_task,
            write_usage_json,
        )

        handle = task_handle or lifecycle.new_handle()
        lifecycle.register(handle, task, transport="in-process")

        hooks: list[Any] = [orch.hook, UsageHook("orchestrator")]
        effective = None
        if ar is not None:
            effective = await self._audit_after_build(ar, orch, framed, mode)
            hooks.append(self._audit_hook(ar, orch))

        text, raw, error, success = "", "", None, False
        cancelled_by_client = False
        stop_reason = "ok"
        usage = None
        try:
            with track_task(orch.task_id):
                lifecycle.note_orch_task_id(handle, orch.task_id)
                # The watcher converts a cancel() from another thread into a
                # plain asyncio cancellation of the run task.
                run_task = asyncio.ensure_future(
                    run_and_capture(
                        orch.bot,
                        framed,
                        orch.session_key,
                        hooks=hooks,
                        timeout=timeout,
                    )
                )
                watcher = asyncio.ensure_future(_watch_cancel(handle, run_task))
                try:
                    text, raw = await run_task
                finally:
                    watcher.cancel()
            success = bool(text)
            if not success and error is None:
                error = "the agent returned no answer"
        except asyncio.TimeoutError:
            error = f"task timed out after {timeout}s"
            stop_reason = "timeout"
        except asyncio.CancelledError:
            stop_reason = "cancelled"
            if not lifecycle.cancelled(handle):
                lifecycle.finish(handle, "cancelled")
                raise  # the caller cancelled arun itself — propagate
            cancelled_by_client = True
            error = "cancelled by client"
        except Exception as exc:  # noqa: BLE001 - surface in the result, don't crash
            error = f"{type(exc).__name__}: {exc}"
            stop_reason = "error"
        finally:
            try:
                orch.memory.write_task_summary(success=success)
            except Exception:  # noqa: BLE001 - best-effort summary
                pass
            lifecycle.finish(
                handle,
                "cancelled" if cancelled_by_client else ("done" if success else "error"),
            )
            # Aggregate per-task token usage (orchestrator + worker(s) + vision),
            # persist it, then drop the registry entry. Best-effort — never fail
            # the run. Done in the finally so the audit trail sees the tokens of a
            # run that timed out or was interrupted.
            usage = snapshot(orch.task_id)
            if usage is not None:
                write_usage_json(usage)
            pop(orch.task_id)
            if ar is not None:
                self._audit_finish(
                    ar, final_answer=text, raw_content=raw, stop_reason=stop_reason, error=error,
                    usage=usage.to_dict() if usage is not None else None, orch_task_id=orch.task_id,
                    framed_task=framed, effective=effective,
                )

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
            input_tokens=usage.input_tokens if usage is not None else 0,
            output_tokens=usage.output_tokens if usage is not None else 0,
            total_tokens=usage.total_tokens if usage is not None else 0,
            usage=usage.to_dict() if usage is not None else None,
            task_handle=handle,
            cancelled=cancelled_by_client,
            audit_dir=str(ar.run_dir) if ar is not None else None,
        )

    # ----- audit trail (opt-in; see superbrowser_bridge.audit) -----

    def _audit_begin(self, task: str, *, url: str | None, mode: str, timeout: float | None,
                     audit_task: dict[str, Any] | None, seed: int):
        if self._audit is None:
            return None
        try:
            row = dict(audit_task or {})
            row.setdefault("instruction", task)
            if url and not row.get("start_url"):
                row["start_url"] = url
            ar = self._audit.begin(task=row, seed=seed, model=self.model, timeout=timeout,
                                   server_url=self.server_url, mode=mode)
            ar.apply_env()
            return ar
        except Exception as exc:  # noqa: BLE001 - the recorder must never block a run
            try:
                from loguru import logger

                logger.warning("audit trail disabled for this run: {}", exc)
            except Exception:  # noqa: BLE001
                pass
            return None

    async def _audit_after_build(self, ar, orch, framed: str, mode: str):
        from superbrowser_bridge.audit import redact_config

        effective = None
        try:
            ar.write_preliminary_meta(orch.task_id, framed)
            _, effective = redact_config(ar.run_dir)
            if self.model:
                effective = dict(effective or {}, model=self.model)
            health = await self._server.health_payload() if mode == "browser" else None
            ar.write_manifest(server_url=self.server_url, health=health, effective_defaults=effective,
                              extra={"sdk": {"mode": mode, "auto_start_server": self.auto_start_server,
                                             "model_override": self.model}})
        except Exception:  # noqa: BLE001
            pass
        return effective

    @staticmethod
    def _audit_hook(ar, orch):
        from superbrowser_bridge.audit import AuditHook

        return AuditHook("orchestrator", memory=orch.memory, task_id=orch.task_id, roster_path=ar.roster_path)

    @staticmethod
    def _audit_finish(ar, *, final_answer: str, raw_content: str, stop_reason: str, error: str | None,
                      usage: dict[str, Any] | None, orch_task_id: str | None, framed_task: str | None,
                      effective: dict[str, Any] | None) -> None:
        try:
            ar.finish(final_answer=final_answer, raw_content=raw_content, stop_reason=stop_reason, error=error,
                      usage=usage, orch_task_id=orch_task_id, framed_task=framed_task, effective_defaults=effective)
        finally:
            ar.restore_env()

    def _audit_abort(self, ar, exc: BaseException) -> None:
        if ar is None:
            return
        self._audit_finish(ar, final_answer="", raw_content="", stop_reason="error",
                           error=f"{type(exc).__name__}: {exc}", usage=None, orch_task_id=None,
                           framed_task=None, effective=None)

    # ----- streaming (progress / step events) -----

    async def _build_inprocess(
        self,
        task: str,
        *,
        mode: Mode,
        url: str | None,
        output_schema: Any | None,
        force_browser: bool,
        enable_human_handoff: bool,
    ):
        """Classify, ensure the engine when needed, build the orchestrator, and
        frame the task. Shared by :meth:`arun` and :meth:`astream` in-process."""
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
        return orch, framed, classification

    async def astream(
        self,
        task: str,
        *,
        mode: Mode = "auto",
        url: str | None = None,
        output_schema: Any | None = None,
        force_browser: bool = False,
        enable_human_handoff: bool = True,
        timeout: float | None = None,
        task_handle: str | None = None,
        audit_task: dict[str, Any] | None = None,
        audit_seed: int = 0,
    ) -> AsyncIterator[dict]:
        """Stream a task as step-level events, ending with a ``result`` event.

        Each yielded item is a JSON-serializable dict with a ``type``:
        ``classification`` / ``status`` / ``thinking`` / ``tool`` / ``tool_hint``
        / ``message`` for progress, then a final ``{"type": "result", ...}``
        mirroring :class:`RunResult`. Works in remote, local-agent, and
        in-process modes. ``task_handle`` registers the run for
        :meth:`cancel` / :meth:`tasks` (in-process and local-agent modes).
        """
        if self.remote:
            async for ev in self._astream_via_client(self._remote_stream_client, task, mode, url):
                yield ev
            return
        if self.local_agent:
            async for ev in self._astream_via_client(
                self._local_stream_client, task, mode, url,
                timeout=timeout, task_handle=task_handle,
            ):
                yield ev
            return

        ar = self._audit_begin(task, url=url, mode=mode, timeout=timeout, audit_task=audit_task, seed=audit_seed)
        try:
            orch, framed, classification = await self._build_inprocess(
                task,
                mode=mode,
                url=url,
                output_schema=output_schema,
                force_browser=force_browser,
                enable_human_handoff=enable_human_handoff,
            )
        except BaseException as exc:
            self._audit_abort(ar, exc)
            raise
        if classification is not None:
            yield {"type": "classification", "classification": classification}

        from superbrowser_bridge.usage import (
            UsageHook,
            pop,
            snapshot,
            track_task,
            write_usage_json,
        )

        handle = task_handle or lifecycle.new_handle()
        lifecycle.register(handle, task, transport="in-process")

        hooks: list[Any] = [orch.hook, UsageHook("orchestrator")]
        effective = None
        if ar is not None:
            effective = await self._audit_after_build(ar, orch, framed, mode)
            hooks.append(self._audit_hook(ar, orch))

        final: dict | None = None
        cancelled_by_client = False
        agen = None
        usage = None
        stop_reason = "ok"
        try:
            with track_task(orch.task_id):
                lifecycle.note_orch_task_id(handle, orch.task_id)
                agen = stream_and_capture(
                    orch.bot,
                    framed,
                    orch.session_key,
                    hooks=hooks,
                    timeout=timeout,
                )
                async for ev in agen:
                    if lifecycle.cancelled(handle):
                        cancelled_by_client = True
                        break
                    if ev.get("type") == "result":
                        final = ev
                    else:
                        yield ev
        except BaseException:
            stop_reason = "error"
            raise
        finally:
            # ALWAYS close the inner generator — this is the deterministic
            # cancel point. stream_and_capture's finally does run.cancel() +
            # await run, stopping the browser task. Closing must happen on
            # EVERY exit path: an internal cancel (cancelled_by_client), a
            # normal finish (no-op on an exhausted generator), AND — critically
            # — an external GeneratorExit from a consumer calling gen.close()
            # (how deploy/main.py's cooperative cancel unwinds the run). If we
            # only closed on cancelled_by_client, the external-close path would
            # leave the run task to non-deterministic GC and the browser task
            # could keep running.
            if agen is not None:
                try:
                    await agen.aclose()
                except Exception:  # noqa: BLE001 - already unwinding
                    pass
            try:
                orch.memory.write_task_summary(success=bool(final and final.get("success")))
            except Exception:  # noqa: BLE001 - best-effort summary
                pass
            lifecycle.finish(
                handle,
                "cancelled"
                if cancelled_by_client
                else ("done" if final and final.get("success") else "error"),
            )
            usage = snapshot(orch.task_id)
            if usage is not None:
                write_usage_json(usage)
            pop(orch.task_id)
            if ar is not None:
                if cancelled_by_client:
                    stop_reason = "cancelled"
                elif final is not None and "timed out" in str(final.get("error") or ""):
                    stop_reason = "timeout"
                self._audit_finish(
                    ar, final_answer=(final or {}).get("text", "") or "", raw_content=(final or {}).get("raw_content", "") or "",
                    stop_reason=stop_reason, error=(final or {}).get("error") if final else ("cancelled by client" if cancelled_by_client else None),
                    usage=usage.to_dict() if usage is not None else None, orch_task_id=orch.task_id,
                    framed_task=framed, effective=effective,
                )

        if cancelled_by_client:
            final = {
                "type": "result", "text": "", "raw_content": "",
                "success": False, "error": "cancelled by client",
            }
        final = final or {
            "type": "result", "text": "", "raw_content": "",
            "success": False, "error": "the agent returned no answer",
        }
        text = final.get("text", "") or ""
        success = bool(final.get("success"))
        data = parse_output(text, output_schema) if (success and output_schema is not None) else None
        yield {
            "type": "result",
            "text": text,
            "success": success,
            "task_id": orch.task_id,
            "mode": mode,
            "data": data,
            "error": final.get("error"),
            "raw_content": final.get("raw_content", text),
            "classification": classification,
            "input_tokens": usage.input_tokens if usage is not None else 0,
            "output_tokens": usage.output_tokens if usage is not None else 0,
            "total_tokens": usage.total_tokens if usage is not None else 0,
            "usage": usage.to_dict() if usage is not None else None,
            "task_handle": handle,
            "cancelled": cancelled_by_client,
            "audit_dir": str(ar.run_dir) if ar is not None else None,
        }

    def stream(
        self,
        task: str,
        *,
        mode: Mode = "auto",
        url: str | None = None,
        output_schema: Any | None = None,
        force_browser: bool = False,
        enable_human_handoff: bool = True,
        timeout: float | None = None,
        task_handle: str | None = None,
        audit_task: dict[str, Any] | None = None,
        audit_seed: int = 0,
    ) -> Iterator[dict]:
        """Synchronous streaming. Raises if called from a running event loop —
        use :meth:`astream` there. Yields the same events as :meth:`astream`."""
        # Remote / local-agent: iterate the runagent client's sync stream directly.
        if self.remote or self.local_agent:
            client = (self._remote_stream_client() if self.remote
                      else self._local_stream_client())
            input_kwargs: dict[str, Any] = {"task": task, "mode": mode}
            if url is not None:
                input_kwargs["url"] = url
            if timeout is not None:
                input_kwargs["timeout"] = timeout
            if self.local_agent and task_handle and "cancel" in self._server_entrypoints():
                input_kwargs["client_task_id"] = task_handle
            yield from client.run_stream(**input_kwargs)
            return
        # In-process: drive the async generator from a sync caller.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            yield from _drive_async_gen(
                lambda: self.astream(
                    task, mode=mode, url=url, output_schema=output_schema,
                    force_browser=force_browser,
                    enable_human_handoff=enable_human_handoff, timeout=timeout,
                    task_handle=task_handle, audit_task=audit_task, audit_seed=audit_seed,
                )
            )
            return
        raise RuntimeError(
            "SuperBrowser.stream() cannot be called from inside a running event "
            "loop; use `async for ev in SuperBrowser.astream(...)` instead."
        )

    async def _astream_via_client(
        self,
        client_factory,
        task: str,
        mode: str,
        url: str | None,
        *,
        timeout: float | None = None,
        task_handle: str | None = None,
    ):
        """Bridge a runagent ``RunAgentClient`` sync streaming generator to async
        by stepping it in the default executor (the socket I/O is blocking)."""
        client = client_factory()
        input_kwargs: dict[str, Any] = {"task": task, "mode": mode}
        if url is not None:
            input_kwargs["url"] = url
        if timeout is not None:
            input_kwargs["timeout"] = timeout
        if self.local_agent and task_handle and "cancel" in self._server_entrypoints():
            input_kwargs["client_task_id"] = task_handle
        loop = asyncio.get_running_loop()
        done = object()
        iterator = await loop.run_in_executor(None, lambda: client.run_stream(**input_kwargs))
        try:
            while True:
                chunk = await loop.run_in_executor(None, lambda: next(iterator, done))
                if chunk is done:
                    break
                yield chunk
        finally:
            # aclose()/GC of this generator closes the socket iterator, which
            # drops the WS and lets the server unwind the task.
            try:
                iterator.close()
            except Exception:  # noqa: BLE001 - already unwinding
                pass

    def _remote_stream_client(self):
        if self._remote_stream_client_obj is None:
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
            self._remote_stream_client_obj = RunAgentClient(
                agent_id=self.agent_id,
                entrypoint_tag="run_stream",
                local=False,
                user_id=self.user_id,
                persistent_memory=self.persistent,
                api_key=self.api_key,
                base_url=self.base_url,
            )
        return self._remote_stream_client_obj

    def _local_stream_client(self):
        if self._local_stream_client_obj is None:
            try:
                from runagent import RunAgentClient
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise ImportError(
                    "Local-agent mode needs the runagent SDK. Install it with "
                    "`pip install 'runagent-superbrowser[remote]'` (or `pip install runagent`)."
                ) from exc
            host, port = _split_url(self.local_agent_url or "")
            self._local_stream_client_obj = RunAgentClient(
                agent_id=self.local_agent_id,
                entrypoint_tag="run_stream",
                local=True,
                host=host,
                port=port,
                user_id=self.user_id,
                persistent_memory=self.persistent,
            )
        return self._local_stream_client_obj

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
        if timeout is not None:
            # main.py:run has accepted `timeout` since the first release —
            # safe to forward unconditionally.
            input_kwargs["timeout"] = timeout
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
        task_handle: str | None = None,
        on_event: Callable[[dict], None] | None = None,
    ) -> RunResult:
        """Execute against a local ``runagent serve`` agent server (the all-in-one
        Docker container) via ``RunAgentClient(local=True)``. No API key required.

        Default transport is the run_stream WebSocket (see
        :meth:`_run_local_agent_via_stream`) — a dying client drops the socket
        and the server unwinds the task, so force-killed SDK processes no
        longer orphan work inside Docker. ``SUPERBROWSER_RUN_TRANSPORT=rest``
        restores the classic HTTP path (documented residual risk: SIGKILL of a
        REST client leaves the task running to completion).

        Unlike remote mode, ``output_schema`` IS parsed locally here — we own both
        ends of the round-trip and the engine returns the answer text.
        """
        transport = (os.environ.get("SUPERBROWSER_RUN_TRANSPORT") or "stream").strip().lower()
        if transport != "rest":
            return self._run_local_agent_via_stream(
                task,
                mode=mode,
                url=url,
                output_schema=output_schema,
                timeout=timeout,
                task_handle=task_handle,
                on_event=on_event,
            )

        client = self._local_runagent_client()
        handle = task_handle or lifecycle.new_handle()
        supports_cancel = "cancel" in self._server_entrypoints()
        input_kwargs: dict[str, Any] = {"task": task, "mode": mode}
        if url is not None:
            input_kwargs["url"] = url
        if timeout is not None:
            input_kwargs["timeout"] = timeout
        if supports_cancel:
            # Old containers would TypeError on the unknown kwarg — only send
            # it when the architecture probe shows the cancel entrypoint.
            input_kwargs["client_task_id"] = handle
            lifecycle.register(handle, task, transport="rest-local")
            from . import _reaper

            _reaper.install(self)
        try:
            payload = client.run(**input_kwargs)
        except Exception as exc:  # noqa: BLE001 - surface in the result, don't crash
            if supports_cancel:
                lifecycle.finish(handle, "error")
            return RunResult(
                text="",
                success=False,
                task_id="",
                mode=mode,
                data=None,
                error=f"{type(exc).__name__}: {exc}",
                raw_content="",
                classification=None,
                task_handle=handle if supports_cancel else "",
            )
        if supports_cancel:
            lifecycle.finish(handle, "done")
        result = self._result_from_remote(payload, mode)
        if supports_cancel and not result.task_handle:
            result.task_handle = handle
        if output_schema is not None and result.success and result.data is None:
            result.data = parse_output(result.text, output_schema)
        return result

    def _run_local_agent_via_stream(
        self,
        task: str,
        *,
        mode: Mode = "auto",
        url: str | None = None,
        output_schema: Any | None = None,
        timeout: float | None = None,
        task_handle: str | None = None,
        on_event: Callable[[dict], None] | None = None,
    ) -> RunResult:
        """Default local-agent transport: drive ``run_stream`` and aggregate the
        final ``result`` event.

        Why streaming for a blocking ``run()``: the WebSocket is the lifeline.
        SIGKILL/crash of this process drops the socket; the server notices on
        its next send and cancels the underlying task (verified chain:
        socket_utils WebSocketDisconnect → async-gen close → _capture cancel).
        The client-side watchdog (`timeout + 30s`) is a backstop on top of the
        server's own asyncio.wait_for; it only advances between events, which
        arrive every LLM step.
        """
        client = self._local_stream_client()
        handle = task_handle or lifecycle.new_handle()
        supports_cancel = "cancel" in self._server_entrypoints()
        input_kwargs: dict[str, Any] = {"task": task, "mode": mode}
        if url is not None:
            input_kwargs["url"] = url
        if timeout is not None:
            input_kwargs["timeout"] = timeout
        if supports_cancel:
            input_kwargs["client_task_id"] = handle
        lifecycle.register(handle, task, transport="stream-local")

        deadline = (time.monotonic() + timeout + 30.0) if timeout else None
        final: dict | None = None
        error: str | None = None
        iterator = None
        try:
            iterator = client.run_stream(**input_kwargs)
            for ev in iterator:
                if isinstance(ev, dict):
                    if ev.get("type") == "result":
                        final = ev
                    elif on_event is not None:
                        try:
                            on_event(ev)
                        except Exception:  # noqa: BLE001 - observer must not kill the run
                            pass
                if deadline is not None and time.monotonic() > deadline:
                    error = f"task timed out after {timeout}s (client watchdog)"
                    break
        except Exception as exc:  # noqa: BLE001 - surface in the result, don't crash
            error = f"{type(exc).__name__}: {exc}"
        finally:
            if iterator is not None:
                # Closing the iterator closes the WS; if the task is still
                # running server-side, that unwinds it.
                try:
                    iterator.close()
                except Exception:  # noqa: BLE001
                    pass
            if final is not None and final.get("cancelled"):
                lifecycle.finish(handle, "cancelled")
            else:
                lifecycle.finish(handle, "error" if (error and final is None) else "done")

        if final is None:
            return RunResult(
                text="",
                success=False,
                task_id="",
                mode=mode,
                data=None,
                error=error or "the agent returned no result",
                raw_content="",
                classification=None,
                task_handle=handle,
            )
        payload = {k: v for k, v in final.items() if k != "type"}
        result = self._result_from_remote(payload, mode)
        if not result.task_handle:
            result.task_handle = handle
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

    # ----- task lifecycle (cancel / list) -----

    def _server_entrypoints(self) -> set[str]:
        """Entrypoint tags exposed by the local agent server, from its
        ``/architecture`` endpoint. Cached for the client's lifetime; empty on
        any failure so callers degrade to the pre-cancel behavior (and never
        send ``client_task_id`` to an old container that would TypeError)."""
        if self._entrypoints_cache is not None:
            return self._entrypoints_cache
        tags: set[str] = set()
        try:
            import json as _json
            import urllib.request

            host, port = _split_url(self.local_agent_url or "")
            url = f"http://{host}:{port}/api/v1/agents/{self.local_agent_id}/architecture"
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = _json.load(resp)
            for entry in (data.get("data") or {}).get("entrypoints") or []:
                tag = entry.get("tag")
                if tag:
                    tags.add(str(tag))
        except Exception:  # noqa: BLE001 - unreachable/old server -> degrade
            pass
        self._entrypoints_cache = tags
        return tags

    def _entrypoint_client(self, tag: str):
        """A cached RunAgentClient for an auxiliary local entrypoint
        (``cancel`` / ``tasks``)."""
        cached = self._aux_clients.get(tag)
        if cached is not None:
            return cached
        from runagent import RunAgentClient

        host, port = _split_url(self.local_agent_url or "")
        client = RunAgentClient(
            agent_id=self.local_agent_id,
            entrypoint_tag=tag,
            local=True,
            host=host,
            port=port,
            user_id=self.user_id,
            persistent_memory=self.persistent,
        )
        self._aux_clients[tag] = client
        return client

    def cancel(self, task_handle: str) -> bool:
        """Request cooperative cancellation of a running task.

        - in-process: flips the local registry's cancel event; the run unwinds
          within ~a poll tick / next stream event.
        - local-agent (Docker): calls the server's ``cancel`` entrypoint
          (new images only — returns False against an old container).
        - remote: unsupported in v1 — returns False.

        Returns True when a live task matched.
        """
        if not task_handle:
            return False
        if self.remote:
            return False
        if self.local_agent:
            if "cancel" not in self._server_entrypoints():
                return False
            try:
                payload = self._entrypoint_client("cancel").run(client_task_id=task_handle)
            except Exception:  # noqa: BLE001 - server gone -> nothing to cancel
                return False
            return bool(isinstance(payload, dict) and payload.get("cancelled"))
        return lifecycle.request_cancel(task_handle)

    def tasks(self) -> list[dict[str, Any]]:
        """List known tasks (running first).

        Local-agent mode queries the server's ``tasks`` entrypoint (the
        authoritative registry lives in the server process); everything else
        reads the process-local registry.
        """
        if self.local_agent and "tasks" in self._server_entrypoints():
            try:
                payload = self._entrypoint_client("tasks").run()
            except Exception:  # noqa: BLE001 - fall back to the local view
                return lifecycle.list_tasks()
            if isinstance(payload, dict) and isinstance(payload.get("tasks"), list):
                return payload["tasks"]
            return []
        return lifecycle.list_tasks()

    @staticmethod
    def _result_from_remote(payload: Any, mode: str) -> RunResult:
        """Wrap the in-VM ``main.py:run`` dict (already deserialized by
        RunAgentClient) into a RunResult."""
        if isinstance(payload, dict):
            text = payload.get("text", "") or ""
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
            return RunResult(
                text=text,
                success=bool(payload.get("success", bool(text))),
                task_id=payload.get("task_id") or "",
                mode=payload.get("mode") or mode,
                data=payload.get("data"),
                error=payload.get("error"),
                raw_content=text,
                classification=payload.get("classification"),
                input_tokens=int(payload.get("input_tokens") or (usage or {}).get("input_tokens") or 0),
                output_tokens=int(payload.get("output_tokens") or (usage or {}).get("output_tokens") or 0),
                total_tokens=int(payload.get("total_tokens") or (usage or {}).get("total_tokens") or 0),
                usage=usage,
                task_handle=str(payload.get("task_handle") or ""),
                cancelled=bool(payload.get("cancelled")),
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
