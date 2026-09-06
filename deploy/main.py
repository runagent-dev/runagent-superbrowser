"""RunAgent SuperBrowser — serverless agent entrypoint.

This module exposes the agent's ``run`` / ``run_stream`` entrypoints. The same
file is used in three places (kept byte-identical): baked into the serverless VM
at /root/main.py, the all-in-one Docker container's deploy/main.py, and the
``runagent init --from-template superbrowser/default`` scaffold. The vsock runner
/ ``runagent serve`` loads it and invokes ``run`` per request. It drives the
superbrowser engine started on 127.0.0.1:3100, with per-user cookies/profiles
under ~/.superbrowser and nanobot state under ~/.nanobot (persisted on the
/persistent disk in the VM).

An LLM key (OPENAI_API_KEY / ANTHROPIC_API_KEY, or the LLM_PROVIDER / LLM_MODEL /
LLM_API_KEY contract) must be present in the environment — set it in this
directory's .env (uploaded by `runagent deploy`, loaded by docker-compose, or
written to the VM's /root/.env). See .env.example. The nanobot brain is then
configured from that env by ``ensure_nanobot_config`` (the _nanobot_config bridge).
"""
import asyncio
import os
import time
import urllib.request

ENGINE_URL = os.environ.get("SUPERBROWSER_URL", "http://127.0.0.1:3100")

_sb = None
_config_ready = False


def _ensure_nanobot_config() -> None:
    """Write the env's LLM choice into ~/.nanobot/config.json once.

    runagent-serverless / .env deliver the LLM config as env vars (LLM_PROVIDER /
    LLM_MODEL / LLM_API_KEY / LLM_BASE_URL); nanobot reads it only from its config
    file. The ``_nanobot_config`` bridge reconciles the two. Prefer the installed
    SDK's copy (the single source of truth); fall back to the sibling baked next
    to this file. Runs once per process, before the first nanobot build.
    """
    global _config_ready
    if _config_ready:
        return
    try:
        try:
            from runagent_superbrowser._nanobot_config import ensure_nanobot_config
        except Exception:  # noqa: BLE001 - fall back to the sibling copy
            from _nanobot_config import ensure_nanobot_config

        ensure_nanobot_config()
    except Exception:  # noqa: BLE001 - best effort; never block a run
        pass
    _config_ready = True


def _wait_for_engine(timeout: float = 90.0) -> None:
    """Block until the local engine answers /health (covers cold boot)."""
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{ENGINE_URL}/health", timeout=3) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001 - engine not up yet
            last_err = exc
        time.sleep(1.0)
    raise RuntimeError(f"superbrowser engine not ready at {ENGINE_URL}: {last_err}")


def _client():
    global _sb
    if _sb is None:
        # Imported lazily so module load (at vsock-runner start) never races the
        # not-yet-ready engine.
        from runagent_superbrowser import SuperBrowser

        _ensure_nanobot_config()
        _wait_for_engine()
        # model is also written into ~/.nanobot/config.json by _ensure_nanobot_config;
        # passing it here covers the in-process model-override path on newer SDKs.
        try:
            _sb = SuperBrowser(server_url=ENGINE_URL, model=os.environ.get("LLM_MODEL") or None)
        except TypeError:
            # Older SDK without a model kwarg.
            _sb = SuperBrowser(server_url=ENGINE_URL)
    return _sb


def _has_llm_credentials() -> bool:
    return bool(
        os.environ.get("LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.path.exists(os.path.expanduser("~/.nanobot/config.json"))
    )


def _lifecycle():
    """The SDK's process-local task registry, or None on an old SDK.

    ``runagent serve`` runs every entrypoint in this one process, so the
    registry a ``run``/``run_stream`` call registers into is the same module
    state the ``cancel``/``tasks`` entrypoints consult.
    """
    try:
        from runagent_superbrowser import lifecycle

        return lifecycle
    except Exception:  # noqa: BLE001 - old SDK without lifecycle support
        return None


def _cancelled_payload(mode, client_task_id):
    return {
        "text": "",
        "success": False,
        "data": None,
        "error": "cancelled by client",
        "cancelled": True,
        "task_id": None,
        "task_handle": client_task_id,
        "mode": mode,
        "classification": None,
    }


def run(task, mode="auto", url=None, output_schema=None, timeout=None, client_task_id=None):
    """Run a browser task and return a JSON-serializable RunResult dict.

    ``client_task_id`` (optional, sent only by SDKs that probed this server's
    ``cancel`` entrypoint): registers the run in the lifecycle registry and
    drives it through the streaming path, checking for a cooperative cancel
    between events. When absent — every old SDK — behavior is byte-identical
    to the classic path below.
    """
    if not _has_llm_credentials():
        return {
            "text": "",
            "success": False,
            "data": None,
            "error": (
                "No LLM credentials found. Set OPENAI_API_KEY or ANTHROPIC_API_KEY "
                "(plus LLM_MODEL) in the agent's .env at deploy time."
            ),
            "task_id": None,
            "mode": mode,
            "classification": None,
        }

    lc = _lifecycle() if client_task_id else None
    if lc is not None:
        lc.register(client_task_id, task, transport="serve-run")
        gen = _client().stream(task, mode=mode, url=url, output_schema=output_schema, timeout=timeout)
        final = None
        try:
            for event in gen:
                if lc.cancelled(client_task_id):
                    lc.finish(client_task_id, "cancelled")
                    return _cancelled_payload(mode, client_task_id)
                if isinstance(event, dict) and event.get("type") == "result":
                    final = event
        finally:
            # Closing the generator unwinds the in-process run (astream's
            # finally cancels the underlying task) — nothing keeps running.
            try:
                gen.close()
            except Exception:  # noqa: BLE001
                pass
        lc.finish(client_task_id, "done" if (final and final.get("success")) else "error")
        if final is None:
            payload = _cancelled_payload(mode, client_task_id)
            payload["cancelled"] = False
            payload["error"] = "the agent returned no result"
            return payload
        payload = {k: v for k, v in final.items() if k != "type"}
        payload.setdefault("mode", mode)
        payload["task_handle"] = client_task_id
        return payload

    result = _client().run(
        task,
        mode=mode,
        url=url,
        output_schema=output_schema,
        timeout=timeout,
    )

    data = getattr(result, "data", None)
    if hasattr(data, "model_dump"):
        data = data.model_dump()

    return {
        "text": getattr(result, "text", "") or "",
        "success": bool(getattr(result, "success", False)),
        "data": data,
        "error": getattr(result, "error", None),
        "task_id": getattr(result, "task_id", None),
        "mode": getattr(result, "mode", mode),
        "classification": getattr(result, "classification", None),
    }


def cancel(client_task_id):
    """Cooperatively cancel a running task registered under ``client_task_id``.

    Returns ``{"supported": bool, "cancelled": bool}`` — ``supported=False``
    means the installed SDK predates the lifecycle registry.
    """
    lc = _lifecycle()
    if lc is None:
        return {"supported": False, "cancelled": False}
    return {"supported": True, "cancelled": lc.request_cancel(client_task_id)}


def tasks():
    """List tasks known to this server process (running first)."""
    lc = _lifecycle()
    if lc is None:
        return {"supported": False, "tasks": []}
    return {"supported": True, "tasks": lc.list_tasks()}


async def run_stream(task, mode="auto", url=None, output_schema=None, timeout=None, client_task_id=None):
    """Stream a browser task as step-level events, ending with a result event.

    Yields JSON-serializable dicts (see ``SuperBrowser.astream``): progress events
    (classification / status / thinking / tool / message) followed by a final
    {"type": "result", ...} matching the ``run`` payload. The vsock runner
    serializes each yielded item and frames the stream over the WebSocket back to
    the SDK.

    ``client_task_id`` (optional) registers the run for the ``cancel``/``tasks``
    entrypoints; the loop checks for a cooperative cancel between events. A
    client disconnect (SIGKILL included) closes this generator, which unwinds
    the task regardless — client_task_id only adds the *explicit* cancel path.

    Degrades gracefully: if the installed runagent_superbrowser SDK predates
    streaming (no ``astream``), this runs the task and yields a single result.
    """
    if not _has_llm_credentials():
        yield {
            "type": "result",
            "text": "",
            "success": False,
            "data": None,
            "error": (
                "No LLM credentials found. Set OPENAI_API_KEY or ANTHROPIC_API_KEY "
                "(plus LLM_MODEL) in the agent's .env at deploy time."
            ),
            "task_id": None,
            "mode": mode,
            "classification": None,
        }
        return

    # _client() blocks while waiting for the engine; keep it off the event loop.
    loop = asyncio.get_running_loop()
    sb = await loop.run_in_executor(None, _client)

    lc = _lifecycle() if client_task_id else None
    if lc is not None:
        lc.register(client_task_id, task, transport="serve-stream")

    try:
        if hasattr(sb, "astream"):
            agen = sb.astream(task, mode=mode, url=url, output_schema=output_schema, timeout=timeout)
            try:
                async for event in agen:
                    if lc is not None and lc.cancelled(client_task_id):
                        lc.finish(client_task_id, "cancelled")
                        yield {"type": "result", **_cancelled_payload(mode, client_task_id)}
                        return
                    yield event
            finally:
                # Deterministic cancel point: closing the inner generator runs
                # its finally (run.cancel() + await run), stopping the browser
                # task. This fires on every exit path — normal finish, cooperative
                # cancel, and a client-disconnect GeneratorExit. It must be
                # bullet-proof: an aclose() that raised (e.g. a contextvars token
                # reset in a foreign Context) would escape as an unretrieved task
                # exception and corrupt teardown. The run is already unwinding, so
                # swallow any non-cancellation error and let the original unwind
                # (GeneratorExit / CancelledError, which are BaseExceptions) win.
                try:
                    await agen.aclose()
                except Exception:  # noqa: BLE001 - teardown must never surface
                    pass
        else:
            # Old SDK without streaming: degrade to a single result event.
            result = await loop.run_in_executor(
                None,
                lambda: run(task, mode=mode, url=url, output_schema=output_schema, timeout=timeout),
            )
            result = dict(result)
            result["type"] = "result"
            yield result
    finally:
        if lc is not None:
            lc.finish(client_task_id, "done")
