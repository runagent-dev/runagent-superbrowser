"""00 — Smoke test: verify your SuperBrowser SDK setup end to end.

Runs a battery of checks and prints a PASS / WARN / FAIL summary, so you can
confirm a fresh setup in one command. It works against either target:

  (default)   in-process SDK   — needs an LLM (`nanobot onboard`, or LLM_MODEL +
                                  a provider key in .env)
  --docker    the all-in-one container on :8450 (`docker compose up -d`) — no
                                  RunAgent key needed; needs the [remote] extra
  --browser   ALSO exercise real browser mode (needs the TS engine / Chromium)

It tells apart three outcomes per check:
  PASS  the call worked
  WARN  the pipeline worked but the LLM/provider rejected the call (quota, bad
        key, unknown model) — a config fix, not a code bug
  FAIL  an exception or an empty/unsuccessful result

Run:
  python examples/00_smoke_test.py
  python examples/00_smoke_test.py --docker
  python examples/00_smoke_test.py --browser
  SUPERBROWSER_LOCAL_AGENT_URL=http://host:8450 python examples/00_smoke_test.py --docker

Tip: the agent logs to stderr and this script's summary to stdout, so append
`2>/dev/null` to see just the PASS/WARN/FAIL summary.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

# --- run from a source checkout without installing -------------------------
try:
    import runagent_superbrowser  # noqa: F401
except ModuleNotFoundError:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "nanobot"))
# ---------------------------------------------------------------------------

from runagent_superbrowser import SuperBrowser

# Provider errors bubble up as the agent's answer text; spot them so a billing /
# key / model problem reads as WARN (fix your .env) instead of a green PASS.
# Covers both the raw provider JSON (Docker path) and nanobot's normalized
# wrapper ("The AI provider rejected the request because …").
_LLM_ERR_HINTS = (
    "the ai provider rejected", "out of quota", "in arrears", "check the billing",
    "top up", "insufficient_quota", "exceeded your current quota", "invalid_api_key",
    "incorrect api key", "api key is invalid", "model_not_found", "does not exist",
    "authentication", "permission denied", "rate limit", "no llm credentials",
)
_CALL_TIMEOUT = 120.0  # seconds — keep each check bounded


def _quiet_logs() -> None:
    """Best-effort: silence nanobot's DEBUG/INFO loguru noise so the summary is
    readable. Override the level with SUPERBROWSER_SMOKE_LOG, or just redirect
    stderr (`2>/dev/null`) — the summary is printed to stdout."""
    try:
        from loguru import logger

        logger.remove()
        logger.add(sys.stderr, level=os.environ.get("SUPERBROWSER_SMOKE_LOG", "WARNING").upper())
    except Exception:  # noqa: BLE001
        pass


def _looks_like_llm_error(text: str, error: str | None) -> str | None:
    blob = f"{text or ''}\n{error or ''}".lower()
    for hint in _LLM_ERR_HINTS:
        if hint in blob:
            return hint
    if (text or "").strip().lower().startswith("error:"):
        return "agent returned an error string"
    return None


def _trunc(s: str, n: int = 96) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


class Report:
    _ICON = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗", "SKIP": "·"}

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def record(self, name: str, status: str, detail: str = "") -> None:
        self.rows.append((name, status, detail))
        print(f"  {self._ICON[status]} {status:<4}  {name}" + (f"  — {detail}" if detail else ""))

    def summary(self) -> dict[str, int]:
        n = {s: sum(1 for _, st, _ in self.rows if st == s) for s in self._ICON}
        print("\n" + "-" * 64)
        print(f"  {n['PASS']} passed · {n['WARN']} warn · {n['FAIL']} failed · {n['SKIP']} skipped")
        if n["WARN"]:
            print("  note: WARN = the SDK pipeline works, but the LLM/provider rejected the")
            print("        call (quota / bad key / unknown model). Fix LLM_MODEL + key in .env.")
        return n


def _classify_result(rep: Report, name: str, text: str, error: str | None, success: bool,
                     ok_detail: str) -> None:
    """Record a normal text result as PASS / WARN / FAIL."""
    hint = _looks_like_llm_error(text, error)
    if hint:
        rep.record(name, "WARN", f"LLM/provider issue: {hint}")
    elif success and (text or "").strip():
        rep.record(name, "PASS", ok_detail)
    else:
        rep.record(name, "FAIL", error or "no answer")


def run_checks(sb: SuperBrowser, rep: Report, *, do_browser: bool) -> None:
    # 1) fetch — the simplest round-trip (no browser engine)
    try:
        r = sb.run("What is 2+2? Reply with just the number.", mode="fetch", timeout=_CALL_TIMEOUT)
        _classify_result(rep, "fetch mode", r.text, r.error, r.success, _trunc(r.text))
    except Exception as e:  # noqa: BLE001
        rep.record("fetch mode", "FAIL", f"{type(e).__name__}: {e}")

    # 2) auto — routing + the classification verdict
    try:
        r = sb.run("what is the top story on Hacker News right now?", mode="auto", timeout=_CALL_TIMEOUT)
        approach = (r.classification or {}).get("approach", "?") if r.classification else "?"
        _classify_result(rep, "auto mode", r.text, r.error, r.success,
                         f"approach={approach} · {_trunc(r.text, 60)}")
    except Exception as e:  # noqa: BLE001
        rep.record("auto mode", "FAIL", f"{type(e).__name__}: {e}")

    # 3) structured output — typed data back via a pydantic schema
    try:
        from pydantic import BaseModel

        class Fact(BaseModel):
            answer: int

        r = sb.run('Return JSON {"answer": N} where N is 7 times 6.',
                   mode="fetch", output_schema=Fact, timeout=_CALL_TIMEOUT)
        if _looks_like_llm_error(r.text, r.error):
            rep.record("structured output", "WARN", "LLM/provider issue (see above)")
        elif r.data is not None:
            rep.record("structured output", "PASS", f"parsed -> {r.data!r}")
        elif r.success:
            rep.record("structured output", "WARN", f"no clean JSON parsed (got: {_trunc(r.text, 50)})")
        else:
            rep.record("structured output", "FAIL", r.error or "no answer")
    except Exception as e:  # noqa: BLE001
        rep.record("structured output", "FAIL", f"{type(e).__name__}: {e}")

    # 4) streaming — step events that end with a single result event
    try:
        events, final = 0, None
        for ev in sb.stream("What is the capital of France? One word.", mode="fetch", timeout=_CALL_TIMEOUT):
            events += 1
            if ev.get("type") == "result":
                final = ev
        if final is None:
            rep.record("streaming", "FAIL", f"{events} events, no result event")
        else:
            _classify_result(rep, "streaming", final.get("text", ""), final.get("error"),
                             bool(final.get("success")), f"{events} events · {_trunc(final.get('text', ''), 44)}")
    except Exception as e:  # noqa: BLE001
        rep.record("streaming", "FAIL", f"{type(e).__name__}: {e}")

    # 5) browser (opt-in) — a real engine round-trip
    if do_browser:
        try:
            r = sb.run("Go to https://example.com and report the page's H1 heading text.",
                       url="https://example.com", mode="browser", timeout=_CALL_TIMEOUT)
            _classify_result(rep, "browser mode", r.text, r.error, r.success, _trunc(r.text))
        except Exception as e:  # noqa: BLE001
            rep.record("browser mode", "FAIL", f"{type(e).__name__}: {e} (TS engine / Chromium up?)")
    else:
        rep.record("browser mode", "SKIP", "pass --browser to run (needs the TS engine on :3100)")


def main() -> int:
    ap = argparse.ArgumentParser(description="SuperBrowser SDK smoke test")
    ap.add_argument("--docker", action="store_true",
                    help="target the all-in-one container instead of running in-process")
    ap.add_argument("--browser", action="store_true",
                    help="also run real browser mode (needs the TS engine / Chromium)")
    ap.add_argument("--url", default=os.environ.get("SUPERBROWSER_LOCAL_AGENT_URL", "http://localhost:8450"),
                    help="container URL for --docker (default http://localhost:8450)")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero on WARN too (e.g. an out-of-quota key), for CI")
    args = ap.parse_args()

    target = f"docker all-in-one ({args.url})" if args.docker else "in-process"
    print(f"SuperBrowser SDK smoke test — target: {target}")
    print("-" * 64)

    rep = Report()

    # Construct the facade for the chosen target.
    try:
        if args.docker:
            # remote=False + a local agent URL -> talk to the container (no API key).
            sb = SuperBrowser(local_agent_url=args.url, persistent=True)
        else:
            # in-process; auto-start the TS engine only if we're testing browser mode.
            sb = SuperBrowser(auto_start_server=args.browser)
        rep.record("construct SuperBrowser", "PASS", target)
    except Exception as e:  # noqa: BLE001
        rep.record("construct SuperBrowser", "FAIL", f"{type(e).__name__}: {e}")
        rep.summary()
        return 1

    # LLM-config visibility (in-process only; the container manages its own).
    if not args.docker:
        cfg = pathlib.Path("~/.nanobot/config.json").expanduser()
        env_llm = any(os.environ.get(k) for k in ("LLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LLM_MODEL"))
        if cfg.exists():
            rep.record("LLM configured", "PASS", "~/.nanobot/config.json")
        elif env_llm:
            rep.record("LLM configured", "PASS", "LLM_* in .env/env (bridged on first run)")
        else:
            rep.record("LLM configured", "WARN", "no config and no LLM_* — run `nanobot onboard` or set .env")

    _quiet_logs()  # nanobot is imported on the first run; hush its DEBUG noise
    try:
        run_checks(sb, rep, do_browser=args.browser)
    finally:
        try:
            sb.close()
        except Exception:  # noqa: BLE001
            pass

    n = rep.summary()
    failed = n["FAIL"] + (n["WARN"] if args.strict else 0)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
