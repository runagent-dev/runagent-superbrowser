"""Live pre-flight for the eval harness: verify everything a sweep needs.

``eval.rehearse`` is the OFFLINE pre-flight (schedules + replay, no network).
This one touches the real services, deliberately cheaply: a one-token chat per
distinct LLM endpoint, one browser session opened and closed, no benchmark task
run. It exists because the expensive failure mode is a sweep that runs for hours
and only then reveals that the judge was pointed at the wrong endpoint.

    python -m eval.preflight                 # check everything
    python -m eval.preflight --model google/gemini-3.5-flash
    python -m eval.preflight --skip-llm      # config + server only, zero cost

Exit code 0 = ready to launch, 1 = at least one blocking problem.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from eval import _bootstrap  # noqa: F401  (sys.path + .env)
from eval._bootstrap import DEFAULT_CONFIG_PATH, REPO_ROOT
from eval.core import server
from eval.core.protocol import DEFAULT_PROTOCOL
from eval.core.tasks import load_benchmark

OK, WARN, BAD = "ok", "warn", "FAIL"


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []

    def add(self, status: str, area: str, detail: str, fix: str = "") -> None:
        self.rows.append((status, area, detail, fix))

    @property
    def failed(self) -> bool:
        return any(r[0] == BAD for r in self.rows)

    def render(self) -> str:
        width = max(len(r[1]) for r in self.rows) if self.rows else 10
        out = []
        for status, area, detail, fix in self.rows:
            mark = {OK: "  ok  ", WARN: " warn ", BAD: " FAIL "}[status]
            out.append(f"[{mark}] {area.ljust(width)}  {detail}")
            if fix and status != OK:
                out.append(f"{' ' * (width + 11)}-> {fix}")
        return "\n".join(out)


# ----------------------------------------------------------------- checks
def check_config(rep: Report, model_override: str | None) -> dict[str, Any]:
    """The brain: model, provider, and a usable key for that provider."""
    path = Path(DEFAULT_CONFIG_PATH)
    if not path.exists():
        rep.add(BAD, "nanobot config", f"{path} not found",
                "run `nanobot onboard` or copy a config there")
        return {}
    data = json.loads(path.read_text())
    defaults = data.get("agents", {}).get("defaults", {})
    model = model_override or defaults.get("model")
    provider = defaults.get("provider")
    rep.add(OK if model else BAD, "brain model",
            f"{model or '(unset)'} via {provider or '(unset)'}"
            + (" (--model override)" if model_override else " (from ~/.nanobot/config.json)"),
            "pass --model or set agents.defaults.model")
    if model and (":free" in model or "free" in str(model).split("/")[-1]):
        rep.add(WARN, "brain model", "a ':free' model is rate-limited and usually not paper-grade",
                "pin a paid model id for the study, e.g. --model google/gemini-3.5-flash")
    key = (data.get("providers", {}).get(provider or "", {}) or {}).get("apiKey")
    rep.add(OK if key else BAD, "brain key",
            f"providers.{provider}.apiKey present" if key else f"providers.{provider}.apiKey is empty",
            f"set the {provider} key in ~/.nanobot/config.json")
    p = DEFAULT_PROTOCOL
    rep.add(OK, "protocol pins",
            f"maxToolIterations={p.max_iterations} contextWindowTokens={p.context_window_tokens} "
            f"maxTokens={p.max_tokens} temperature={p.temperature} (patched per run, your config is not edited)")
    if p.context_window_tokens - p.max_tokens - 1024 <= 0:
        rep.add(WARN, "protocol pins", "host history-snip is inactive under these pins")
    return {"model": model, "provider": provider}


async def check_credits(rep: Report, cfg: dict[str, Any]) -> None:
    """Ask the provider what is left, when it can tell us."""
    if cfg.get("provider") != "openrouter":
        return
    try:
        import json as _json

        import httpx

        data = _json.loads(Path(DEFAULT_CONFIG_PATH).read_text())
        key = ((data.get("providers", {}) or {}).get("openrouter", {}) or {}).get("apiKey")
        if not key:
            return
        async with httpx.AsyncClient(timeout=20) as h:
            r = await h.get("https://openrouter.ai/api/v1/credits",
                            headers={"Authorization": f"Bearer {key}"})
        d = (r.json() or {}).get("data") or {}
        total, used = float(d.get("total_credits") or 0), float(d.get("total_usage") or 0)
        left = total - used
        status = OK if left > 20 else (WARN if left > 0 else BAD)
        rep.add(status, "credits", f"OpenRouter balance ${left:.2f} (granted ${total:.2f}, used ${used:.2f})",
                "top up before sweeping: with no balance every run fails instantly and is recorded as an "
                "excluded api_error, so the sweep produces no usable data" if left <= 0 else
                "a sweep costs several dollars per run; this will not finish" if status == WARN else "")
    except Exception:
        return


async def ping_brain(rep: Report, cfg: dict[str, Any]) -> None:
    """One tiny completion through the ACTUAL brain provider.

    The judges having credit says nothing about the brain's key. A sweep whose
    brain provider is out of quota finishes every run in about a second, and
    those runs look like ordinary task failures unless something checks first.
    """
    model, provider = cfg.get("model"), cfg.get("provider")
    if not model:
        return
    try:
        import json as _json

        from openai import AsyncOpenAI

        data = _json.loads(Path(DEFAULT_CONFIG_PATH).read_text())
        pc = (data.get("providers", {}) or {}).get(provider or "", {}) or {}
        key = pc.get("apiKey")
        base = pc.get("apiBase") or {"openrouter": "https://openrouter.ai/api/v1"}.get(provider)
        if not key:
            rep.add(BAD, "brain ping", f"no apiKey for provider {provider!r}")
            return
        client = AsyncOpenAI(api_key=key, **({"base_url": base} if base else {}))
        # Ping with the protocol's REAL max_tokens. Providers reserve credit for the
        # requested ceiling, so a tiny probe passes on an account whose balance
        # cannot fund an actual run — the check would then bless a sweep that
        # fails on every task.
        cap = DEFAULT_PROTOCOL.max_tokens
        resp = await client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": "Reply with: ok"}], max_tokens=cap)
        reply = ((resp.choices[0].message.content or "").strip() if resp.choices else "")
        rep.add(OK, "brain ping", f"{model} answered {reply[:20]!r} at the protocol's max_tokens={cap}")
    except Exception as exc:
        msg = str(exc)
        money = any(w in msg.lower() for w in ("quota", "billing", "credit", "arrears", "402", "payment"))
        rep.add(BAD, "brain ping", f"{model}: {type(exc).__name__}: {msg[:170]}",
                "the brain provider is refusing requests — top up or switch --model BEFORE sweeping; "
                "every run would otherwise finish in ~1s and be recorded as an excluded api_error"
                if money else "fix the model id / key / provider above before launching")


def check_vision(rep: Report) -> None:
    enabled = os.environ.get("VISION_ENABLED", "").strip() not in ("", "0", "false", "False")
    prov = os.environ.get("VISION_PROVIDER", "")
    vmodel = os.environ.get("VISION_MODEL", "")
    vkey = os.environ.get("VISION_API_KEY") or os.environ.get("VISION_GEMINI_API_KEY")
    if not enabled:
        rep.add(WARN, "vision", "VISION_ENABLED is off — the brain would pay image tokens (legacy path)",
                "set VISION_ENABLED=1 in .env for the paper's configuration")
        return
    rep.add(OK if vkey else BAD, "vision",
            f"{prov or '(no provider)'} / {vmodel or '(no model)'}, key {'set' if vkey else 'MISSING'}",
            "set VISION_API_KEY in .env (a key for VISION_PROVIDER, separate from the brain key)")


def check_judges(rep: Report) -> list[tuple[str, Any, str]]:
    """Resolve both judges and flag an endpoint/model mismatch before it costs a sweep."""
    from eval.core.judges.base import resolve_client

    clients: list[tuple[str, Any, str]] = []
    for name, model_env, default, prefix in (
        ("webjudge (primary)", "SUPERBROWSER_EVAL_WEBJUDGE_MODEL", "gpt-4o", "SUPERBROWSER_EVAL_WEBJUDGE"),
        ("answer_judge", "SUPERBROWSER_EVAL_JUDGE_MODEL", "gpt-5.5", "SUPERBROWSER_EVAL_ANSWER_JUDGE"),
    ):
        client, model = resolve_client(model_env=model_env, default_model=default, prefix=prefix)
        if client is None:
            rep.add(BAD, name, f"model {model} but no API key resolved",
                    f"set {prefix}_API_KEY (or OPENAI_API_KEY)")
            continue
        base = str(getattr(client, "base_url", "") or "https://api.openai.com/v1")
        rep.add(OK, name, f"model {model} at {base}")
        host_family = ("google" if "googleapis" in base else
                       "openrouter" if "openrouter" in base else
                       "openai" if "api.openai.com" in base else "custom")
        model_family = ("google" if model.startswith(("gemini", "google/")) else
                        "openai" if model.startswith(("gpt-", "o1", "o3", "o4", "openai/")) else
                        "anthropic" if model.startswith(("claude", "anthropic/")) else "other")
        if host_family in ("google", "openai") and model_family in ("google", "openai") and host_family != model_family:
            rep.add(BAD, name, f"model family '{model_family}' cannot be served by {host_family} endpoint {base}",
                    f"either set {model_env} to a {host_family} model, or give this judge its own "
                    f"{prefix}_API_KEY + {prefix}_BASE_URL")
        clients.append((name, client, model))
    return clients


async def ping_llm(rep: Report, clients: list[tuple[str, Any, str]]) -> None:
    """One tiny real completion per judge: proves model id + key + endpoint agree.

    The cap is deliberately not 1 token: a reasoning model spends its output
    budget on hidden reasoning first and errors out before writing anything,
    which would look like a broken judge.
    """
    for name, client, model in clients:
        try:
            kwargs: dict[str, Any] = {"model": model, "messages": [{"role": "user", "content": "Reply with: ok"}]}
            try:
                resp = await client.chat.completions.create(max_tokens=64, **kwargs)
            except Exception as exc:  # reasoning models reject max_tokens
                if "max_tokens" not in str(exc).lower():
                    raise
                resp = await client.chat.completions.create(max_completion_tokens=64, **kwargs)
            reply = ((resp.choices[0].message.content or "").strip() if resp.choices else "")
            rep.add(OK, f"{name} ping", f"{model} answered {reply[:20]!r}" if reply
                    else f"{model} reachable (empty reply; judge widens the cap at judge time)")
        except Exception as exc:
            rep.add(BAD, f"{name} ping", f"{model}: {type(exc).__name__}: {str(exc)[:160]}",
                    "fix the model id / key / base URL above before launching")


def check_server(rep: Report) -> str | None:
    base = os.environ.get("SUPERBROWSER_URL", server.BASE_URL)
    if not server.http_ok(server.health_url(base)):
        rep.add(BAD, "browser server", f"no healthy server at {base}",
                "run `npm run dev` (or `npm run build && npm start`) in another terminal")
        return None
    rep.add(OK, "browser server", f"healthy at {base}")
    return base


async def check_session(rep: Report, base: str) -> None:
    """Open and close one real session: proves Chrome, the profile and the token work."""
    import httpx

    token = os.environ.get("TOKEN") or os.environ.get("SUPERBROWSER_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with httpx.AsyncClient(timeout=90.0) as http:
            # no url: the server opens about:blank itself (passing it explicitly trips the SSRF guard)
            r = await http.post(f"{base}/session/create", json={}, headers=headers)
            r.raise_for_status()
            body = r.json() or {}
            sid = body.get("sessionId") or body.get("id")
            if not sid:
                rep.add(BAD, "browser session", f"POST /session/create returned no session id: {str(r.text)[:120]}")
                return
            try:
                rep.add(OK, "browser session", f"opened and closed {sid} (url={body.get('url', '?')})")
            finally:
                await http.delete(f"{base}/session/{sid}", headers=headers)
    except Exception as exc:
        rep.add(BAD, "browser session", f"{type(exc).__name__}: {str(exc)[:160]}",
                "check the server log; a 401 means TOKEN in .env does not match the server's")


def check_benchmark(rep: Report) -> None:
    try:
        tasks = load_benchmark(DEFAULT_PROTOCOL.benchmark, annotate=False)
        rep.add(OK, "benchmark", f"{DEFAULT_PROTOCOL.benchmark}: {len(tasks)} tasks")
    except Exception as exc:
        rep.add(BAD, "benchmark", f"{type(exc).__name__}: {str(exc)[:160]}",
                "eval/benchmarks/*.jsonl missing — see eval/README.md")
    subsets = REPO_ROOT / "eval" / "benchmarks" / "subsets.json"
    if subsets.exists():
        names = json.loads(subsets.read_text())
        shown = ", ".join(f"{k}({len(v.get('task_ids', []))} tasks)" for k, v in names.items() if isinstance(v, dict))
        rep.add(OK, "subsets", shown or "(none defined)")


def check_disk(rep: Report) -> None:
    import shutil as _sh

    free_gb = _sh.disk_usage(str(REPO_ROOT)).free / 1e9
    status = OK if free_gb > 20 else (WARN if free_gb > 5 else BAD)
    rep.add(status, "disk", f"{free_gb:.0f} GB free at the repo (screenshots + context dumps are the bulk)",
            "free space before a full sweep")


async def main_async(args: argparse.Namespace) -> int:
    rep = Report()
    cfg = check_config(rep, args.model)
    check_vision(rep)
    clients = check_judges(rep)
    check_benchmark(rep)
    check_disk(rep)
    base = check_server(rep)
    if not args.skip_llm:
        await check_credits(rep, cfg)
        await ping_brain(rep, cfg)
        if clients:
            await ping_llm(rep, clients)
    if base and not args.skip_session:
        await check_session(rep, base)
    print(rep.render())
    print()
    if rep.failed:
        print("NOT READY — fix the FAIL lines above, then re-run `python -m eval.preflight`.")
        return 1
    print("Ready. Next: python -m eval.experiments.e1_main.run --model <id> --tasks smoke2 --seeds 1")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Live pre-flight for the eval harness")
    ap.add_argument("--model", default=None, help="brain model id you intend to sweep with")
    ap.add_argument("--skip-llm", action="store_true", help="no LLM pings (zero cost)")
    ap.add_argument("--skip-session", action="store_true", help="do not open a browser session")
    return asyncio.run(main_async(ap.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
