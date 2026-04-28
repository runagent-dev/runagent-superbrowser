"""
Orchestrator tools for the two-agent architecture.

The orchestrator delegates browser work to a fresh browser worker instance,
manages site-specific learnings, and never touches browser tools directly.
"""

from __future__ import annotations

import hashlib
import json as _json
import os
import re as _re
import time as _time
import uuid
from pathlib import Path
from typing import Any

import httpx

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema

# Outer-loop circuit breaker.
#
# Keyed by (domain, sha1(instructions)[:12]) so the same task on the same
# domain is what gets counted — an orchestrator legitimately delegating two
# different browser tasks against the same site is fine.
#
# Each value is (attempt_count, first_seen_ts, last_worker_result). Entries
# older than one hour are treated as stale and reset on next touch; the
# orchestrator may have recovered and the user may be retrying something
# intentional. The result text is kept so a 2nd-delegation gate can inspect
# it for structured-data markers and refuse re-delegation when the previous
# run actually answered the question.
_DELEGATION_ATTEMPTS: dict[str, tuple[int, float, str]] = {}
_DELEGATION_MAX_ATTEMPTS = 2
_DELEGATION_WINDOW_SEC = 60 * 60


# Phase 4: result-quality detector. A worker result is "substantive" when it
# contains markers of verified live data — concrete prices, named addresses,
# specific time stamps, or boolean flags from the page. The orchestrator's
# default reflex on a hedged "Unable to complete this truthfully" phrasing
# is to re-delegate, even when the worker returned 1500+ chars of verified
# findings; that re-delegation throws away progress and starts a fresh
# session that usually fares worse. This signal lets us intercept that
# reflex.
_SUBSTANTIVE_PRICE_RE = _re.compile(r"\$\s?\d+(?:[.,]\d+)?")
_SUBSTANTIVE_KEYWORDS = (
    "in & out", "in&out", "in and out", "in-and-out",
    "garage", "verified", "found", "options",
)


def _result_is_substantive(text: str) -> tuple[bool, list[str]]:
    """Return (is_substantive, reasons) for a worker result.

    Substantive means: ≥ 400 chars AND at least one of:
      - contains a price token like "$15.72" or "$ 6"
      - contains a numbered list of options ("1.", "2.")
      - contains domain-specific keywords ("In & Out", "garage", "verified")
    Used to refuse a 2nd re-delegation that would discard verified work.
    The 400-char floor avoids false positives from short error captions
    that happen to contain a price or keyword (e.g. "[error: $0 returned"
    on a 50-char failure).
    """
    if not text:
        return False, []
    reasons: list[str] = []
    if len(text) < 400:
        return False, []
    if _SUBSTANTIVE_PRICE_RE.search(text):
        reasons.append("price_tokens")
    lower = text.lower()
    kw_hits = [kw for kw in _SUBSTANTIVE_KEYWORDS if kw in lower]
    if kw_hits:
        reasons.append(f"keywords({','.join(kw_hits[:3])})")
    if "1." in text and "2." in text:
        reasons.append("numbered_list")
    return (bool(reasons), reasons)

from superbrowser_bridge.routing import (
    LEARNINGS_DIR,
    _captcha_learnings_path,
    _classify_task,
    _domain_from_url,
    _learnings_path,
    _looks_blocked,
    _preferred_approach,
    _record_routing_outcome,
    _rewrite_for_search,
    _routing_path,
    learning_reads_enabled,
)

# Where the browser worker workspace lives (relative to this file)
_BASE = Path(__file__).resolve().parent.parent
BROWSER_WORKSPACE = str(_BASE / "workspace_browser")




def _task_fingerprint(instructions: str) -> str:
    """SHA1 fingerprint of a normalized task-instruction string.

    Used to gate the "Resume From Checkpoint" injection so checkpoints
    from a completed task can't leak into the prompt of a SUBSEQUENT,
    different task on the same domain. Normalization is aggressive on
    purpose — whitespace collapsed, lowercased, punctuation stripped —
    so that tiny prompt tweaks (extra space, casing) still match.
    """
    import hashlib as _hashlib
    import re as _re
    if not instructions:
        return ""
    s = (instructions or "").lower()
    # Strip punctuation beyond alnum/space; collapse whitespace.
    s = _re.sub(r"[^a-z0-9 ]+", " ", s)
    s = _re.sub(r"\s+", " ", s).strip()
    return _hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def _update_captcha_learnings(domain: str, steps: list[dict]) -> dict | None:
    """Parse captcha solve results from step_history and update per-domain JSON.

    Looks for browser_solve_captcha steps whose result payload contains a
    structured JSON block (we emit one from BrowserSolveCaptchaTool). Each
    solve contributes:
      - method/subMethod that succeeded, plus vendor + duration
      - success_rate over the last 10 attempts
      - median_solve_ms of successful attempts

    Stale entries (>30 days or ≥5 consecutive failures) are pruned when the
    file is rewritten. The schema is intentionally small so future tasks
    can read it quickly.
    """
    from datetime import datetime, timezone
    import statistics

    # Extract solve attempts from steps.
    new_attempts: list[dict] = []
    for step in steps or []:
        if step.get("tool") != "browser_solve_captcha":
            continue
        result = step.get("result") or ""
        # The tool returns "<summary>\n\nResult JSON:\n{...}" — pull the JSON.
        brace = result.find("{")
        if brace < 0:
            continue
        try:
            parsed = _json.loads(result[brace:])
        except (_json.JSONDecodeError, ValueError):
            continue
        if not isinstance(parsed, dict):
            continue
        parsed["observed_at"] = datetime.now(timezone.utc).isoformat()
        parsed["step_url"] = step.get("url") or ""
        new_attempts.append(parsed)

    if not new_attempts:
        return None

    path = _captcha_learnings_path(domain)
    existing: dict = {"attempts": [], "updated_at": None}
    if learning_reads_enabled() and os.path.exists(path):
        try:
            with open(path) as f:
                existing = _json.load(f)
        except (ValueError, OSError):
            existing = {"attempts": [], "updated_at": None}

    # Prune stale attempts (>30 days old).
    cutoff = (datetime.now(timezone.utc).timestamp() - 30 * 86400)
    def _is_fresh(a: dict) -> bool:
        ts = a.get("observed_at")
        if not ts:
            return False
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() >= cutoff
        except ValueError:
            return False
    kept = [a for a in existing.get("attempts", []) if _is_fresh(a)]
    kept.extend(new_attempts)
    # Cap at last 50 attempts to keep the file bounded.
    kept = kept[-50:]

    # Consecutive-failure decay: if last 5 attempts all failed, mark domain
    # as cold so future workers know not to trust the cached "winning method".
    last_five = kept[-5:]
    cold = len(last_five) == 5 and all(not a.get("solved") for a in last_five)

    # Compute per-method stats.
    per_method: dict[str, dict] = {}
    for a in kept:
        method = a.get("method") or "unknown"
        bucket = per_method.setdefault(
            method,
            {"attempts": 0, "solved": 0, "durations": [], "steps": []},
        )
        bucket["attempts"] += 1
        if a.get("solved"):
            bucket["solved"] += 1
            if a.get("durationMs"):
                bucket["durations"].append(int(a["durationMs"]))
            # Iterative loop emits a "steps" field. Track for budget
            # tuning: if p95 steps creeps up on a domain, the site has
            # likely hardened its challenge cadence and we should widen
            # the screenshot budget or shortcut to human handoff earlier.
            if isinstance(a.get("steps"), int) and a["steps"] > 0:
                bucket["steps"].append(int(a["steps"]))

    # Pick the winning method = highest success rate, tiebreak by speed.
    best_method = None
    best_rate = -1.0
    best_duration = float("inf")
    for method, bucket in per_method.items():
        rate = bucket["solved"] / bucket["attempts"] if bucket["attempts"] else 0.0
        median = statistics.median(bucket["durations"]) if bucket["durations"] else float("inf")
        if rate > best_rate or (rate == best_rate and median < best_duration):
            best_method = method
            best_rate = rate
            best_duration = median

    last10 = kept[-10:]
    last10_success = sum(1 for a in last10 if a.get("solved"))
    # Human-handoff flag: if any captcha ever succeeded via the
    # human_handoff strategy on this domain, record it so future tasks can
    # auto-enable the handoff path. Existing value is sticky once true
    # (cheap, and a false negative would silently re-break the flow).
    any_human_success = any(
        a.get("solved") and (a.get("method") or "").startswith("human_handoff")
        for a in kept
    )
    needs_human = bool(existing.get("needs_human_handoff")) or any_human_success

    summary = {
        "domain": domain,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "winning_method": best_method,
        "winning_success_rate": round(best_rate, 3) if best_rate >= 0 else None,
        "winning_median_ms": None if best_duration == float("inf") else int(best_duration),
        "success_rate_last_10": round(last10_success / len(last10), 3) if last10 else None,
        "cold": cold,
        "needs_human_handoff": needs_human,
        "per_method": {
            m: {
                "attempts": b["attempts"],
                "solved": b["solved"],
                "success_rate": round(b["solved"] / b["attempts"], 3) if b["attempts"] else 0.0,
                "median_ms": int(statistics.median(b["durations"])) if b["durations"] else None,
                "steps_p50": int(statistics.median(b["steps"])) if b["steps"] else None,
                "steps_p95": (
                    int(statistics.quantiles(b["steps"], n=20)[18])
                    if len(b["steps"]) >= 2 else (
                        b["steps"][0] if b["steps"] else None
                    )
                ),
            }
            for m, b in per_method.items()
        },
        "attempts": kept,
    }

    try:
        with open(path, "w") as f:
            _json.dump(summary, f, indent=2, default=str)
    except OSError:
        return None
    return summary


def _domain_needs_human_handoff(domain: str) -> bool:
    """Return True if a prior task on this domain succeeded via human handoff.

    Cheap read on the captcha-learnings JSON. Missing file / malformed
    JSON / missing field all return False so this is safe to call on
    first-touch domains.
    """
    if not domain:
        return False
    if not learning_reads_enabled():
        return False
    try:
        path = _captcha_learnings_path(domain)
    except Exception:
        return False
    if not os.path.exists(path):
        return False
    try:
        with open(path) as f:
            data = _json.load(f)
    except (ValueError, OSError):
        return False
    return bool(data.get("needs_human_handoff"))


async def _probe_url(url: str) -> dict:
    """Lightweight HTTP probe — no browser, no JS rendering.

    Returns a dict with keys: unreachable, blocked, status, error, reason, title.
    Used by DelegateBrowserTaskTool before spawning an expensive browser worker.
    """
    import re as _re_probe
    result: dict = {
        "unreachable": False,
        "blocked": False,
        "status": 0,
        "error": "",
        "reason": "",
        "title": "",
    }
    try:
        async with httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=True,
            verify=False,  # some sites have self-signed certs
        ) as client:
            r = await client.get(url, headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml",
            })
            result["status"] = r.status_code
            if r.status_code >= 500:
                result["unreachable"] = True
                result["error"] = f"HTTP {r.status_code}"
                return result
            body = r.text[:5000]
            blocked, reason = _looks_blocked(body)
            result["blocked"] = blocked
            result["reason"] = reason
            # Extract <title> for sanity check
            title_match = _re_probe.search(
                r"<title[^>]*>(.*?)</title>", body, _re_probe.IGNORECASE | _re_probe.DOTALL,
            )
            if title_match:
                result["title"] = title_match.group(1).strip()[:200]
    except httpx.ConnectError as exc:
        result["unreachable"] = True
        result["error"] = f"Connection failed: {exc}"
    except httpx.TimeoutException:
        result["unreachable"] = True
        result["error"] = "Connection timed out (10s)"
    except Exception as exc:
        result["unreachable"] = True
        result["error"] = str(exc)[:200]
    return result


from nanobot.agent.tools.schema import BooleanSchema


@tool_parameters(
    tool_parameters_schema(
        instructions=StringSchema(
            "Specific browser task instructions for the worker. "
            "Be detailed: include URL, what to click, what to fill, what to extract."
        ),
        url=StringSchema("Starting URL for the task", nullable=True),
        force=BooleanSchema(
            description=(
                "Override the routing classifier. Use only if the classifier "
                "suggests 'search' but you KNOW the task requires real browser interaction."
            ),
            default=False,
        ),
        enable_human_handoff=BooleanSchema(
            description=(
                "When true (default), if every captcha auto-solve strategy fails, "
                "pause the agent and surface a view URL so the user can solve the "
                "captcha themselves. Set to false only for fully-unattended runs "
                "where a human will not be available to respond."
            ),
            default=True,
        ),
        required=["instructions"],
    )
)
class DelegateBrowserTaskTool(Tool):
    """Delegate a browser task to the worker agent.

    Creates a fresh browser worker Nanobot with isolated state,
    runs the task, and returns the result. Each call gets a clean
    session with no history pollution.
    """

    name = "delegate_browser_task"
    description = (
        "Send a browser task to the worker agent. The worker opens a fresh browser, "
        "executes scripts, and returns the result. Write SPECIFIC instructions. "
        "Prefer delegate_search_task for data-retrieval/aggregation tasks; the "
        "classifier will warn you if this call looks like it should be search."
    )

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self,
        instructions: str,
        url: str | None = None,
        force: bool = False,
        # Default True so a human can solve captchas the auto-solver fails on.
        # Pass False only for fully unattended runs.
        enable_human_handoff: bool = True,
        **kw: Any,
    ) -> str:
        # --- Outer-loop circuit breaker --------------------------------
        # Before doing anything expensive, check whether this same task
        # has already been delegated twice and failed both times. A third
        # attempt would replay the resumption-artifact → inner-loop →
        # failure cascade that produced the regression. Key by (domain,
        # task-hash) so unrelated tasks aren't collateral-damaged.
        _attempt_domain = _domain_from_url(url) if url else ""
        _dedup_key = (
            f"{_attempt_domain or 'no-domain'}::"
            f"{hashlib.sha1(instructions.encode('utf-8')).hexdigest()[:12]}"
        )
        _prev = _DELEGATION_ATTEMPTS.get(_dedup_key)
        _now = _time.time()
        if _prev is not None and (_now - _prev[1]) > _DELEGATION_WINDOW_SEC:
            _prev = None  # window expired, treat as fresh
        _attempts_so_far = _prev[0] if _prev else 0
        # Phase 4: refuse a 2nd-or-later delegation when the prior worker
        # returned a substantive result (verified prices/addresses/options).
        # Reflexive re-delegation in that case discards verified progress.
        # The orchestrator must instead present the prior findings to the
        # user — possibly with caveats — rather than spawn a fresh worker
        # that loses all context.
        if (
            _prev is not None
            and _attempts_so_far >= 1
            and len(_prev) >= 3
            and os.environ.get("REDELEGATION_SUBSTANCE_GUARD", "1") not in ("0", "false", "no")
        ):
            _prev_result = _prev[2] or ""
            _is_sub, _reasons = _result_is_substantive(_prev_result)
            if _is_sub:
                # Don't pop the entry — keep it so a 3rd reflex retry also
                # gets blocked. The orchestrator may still proceed if it
                # explicitly passes force=True with an explanation of WHY
                # the previous result was insufficient.
                if not bool(force):
                    print(
                        f"\n>> [REDELEGATION_BLOCKED domain={_attempt_domain}] "
                        f"prev result substantive (reasons={_reasons}); "
                        f"refusing re-delegation"
                    )
                    return (
                        f"[REDELEGATION_BLOCKED domain={_attempt_domain} "
                        f"prev_chars={len(_prev_result)} signals={','.join(_reasons)}]\n"
                        f"The previous worker returned {len(_prev_result)} chars "
                        f"of structured findings — re-delegating typically "
                        f"discards that progress and starts a fresh session "
                        f"that fares worse.\n\n"
                        f"Before calling delegate_browser_task again:\n"
                        f"  1. **Read the previous worker result above this "
                        f"call** — it likely already answers the user's "
                        f"question (possibly with caveats).\n"
                        f"  2. Present that result to the user with any "
                        f"caveats the worker raised. Honest hedged answers "
                        f"with verified data are SUCCESS, not failure.\n"
                        f"  3. Only re-delegate if the previous result was "
                        f"genuinely empty / network-blocked / captcha-blocked "
                        f"with NO data extracted — and in that case, pass "
                        f"force=true and explain in your reasoning what "
                        f"specifically the prior run missed."
                    )
        if _attempts_so_far >= _DELEGATION_MAX_ATTEMPTS:
            # Clear the resumption artifact so whatever task follows this
            # one starts from a clean slate — the artifact may well be
            # what's poisoning the loop.
            try:
                from superbrowser_bridge.session_tools import clear_resumption_artifact
                clear_resumption_artifact()
            except Exception:
                pass
            _DELEGATION_ATTEMPTS.pop(_dedup_key, None)
            print(
                f"\n>> [DELEGATION_BUDGET_EXHAUSTED domain={_attempt_domain}] "
                f"refusing third delegation of the same task"
            )
            return (
                f"[DELEGATION_BUDGET_EXHAUSTED domain={_attempt_domain}]\n"
                f"This task has been delegated to the browser worker "
                f"{_DELEGATION_MAX_ATTEMPTS} times and failed each time. "
                f"Further delegations will replay the same failure cascade.\n\n"
                f"Do NOT call delegate_browser_task again for this task. "
                f"Either:\n"
                f"  1. Report the failure to the user with an honest diagnosis "
                f"(do not fabricate data to fill the gap).\n"
                f"  2. Call delegate_search_task if the task is viable as "
                f"public-data search."
            )
        _DELEGATION_ATTEMPTS[_dedup_key] = (
            _attempts_so_far + 1,
            _prev[1] if _prev else _now,
            (_prev[2] if (_prev is not None and len(_prev) >= 3) else ""),
        )

        # --- Pre-validation probe (Layer 1.5) ----------------------------
        # Before spawning an expensive browser worker, do a lightweight HTTP
        # probe to verify the URL is reachable and not obviously blocked.
        # This saves 25+ iterations on dead/wrong URLs.
        _probe_warning: str | None = None
        if url:
            probe = await _probe_url(url)
            if probe["unreachable"]:
                print(f"\n>> pre-validation: {url} is unreachable: {probe['error']}")
                return (
                    f"[URL_UNREACHABLE] {url} is not reachable: {probe['error']}. "
                    f"Do NOT delegate to browser — the site is down or the URL "
                    f"is wrong. Either fix the URL or use delegate_search_task."
                )
            if probe["blocked"]:
                _probe_warning = (
                    f"\n## Pre-validation Warning\n"
                    f"HTTP probe returned status {probe['status']} with bot-block "
                    f"markers: {probe['reason']}. The site may block automated "
                    f"access. If you hit a captcha, use "
                    f"browser_solve_captcha(method='auto') immediately — if that "
                    f"fails the system will auto-escalate to human handoff."
                )

        # --- Classifier gate (Layer 2) ---------------------------------
        # Run the deterministic classifier BEFORE spawning a worker. If it
        # disagrees with confidence >= 0.7 and the orchestrator hasn't
        # passed force=True, return a warn-back string so the orchestrator
        # can re-route on the next turn without burning an iteration on a
        # wrong-path worker spawn.
        classification = _classify_task(instructions, url)
        if not force and classification["approach"] != "browser" and classification["confidence"] >= 0.7:
            print(f"\n>> delegate_browser_task classifier warn-back: {classification}")
            return (
                "[ROUTING WARNING] This task looks like it wants "
                f"`{classification['approach']}`, not `browser` "
                f"(reason: {classification['reason']}). "
                f"Call `delegate_search_task` instead. "
                f"If you genuinely need browser interaction (JS-rendered content, "
                f"login, form submission), re-call `delegate_browser_task` with "
                f"`force=true` and explain why in the instructions."
            )

        from nanobot import Nanobot
        from superbrowser_bridge.session_tools import (
            BrowserSessionState,
            clear_resumption_artifact,
            load_resumption_artifact,
            register_session_tools,
            save_resumption_artifact,
        )
        from superbrowser_bridge.worker_hook import BrowserWorkerHook

        task_id = uuid.uuid4().hex[:8]
        session_key = f"worker:{task_id}"

        # Research tasks need more iterations: search + visit 3-5 pages + refine
        _research_kw = ("search", "research", "find information", "google", "look up", "investigate")
        is_research = any(k in instructions.lower() for k in _research_kw)
        # Caps are env-overridable so a stuck flow can be widened without
        # a code edit. Defaults sized for the click-accuracy era — tight
        # enough that a genuinely looping worker still bails out, loose
        # enough that a clean multi-step flow (login → form → submit →
        # verify) doesn't time out on its own.
        max_iterations = (
            int(os.environ.get("SUPERBROWSER_WORKER_MAX_ITER_RESEARCH") or "75")
            if is_research
            else int(os.environ.get("SUPERBROWSER_WORKER_MAX_ITER") or "50")
        )

        print(f"\n>> delegate_browser_task(session={session_key})")
        print(f"   Instructions: {instructions[:120]}...")

        # Create a FRESH browser worker — isolated state, clean session
        worker = Nanobot.from_config(workspace=BROWSER_WORKSPACE)

        # Remove default nanobot tools — worker only needs browser tools
        default_tools_to_remove = [
            "web_search", "web_fetch",
            "read_file", "write_file", "edit_file",
            "list_dir", "glob", "grep",
            "exec", "spawn", "cron", "message",
        ]
        for name in default_tools_to_remove:
            worker._loop.tools.unregister(name)

        # Register ALL browser tools with isolated state
        worker_state = BrowserSessionState()
        # Task-complexity-aware screenshot budget (replaces hardcoded MAX_SCREENSHOTS=2).
        # Research tasks, captcha-keywords, and known-hard domains bump the cap.
        worker_state.configure_budget(
            task_instruction=instructions,
            target_url=url or "",
            is_research=is_research,
        )
        worker_state.task_id = task_id

        # Resolve the target domain once, up front — used by human-handoff
        # auto-enable (below), learnings injection (further down), and the
        # success-outcome recorder at the end. Must be defined before the
        # handoff-budget logic references it.
        domain = _domain_from_url(url) if url else None

        # Domain pinning: prevent the worker from navigating to unrelated
        # sites. When the target site blocks the agent, the LLM's
        # helpfulness bias drives it to find the answer elsewhere (e.g.,
        # Zara task → Amazon). Pinning stops that at the tool level.
        if domain and domain != "unknown":
            worker_state.pinned_domain = domain.replace("www.", "")

        # Human handoff: opt in via tool arg OR auto-enable if per-domain
        # learnings mark this site as "needs_human_handoff=true" (flipped
        # after any prior task succeeded via the human_handoff captcha
        # strategy). Per-session budget from SUPERBROWSER_MAX_HUMAN_HANDOFFS.
        auto_handoff = _domain_needs_human_handoff(domain) if domain else False
        if enable_human_handoff or auto_handoff:
            worker_state.human_handoff_enabled = True
            try:
                worker_state.human_handoff_budget = max(
                    0,
                    int(os.environ.get("SUPERBROWSER_MAX_HUMAN_HANDOFFS", "1")),
                )
            except ValueError:
                worker_state.human_handoff_budget = 1
            print(
                f"   [human-handoff enabled: explicit={enable_human_handoff}, "
                f"auto={auto_handoff}, budget={worker_state.human_handoff_budget}]"
            )

        register_session_tools(worker, worker_state)

        # Create mid-session guardrail hook
        worker_hook = BrowserWorkerHook(worker_state, max_iterations=max_iterations)

        # 25 iterations — enough for: open + inspect + script + fail + retry + verify + close
        worker._loop.max_iterations = max_iterations

        # Build the worker prompt with enforced workflow structure
        parts = []

        # Hard rule sits at position 0 of the prompt so it's the first thing
        # the worker's LLM reads. Previous iteration of the prompt put the
        # "browser_open(url)" tool listing at the bottom of the Execution
        # Plan; some models (observed with gemini-3-flash-preview) default
        # to firing that tool on every turn, spawning a new session each
        # call and cascading into the outer delegation loop. The text below
        # is deliberately hedge-free and placed early so models can't miss
        # it. Reinforced by the hard idempotency guard in BrowserOpenTool.
        # The `Target URL:` line below (appended only when `url` was
        # provided to delegate_browser_task) turns the rule into an
        # absolute command. Without that line the "first call must be
        # Target URL" text would be nonsense, so the wording below says
        # "if a Target URL is given" and the domain-pin block further
        # down enforces the rest.
        parts.append(
            "## HARD RULE — READ FIRST\n"
            "You may call `browser_open` AT MOST ONCE per task. If a "
            "`Target URL:` line appears below, the FIRST and ONLY "
            "`browser_open` call MUST use exactly that URL — do NOT "
            "detour to Google search, do NOT invent an article URL, do "
            "NOT search 'site:foo.com X'. The Target URL is the "
            "authoritative starting point; the orchestrator already "
            "chose it.\n"
            "The first call returns a session_id. Every later browser tool "
            "(browser_screenshot, browser_click, browser_type, "
            "browser_navigate, browser_get_markdown, browser_run_script, …) "
            "MUST take that session_id and operate on the SAME session. "
            "A second `browser_open` call is almost always a bug — it "
            "throws away your progress and spawns a fresh throwaway "
            "browser.\n"
            "If a tool result looks empty, a screenshot seems missing, or "
            "you're unsure what the page looks like, call "
            "`browser_screenshot(session_id=<id>)` — NEVER `browser_open` — "
            "to re-ground yourself. The tool will refuse a redundant "
            "`browser_open` with a [SESSION_ALREADY_OPEN …] message; if "
            "you see that tag, stop calling `browser_open` immediately and "
            "switch to the right tool named in the refusal message."
        )

        parts.append(
            "## PICK THE RIGHT CLICK TOOL\n"
            "- **Puzzle / game / captcha?** Call `browser_solve_puzzle(session_id)` "
            "first — it auto-detects chess, slider, jigsaw, rotation, and grid-drag "
            "puzzles and runs a dedicated solver. No vision round-trip per move.\n"
            "- **Slider / range control** (retirement calculators, filter ranges, "
            "volume, any slider widget)? NEVER `browser_click` / "
            "`browser_click_at` — clicks on slider thumbs do NOTHING. "
            "NEVER `browser_drag(x1,y1,x2,y2)` with guessed coords — "
            "it's open-loop and overshoots. Three tools, in order of "
            "preference:\n"
            "    • `browser_list_slider_handles(session_id)` — "
            "FIRST step for any custom slider page. DOM-only "
            "enumeration: returns every slider handle with its bbox + "
            "nearest label text. No vision needed. Use this when vision "
            "times out or returns empty bboxes.\n"
            "    • `browser_drag_slider_until(label_hint=..., "
            "target_value, label_pattern)` — CLOSED-LOOP drag. Holds "
            "mouse down, steps while reading the rendered value label, "
            "stops at target. Three ways to pick the handle, use the "
            "first that applies:\n"
            "        (a) `label_hint='Monthly contribution'` — "
            "auto-resolves via browser_list_slider_handles (best choice "
            "for custom widgets).\n"
            "        (b) `handle_bbox_json='{\"x\":..,\"y\":..,\"w\":..,\"h\":..}'` — "
            "pass a bbox from browser_list_slider_handles directly "
            "(dual-thumb case where one label matches multiple "
            "handles).\n"
            "        (c) `vision_index=V_n` — legacy, only when vision "
            "is healthy.\n"
            "      *** CALL THESE ONE AT A TIME ***. Never batch parallel "
            "`browser_drag_slider_until` calls — the CDP mouse is "
            "session-scoped; concurrent drags fight over the cursor. "
            "Wait for each to return (check for [drag_slider_until_failed:...]) "
            "before firing the next. If 'initial_readback_failed', the "
            "regex didn't match any text near the handle — the returned "
            "`label_text` lists the real nearby labels so you can fix "
            "the pattern.\n"
            "    • `browser_set_slider_at(vision_index=V_n, value, "
            "value_mode)` — open-loop variant. Faster but requires vision "
            "to have emitted BOTH slider_handle AND slider_widget bboxes. "
            "Fall back to drag_slider_until if it fails with no_track.\n"
            "    • `browser_set_slider(selector, value_json)` — only if "
            "the page has a stable CSS selector and uses "
            "`<input type=range>` / `role=slider`. Rare on financial "
            "sites.\n"
            "    Do NOT navigate to the iframe URL directly "
            "(domain-pinned). Do NOT guess selectors like "
            "`[class*=slider i]`.\n"
            "- **Target has a stable CSS hook** (chess squares like `.square-54`, "
            "form fields, buttons with `data-test-id`, captcha handles)? Prefer "
            "`browser_click_selector` / `browser_drag_selectors` / `browser_drag_path`. "
            "Zero Gemini latency; pixel-exact centre.\n"
            "- **No obvious selector?** Use `browser_click_at(vision_index=V_n)` — "
            "fine for cookie banners, arbitrary buttons, and elements the vision "
            "agent has labelled."
        )

        if url:
            parts.append(f"Target URL: {url}")

        # Domain constraint: tell the worker not to leave the target site.
        # When a Target URL was explicitly given, the Google search
        # exception is REMOVED — the orchestrator has already decided
        # where to start. Models previously hopped to Google under the
        # "search-based research" loophole and got caught in captcha
        # or opened the wrong site entirely.
        if domain and domain != "unknown":
            _clean_domain = domain.replace("www.", "")
            if url:
                parts.append(
                    f"\n## DOMAIN CONSTRAINT (non-negotiable)\n"
                    f"You MUST only navigate to {_clean_domain} and its "
                    f"subdomains. The Target URL above is the ONLY "
                    f"entry point — do NOT open Google, Bing, or any "
                    f"search engine. Do NOT visit other websites to "
                    f"find the answer (e.g., if asked for Zara dress "
                    f"prices, NEVER go to Amazon or other retailers). "
                    f"If {_clean_domain} blocks you with a captcha or "
                    f"security page, solve the captcha or report "
                    f"failure honestly — do NOT try alternative sites."
                )
            else:
                # No explicit Target URL → Google search IS useful for
                # finding the right page on the target domain.
                parts.append(
                    f"\n## DOMAIN CONSTRAINT (non-negotiable)\n"
                    f"You MUST only navigate to {_clean_domain} and its "
                    f"subdomains. Google.com is allowed ONLY when you "
                    f"need to find the right page on {_clean_domain} "
                    f"(use `site:{_clean_domain}` in the query). Once "
                    f"you find the target page, navigate directly to it. "
                    f"Do NOT visit other websites to find the answer. "
                    f"If {_clean_domain} blocks you with a captcha, "
                    f"solve it or report failure honestly — do NOT try "
                    f"alternative sites."
                )

        # Inject pre-validation warning if the URL probe detected bot-block markers.
        if _probe_warning:
            parts.append(_probe_warning)

        parts.append(f"\n## Task\n{instructions}")

        # Strategy block — bias the brain toward cursor-based actions
        # over scripts. Empirically the LLM reaches for
        # `browser_run_script` as a Swiss Army knife whenever a click
        # feels complicated, but JS clicks are isTrusted=false and
        # bot-detected by Cloudflare / hCaptcha / Akamai-class WAFs.
        # Keep this block EARLY in the prompt so it frames every tool
        # choice that follows.
        parts.append(
            "\n## Strategy — Prefer cursor over scripts\n"
            "The vision agent gives you `[V_n]` bbox indices in every "
            "screenshot reply. Pick targets from that list — the cursor "
            "tools below all dispatch via humanized CDP mouse events "
            "(isTrusted=true), which WAF-protected sites don't flag.\n"
            "\n"
            "Tool preference for element interaction:\n"
            "  1. **FIRST** `browser_click_at(vision_index=V_n)` — "
            "humanized cursor click on the bbox vision just pointed at. "
            "`V_n` MUST come from the most recent screenshot's vision "
            "output; do NOT invent indices or coords.\n"
            "  2. `browser_click_selector(<css>)` — zero-Gemini click "
            "when the target has a stable hook (id, data-test-id, "
            "unique class). Chess squares, form fields, captcha "
            "handles.\n"
            "  3. `browser_click(index=N)` — DOM-index click when "
            "the brain sees the element in the interactive elements "
            "listing but vision didn't surface it.\n"
            "  4. **LAST RESORT** `browser_run_script(mutates=true)` — "
            "only when no bbox/selector works or the action requires "
            "multi-step orchestration. JS clicks are isTrusted=false; "
            "many sites 403 this; don't reach for it reflexively.\n"
            "\n"
            "For text input: `browser_type_at(vision_index=V_n, "
            "text=...)` is the React-safe cursor path.\n"
            "\n"
            "**Popups / modals / country gates:** when vision reports a "
            "blocker layer (cookie banner, region modal, consent), "
            "dismiss it FIRST with `browser_click_at(V_n)` on the "
            "visible dismiss label — e.g. 'Accept', 'Close', 'Continue "
            "Anyway', 'Reject all'. The label IS in the bbox list.\n"
            "\n"
            "**Missing control? Scroll and re-screenshot.** Vision "
            "only sees the current viewport. On dense pages — search "
            "results with filter sidebars, long forms, booking UIs "
            "with vehicle/amenity selectors — the control you need "
            "may be below or beside the fold. If a control mentioned "
            "in your task (vehicle type, in-and-out toggle, filter "
            "chip, amenity checkbox, sort dropdown) is NOT in the "
            "current `[V_n]` list, do NOT invent a selector or click "
            "at guessed coords. Instead: `browser_scroll(percent=...)` "
            "or `browser_scroll_until(target_text='<label>')` to "
            "bring it into view, then `browser_screenshot` for a "
            "fresh bbox list. Repeat until the control appears or "
            "the page can't scroll further.\n"
            "\n"
            "**Still missing after scrolling?** Use "
            "`browser_get_markdown` (free) to inspect the page text "
            "— the interactive elements listing at the bottom of "
            "every tool reply also exposes controls vision culled. "
            "If you see the target in the elements listing as "
            "`[N] tag text=…`, click it via `browser_click(index=N)` "
            "— bypasses vision entirely for stable DOM-indexed "
            "targets. `browser_image_region(bbox_json=...)` can grab "
            "a tight JPEG of a specific viewport region if you need "
            "to OCR/inspect something closely (captcha tiles, price "
            "text near a specific card).\n"
            "\n"
            "**Multi-field forms (filters, booking, signup):** call "
            "`browser_form_begin(intent=..., fields=[...])` to open a "
            "tracked session. The session reminds you of remaining "
            "fields after every tool result, forces a re-screenshot "
            "after each autocomplete pick, and refuses to commit "
            "until every field's typed value is visible on screen. "
            "Mark `autocomplete=true` on fields whose inputs open a "
            "suggestions overlay (city pickers, address autofill) — "
            "the session will require you to click a suggestion "
            "before progressing to the next field. Conclude with "
            "`browser_form_commit` to verify before submit.\n"
            "\n"
            "**Wait for vision before clicking.** The brain receives a "
            "synchronous vision pass after every mutating tool. Do "
            "NOT predict the next click from cached DOM; the "
            "`[vision] …` line attached to every result is your "
            "ground truth. If the vision summary doesn't include the "
            "target you intended to click, RE-SCREENSHOT before "
            "calling `browser_click_at` — the V_n indices you saw "
            "from the previous screenshot may already be stale.\n"
            "\n"
            "**Before `browser_run_script(mutates=true)`:** the tool "
            "is locked until at least 2 distinct cursor strategies "
            "have failed in this session. List in your reasoning the "
            "exact failure captions (e.g. `[click_at_failed:...]`, "
            "`[click_selector_failed]`) you got — if those captions "
            "haven't appeared, the lockout will refuse the script. "
            "JS clicks are isTrusted=false; Cloudflare/Akamai reject "
            "them and the page navigates to a challenge URL, "
            "poisoning the run.\n"
            "\n"
            "**How to phrase your final `done()` call:** if you "
            "extracted ANY verified live data from the page (prices, "
            "named addresses, options, booleans like 'In & Out "
            "Allowed'), START your final_answer with the verified "
            "findings — not with 'Unable to complete this truthfully'. "
            "Hedged-with-data is a SUCCESS, not a failure. Example "
            "good phrasing: 'Found 3 SpotHero options for SFMOMA on "
            "May 3 1pm-5pm: [list with prices]. Caveat: could not "
            "verify Ford F-150 fit at option B; the others either "
            "block trucks or have no In & Out.' Example BAD phrasing "
            "(triggers reflex re-delegation): 'Unable to complete "
            "this truthfully. I reached results but couldn't verify "
            "everything…'. The orchestrator reads the first sentence "
            "as your verdict — open with what you DID confirm, then "
            "list caveats."
        )

        # Auto-inject learnings so the worker follows known patterns
        # (`domain` was resolved earlier, before the handoff-budget logic).
        if domain:
            lpath = _learnings_path(domain)
            if os.path.exists(lpath):
                with open(lpath) as f:
                    learnings = f.read().strip()
                if learnings:
                    parts.append(f"\n## Site Learnings (from past tasks — FOLLOW THESE)\n{learnings}")

            # Tactic penalties — tools that have produced no_effect >=2
            # times on this domain. Surfaces upfront so the brain picks
            # a better-working tactic on the first attempt rather than
            # re-discovering the same wall turn-by-turn.
            try:
                from superbrowser_bridge.routing import (
                    tactic_penalty_summary, TACTIC_ALTERNATIVES,
                )
                penalties = tactic_penalty_summary(domain, min_count=2)
                if penalties:
                    lines = ["\n## Tactic Penalties (prefer alternatives)"]
                    for tool, count in penalties[:5]:
                        alt = TACTIC_ALTERNATIVES.get(
                            tool,
                            "a selector-based or scripted variant",
                        )
                        lines.append(
                            f"- `{tool}`: {count} recent no_effect(s) on "
                            f"{domain} → prefer **{alt}**"
                        )
                    parts.append("\n".join(lines))
            except Exception:
                pass

            # Captcha-specific learnings (Phase 5.3): tell the worker which
            # solve method has worked on this domain so it doesn't blindly
            # retry every strategy.
            cpath = _captcha_learnings_path(domain)
            if os.path.exists(cpath):
                try:
                    with open(cpath) as f:
                        cstats = _json.load(f)
                    winning = cstats.get("winning_method")
                    rate = cstats.get("winning_success_rate")
                    median = cstats.get("winning_median_ms")
                    if winning and rate and rate >= 0.5 and not cstats.get("cold"):
                        parts.append(
                            f"\n## Captcha History for {domain}\n"
                            f"On past tasks, browser_solve_captcha(method='auto') "
                            f"succeeded via **{winning}** with "
                            f"{int(rate * 100)}% success rate (median {median}ms). "
                            f"Trust the auto dispatcher — do not try to solve "
                            f"the captcha manually with clicks."
                        )
                    elif cstats.get("cold"):
                        parts.append(
                            f"\n## Captcha History for {domain}\n"
                            f"WARNING: captcha solves have failed 5+ times in a row. "
                            f"If a captcha appears, call browser_ask_user to get "
                            f"human help rather than looping on browser_solve_captcha."
                        )
                except (ValueError, OSError):
                    pass

        # Check for checkpoint from a previous failed worker attempt.
        # Guard with BOTH domain match AND task-fingerprint match — a
        # domain-only check lets a BMW-search checkpoint leak into a
        # Mercedes-search task on the same site. The fingerprint is a
        # SHA1 of the normalized task text; we only inject a "Resume
        # From Checkpoint" hint when the SAME task re-runs.
        last_checkpoint_path = "/tmp/superbrowser/last_checkpoint.json"
        if os.path.exists(last_checkpoint_path):
            try:
                with open(last_checkpoint_path) as f:
                    checkpoint = _json.load(f)
                cp_url = checkpoint.get("url", "")
                cp_domain = _domain_from_url(cp_url) if cp_url else ""
                cp_fp = str(checkpoint.get("task_fingerprint") or "")
                current_fp = _task_fingerprint(instructions)
                same_domain = bool(cp_url and domain and cp_domain == domain)
                same_task = bool(cp_fp and current_fp and cp_fp == current_fp)
                if same_domain and same_task:
                    parts.append(
                        f"\n## Resume From Checkpoint\n"
                        f"A previous attempt at THIS SAME TASK reached: {cp_url}\n"
                        f"Start from this URL instead of the beginning. "
                        f"Call browser_open with this URL directly.\n"
                        f"Do NOT repeat the steps that led to this point."
                    )
                else:
                    # Different task (or different domain) → remove so
                    # it can't leak into the current worker's prompt.
                    try:
                        os.remove(last_checkpoint_path)
                    except OSError:
                        pass
            except (ValueError, KeyError):
                pass

        # --- Sticky-session resumption (Priority 2) ---------------------
        # If a previous worker on the SAME domain exited stuck within the
        # last RESUMPTION_TTL_SEC and its Puppeteer session is still alive,
        # pre-seed the new worker's session_id so it skips browser_open and
        # resumes on the live page — armed with a list of tactics that
        # already failed, so it won't repeat them.
        resumption: dict | None = None
        if domain:
            resumption = await load_resumption_artifact(domain)
        if resumption:
            worker_state.session_id = resumption["session_id"]
            worker_state.current_url = resumption.get("current_url", "")
            worker_state.best_checkpoint_url = resumption.get("best_checkpoint_url", "") or ""
            failed_lines = [
                f"- {f['tool']}({f['args']}) → {f['result_excerpt']}"
                for f in resumption.get("recent_failures", [])
            ]
            failed_block = "\n".join(failed_lines) if failed_lines else "(none recorded)"
            help_reason = resumption.get("help_reason") or ""
            parts.append(
                f"\n## RESUMPTION — continue on existing browser session\n"
                f"A previous worker got stuck and handed off to you.\n\n"
                f"**Existing session_id**: `{resumption['session_id']}`  "
                f"(Puppeteer page is LIVE at {resumption.get('current_url', '?')})\n\n"
                f"**DO NOT call browser_open.** The session already exists. "
                f"Use the session_id above for every tool call. Start with:\n"
                f"  1. browser_get_markdown(session_id='{resumption['session_id']}') — see current state\n"
                f"  2. Pick a DIFFERENT tactic than what failed below\n\n"
                f"**Tactics that already failed — do NOT repeat them**:\n{failed_block}\n"
                + (f"\n**Previous worker's explanation**: {help_reason}\n" if help_reason else "")
                + "\nIf you also get stuck, call browser_request_help with a concrete "
                "new-tactic suggestion and call done(success=False)."
            )

        # Enforce workflow IN the prompt itself (not just SOUL.md)
        if is_research:
            parts.append("""
## Required Execution Plan — WEB RESEARCH (follow this order)
Step 1: browser_open(url="https://www.google.com/search?q=...") — search Google with a natural, broad query
  Do NOT over-specify with many quoted exact-match phrases. Use natural search terms.
Step 2: browser_get_markdown(session_id) — read the search results, identify promising links
Step 3: For each promising result (visit 3-5 pages):
  - browser_navigate(session_id, url="result-page-url") — ONLY URLs from search results, never fabricated
  - browser_get_markdown(session_id) — extract relevant content (FREE, no budget cost)
  - Note findings and source URL
Step 4: If results are insufficient, do another Google search with refined/alternative terms:
  - browser_navigate(session_id, url="https://www.google.com/search?q=different+query")
  - Repeat Steps 2-3
Step 5: browser_close(session_id) — close when done

KEY FEATURES:
- browser_get_markdown is FREE — use it liberally to read pages
- browser_navigate moves between pages without opening new sessions
- Every action returns updated interactive elements automatically

CRITICAL RULES:
- NEVER fabricate URLs — only visit URLs found in Google search results
- Search snippets are NOT sufficient — you MUST visit actual pages to read full content
- Use BROAD natural queries first, then narrow. Do NOT put all constraints in one query.
- There is no fixed iteration budget. Keep trying concrete tactics until you succeed or genuinely exhaust options — do not pre-announce a bail-out after a few failed tries.
- If you see [GUIDANCE: ...] messages, follow them IMMEDIATELY.
- Return ALL findings with source URLs.
- NEVER invent, estimate, or guess values. If a data point genuinely cannot be retrieved, say so explicitly and return done(success=False) with a brief honest reason. Fabricated numbers with plausible-sounding disclaimers are a FAILURE.""")
        else:
            # When a resumption artifact pre-seeded the worker's session_id,
            # `browser_open` is NOT AVAILABLE — calling it would discard the
            # live page and spawn a new throwaway browser (root cause of the
            # inner-loop regression). Remove it from the tool list entirely
            # so the LLM never sees it as an option.
            if resumption:
                browser_open_line = (
                    "- browser_open — NOT AVAILABLE for this task. A browser session is\n"
                    f"  already active (session_id={resumption['session_id']}). Use that\n"
                    "  session_id on every tool call. If you call browser_open it will\n"
                    "  refuse with [SESSION_ALREADY_OPEN …] — that is the signal to\n"
                    "  switch to browser_screenshot or browser_navigate instead."
                )
            else:
                browser_open_line = (
                    "- browser_open(url) — opens a session, returns screenshot + elements list.\n"
                    "  CALL AT MOST ONCE PER TASK. On any subsequent call the tool will\n"
                    "  refuse with [SESSION_ALREADY_OPEN …] — that is a signal to switch\n"
                    "  to browser_screenshot / browser_navigate / etc., not to retry."
                )

            parts.append("""
## Execution Plan

Typical flow: open → (dismiss popups, fill forms, click actions) → extract → verify → close.
Reading (browser_get_markdown) and interacting can interleave — pick what's
useful next. The rules below are non-negotiable; the order above is a guide.

Available tools:
""" + browser_open_line + """
- browser_click/type/wait_for — single actions, return updated elements.
- browser_run_script — run an in-page script. Use this for multi-step flows
  (fill form, submit, wait, read). Use browser_wait_for(text="...") inside
  or between steps; avoid helpers.sleep() alone.
- browser_get_markdown — FREE text read of the page. Use liberally.
- browser_verify_fact(session_id, claim) — visual sanity check before
  reporting a value. Call this with the EXACT final value.
- browser_request_help — exit with a structured "stuck" signal for the next
  worker to pick up with a different tactic (live session is preserved).
- browser_close — close when done.

EXTRACTION must return a structured object, not a bare value. For a price:
  {{
    "value": <number>, "unit": "per_night|total|per_person|...",
    "currency": "<ISO code>",
    "context_text": "<the label you saw next to the value on the page>",
    "selector_used": "<css or aria-label that matched>",
    "all_candidates": [{{ "value": ..., "label": ... }}, ...]
  }}
`all_candidates` is REQUIRED for prices — it exposes crossed-out / "from" /
with-tax variants so the final answer can disambiguate.

VERIFY before reporting. After extracting, call browser_verify_fact with
your intended final answer. If it returns supported=false, re-extract with
a corrected selector; do NOT report the unverified value.

CRITICAL RULES:
- NEVER invent, estimate, or guess a value. If extraction returns empty,
  call done(success=False) with an honest reason. Fabrication is a FAILURE.
- Final answer must quote the exact `value` + `context_text` from the
  extraction — no paraphrasing, no rounding "$1,234.56" to "around $1,200".
- If `all_candidates` has multiple prices, say so: "Page showed $X (crossed
  out) and $Y (selected); reporting $Y."
- There is no fixed iteration budget. If a script fails, FIX IT and retry on
  the same page — do NOT navigate backward or browser_open again.
- If [GUIDANCE: ...] messages appear, follow them IMMEDIATELY.
- If stuck, try concrete ALTERNATIVE tactics before giving up: different
  selector, keyboard navigation (Tab/ArrowDown/Enter), browser_screenshot
  to re-observe, browser_rewind_to_checkpoint. Call browser_request_help
  only after multiple concrete alternatives failed.""")

        prompt = "\n".join(parts)

        # Pre-announce the view URL if handoff is enabled — a user pre-opening
        # it in their browser avoids racing the 5-min block in the solver.
        if worker_state.human_handoff_enabled:
            public_host = os.environ.get(
                "SUPERBROWSER_PUBLIC_HOST", "http://localhost:3100",
            ).rstrip("/")
            print(
                f"\n>> [HUMAN HANDOFF ARMED for this task] "
                f"If the worker hits a captcha auto-solve can't crack, "
                f"it will pause and ask the user to solve via:\n"
                f">>   {public_host}/session/<session_id>/view\n"
                f">> (session_id is printed when browser_open runs)"
            )

        try:
            result = await worker.run(prompt, session_key=session_key, hooks=[worker_hook])
            content = result.content

            # Diagnostic: how many browser tool calls did the worker actually
            # make? If 0, the model refused to try — classify explicitly so
            # the user-facing reply doesn't blame "technical error" on a bug
            # that's actually "the LLM chose not to act".
            steps_taken = len(worker_state.step_history)
            print(
                f"\n>> Worker result ({len(content)} chars, "
                f"{steps_taken} browser tool calls): {content[:200]}..."
            )

            if steps_taken == 0:
                # Worker never touched the browser. Make this visible to the
                # orchestrator so it can tell the user honestly ("the worker
                # LLM declined to run any tools") instead of generic "technical
                # error" language that implies a system failure.
                content = (
                    f"[WORKER_NO_TOOL_CALLS] The worker model produced "
                    f"{len(content)} chars of text but called zero browser "
                    f"tools (browser_open was never invoked). No captcha "
                    f"could be detected or handed off because no page was "
                    f"ever loaded. Likely cause: prompt complexity or model "
                    f"refusal. Worker's own text:\n\n{content}"
                )

            # Read the activity log the worker saved on close
            task_dir = f"/tmp/superbrowser/{task_id}"
            activity_path = "/tmp/superbrowser/last_activity.md"
            if os.path.exists(activity_path):
                with open(activity_path) as f:
                    activity = f.read().strip()
                if activity:
                    content += f"\n\n[Worker Activity Log]\n{activity}"

            # Read step history for structured analysis
            step_path = os.path.join(task_dir, "step_history.md")
            if os.path.exists(step_path):
                with open(step_path) as f:
                    steps = f.read().strip()
                if steps:
                    content += f"\n\n[Worker Step History]\n{steps}"

            # Mine structured step history for captcha-solve outcomes and
            # persist them as per-domain learnings the next worker will read.
            structured_path = os.path.join(task_dir, "step_history.json")
            if os.path.exists(structured_path) and domain:
                try:
                    with open(structured_path) as f:
                        structured = _json.load(f)
                    summary = _update_captcha_learnings(domain, structured.get("steps", []))
                    if summary and summary.get("winning_method"):
                        content += (
                            f"\n\n[Captcha Learnings Updated for {domain}]"
                            f"\nWinning method: {summary['winning_method']} "
                            f"({summary.get('winning_success_rate', '?')} success, "
                            f"{summary.get('winning_median_ms', '?')}ms median)"
                        )
                except Exception as exc:
                    print(f"  [captcha learnings update failed: {exc}]")

            # Copy checkpoint for potential re-delegation, stamping the
            # task fingerprint on the global copy so the next worker's
            # "Resume From Checkpoint" injection only fires for the SAME
            # task — not just any task on the same domain.
            cp_path = os.path.join(task_dir, "checkpoint.json")
            if os.path.exists(cp_path):
                try:
                    with open(cp_path) as _cpf:
                        cp_data = _json.load(_cpf)
                    if isinstance(cp_data, dict):
                        cp_data["task_fingerprint"] = _task_fingerprint(instructions)
                        with open("/tmp/superbrowser/last_checkpoint.json", "w") as _out:
                            _json.dump(cp_data, _out)
                except (ValueError, OSError, KeyError):
                    # Fall back to a raw copy if stamping fails — worst
                    # case the read-side guard just rejects it next time.
                    import shutil
                    shutil.copy2(cp_path, "/tmp/superbrowser/last_checkpoint.json")

            # --- Determine if the run was captcha-blocked (Layer 3) --------
            # Inspect the step_history.json for explicit captcha-fail signals.
            # A run counts as captcha-blocked when every browser_solve_captcha
            # step returned solved=false AND there was at least one such step.
            captcha_blocked = False
            had_captcha_step = False
            if os.path.exists(structured_path):
                try:
                    with open(structured_path) as f:
                        structured_data = _json.load(f)
                    for step in structured_data.get("steps", []):
                        if step.get("tool") != "browser_solve_captcha":
                            continue
                        had_captcha_step = True
                        result_text = step.get("result") or ""
                        # BrowserSolveCaptchaTool emits 'SOLVED' on success
                        # and 'NOT solved' otherwise — both uppercased in the
                        # summary prefix we standardised on in Phase 2.4.
                        if "NOT solved" in result_text or '"solved": false' in result_text:
                            captcha_blocked = True
                        elif "SOLVED" in result_text:
                            # Any success clears the blocked flag.
                            captcha_blocked = False
                            break
                    if had_captcha_step and not captcha_blocked:
                        # Mixed outcomes but the last was a success.
                        pass
                except (ValueError, OSError):
                    pass

            # Heuristic success: worker returned content that doesn't look
            # like a pure failure AND captcha didn't block us AND the site
            # wasn't blocking us at the network layer.
            lower_content = (content or "").lower()
            net_blocked_early = bool(
                worker_state.network_blocked
                or (content and "NETWORK_BLOCKED" in content)
            )
            looks_failed = (
                "browser worker failed" in lower_content
                or "captcha_unsolved" in lower_content
                or captcha_blocked
                or net_blocked_early
            )
            success = bool(content) and not looks_failed

            # --- Baseline metrics log (one JSONL line per worker exit) ----
            # Enables before/after comparison for reliability work. Fields are
            # all drawn from existing worker state — no new instrumentation.
            try:
                # _time is imported at module top; no local re-import needed.
                metrics_dir = "/tmp/superbrowser"
                os.makedirs(metrics_dir, exist_ok=True)
                metrics_path = os.path.join(metrics_dir, "metrics.jsonl")
                duration_sec = (
                    _time.time() - worker_state.start_time
                    if worker_state.start_time else 0.0
                )
                screenshots_used = (
                    worker_state.max_screenshots - worker_state.screenshot_budget
                )
                # Classify the block layer so per-domain decisions can rest
                # on data, not speculation:
                #   edge       — HTTP 4xx/5xx at the network layer (TLS / IP /
                #                bot-firewall refused content). Behavioral
                #                humanization will NOT help here; needs
                #                TLS/proxy fix.
                #   challenge  — got through edge but site served a captcha /
                #                challenge page. Needs captcha solve or
                #                cookie-jar warm.
                #   behavioral — page served, but interactions didn't produce
                #                expected results (empty responses, extracted
                #                elements missing). Humanization P1-P3 targets
                #                this class.
                #   none       — success.
                if success:
                    block_layer = "none"
                elif net_blocked_early:
                    block_layer = "edge"
                elif captcha_blocked:
                    block_layer = "challenge"
                else:
                    block_layer = "behavioral"
                metric = {
                    "ts": _time.time(),
                    "task_id": task_id,
                    "domain": domain or "",
                    "success": success,
                    "block_layer": block_layer,
                    "captcha_blocked": captcha_blocked,
                    "network_blocked": net_blocked_early,
                    "network_status": worker_state.last_network_status,
                    # Humanization level in effect this run. Hardcoded to
                    # 'light' today (jitter + humanClick + humanScroll via
                    # defaults; humanType via typeText default). Wire to
                    # config once per-domain policy lands.
                    "humanize_level": os.environ.get("SUPERBROWSER_HUMANIZE_LEVEL", "light"),
                    # Headless mode requested at launch. Lets us diff
                    # success rates between 'new' vs 'old' vs headful once
                    # there are enough runs.
                    "headless_mode": os.environ.get("SUPERBROWSER_HEADLESS_MODE", "new"),
                    "duration_sec": round(duration_sec, 2),
                    "screenshots_used": screenshots_used,
                    "vision_calls": worker_state.vision_calls,
                    "text_calls": worker_state.text_calls,
                    "sessions_opened": worker_state.sessions_opened,
                    "regression_count": worker_state.regression_count,
                    "step_count": len(worker_state.step_history),
                    "max_iterations": max_iterations,
                    "is_research": is_research,
                }
                with open(metrics_path, "a") as mf:
                    mf.write(_json.dumps(metric, default=str) + "\n")
            except OSError as exc:
                print(f"  [metrics log append failed: {exc}]")

            # Diagnostic: when we think the worker failed, surface the LAST
            # successful step's extracted content — if it holds real data that
            # the final message lost, we have a result-write race (the worker
            # exited before done() captured the extraction). Purely observational.
            if not success and os.path.exists(structured_path):
                try:
                    with open(structured_path) as f:
                        diag_struct = _json.load(f)
                    steps = diag_struct.get("steps", []) or []
                    last_good = next(
                        (s for s in reversed(steps)
                         if s.get("tool") in ("browser_run_script", "browser_eval",
                                              "browser_get_markdown", "browser_click",
                                              "browser_type")
                         and "error" not in str(s.get("result", "")).lower()
                         and "failed" not in str(s.get("result", "")).lower()),
                        None,
                    )
                    if last_good:
                        print(
                            f"  [diag] last_successful_step={last_good.get('tool')} "
                            f"result={str(last_good.get('result', ''))[:120]}"
                        )
                except (ValueError, OSError):
                    pass

            if domain:
                _record_routing_outcome(domain, "browser", success=success)

            # --- Resumption artifact bookkeeping (Priority 2) --------------
            # If the worker explicitly requested help via browser_request_help,
            # the tool already saved a rich artifact — don't overwrite it.
            # If the worker failed but didn't request help, save a minimal
            # artifact so the next delegation can still resume.
            # On success, clear any stale artifact from a prior failed run.
            # Never save a resumption for network-blocked sessions — resuming
            # them would just replay the same 4xx/5xx.
            #
            # Self-poisoning guard: if this run consumed a resumption artifact
            # and ALSO failed, saving a new artifact here would just seed the
            # NEXT delegation with the same broken state (sticky session that
            # leads the new worker's LLM straight back into the loop). Clear
            # instead so the next task starts clean.
            if success:
                clear_resumption_artifact()
            elif net_blocked_early:
                clear_resumption_artifact()
            elif resumption is not None:
                # We resumed and still failed. That means the resumption
                # artifact is the carrier of the bug, not a solution.
                print(
                    "\n>> resumption artifact consumed but task still failed "
                    "— clearing artifact instead of re-saving to stop the "
                    "self-poisoning loop"
                )
                clear_resumption_artifact()
            else:
                already_requested_help = any(
                    s.get("tool") == "browser_request_help"
                    for s in (worker_state.step_history or [])
                )
                if not already_requested_help and domain:
                    save_resumption_artifact(worker_state, domain)

            # --- Network-layer block fallback (pre-captcha) ----------------
            # NETWORK_BLOCKED means the site refused at the TLS/edge layer
            # (403/429/503 etc). No page-interaction trick will fix this;
            # the only remediations are different IP, different TLS
            # fingerprint, or giving up and searching public sources.
            # Route to search worker if viable, same as the captcha path,
            # but don't re-burn a browser attempt.
            network_blocked = bool(
                worker_state.network_blocked
                or (content and "NETWORK_BLOCKED" in content)
            )
            if network_blocked and not kw.get("_network_fallback_done"):
                print(
                    f"\n>> network block on {domain} "
                    f"(status={worker_state.last_network_status}) "
                    f"— not retrying browser, routing to search"
                )
                viable_for_search = classification["approach"] in ("search", "hybrid")
                if viable_for_search:
                    rewritten = _rewrite_for_search(instructions, url)
                    try:
                        # Local import to avoid a module-level circular
                        # dependency between search_tools and orchestrator_tools.
                        from superbrowser_bridge.search_tools import DelegateSearchTaskTool
                        search_tool = DelegateSearchTaskTool()
                        fallback_result = await search_tool.execute(
                            question=rewritten,
                            search_hints=(
                                f"Originally attempted via browser on {url or domain} "
                                f"but site returned HTTP {worker_state.last_network_status}. "
                                f"Use search snippets + public pages."
                            ),
                            force=True,
                            _fallback_from_browser=True,
                        )
                        return (
                            f"[Network-blocked on {domain} "
                            f"(HTTP {worker_state.last_network_status}) "
                            f"— auto-fell-back to search]\n\n"
                            f"{fallback_result}\n\n"
                            f"[Original browser attempt summary]\n{content[:500]}"
                        )
                    except Exception as fallback_exc:
                        print(f"  [fallback search also failed: {fallback_exc}]")
                # If search isn't viable, return the blocked result as-is
                # so the orchestrator sees the distinct signal and can
                # decide on proxies / human escalation / etc.
                return (
                    f"[NETWORK_BLOCKED on {domain} "
                    f"(HTTP {worker_state.last_network_status})] "
                    f"The site refused at the network/edge layer. Further browser "
                    f"attempts from this infrastructure will not succeed without "
                    f"a different IP or TLS fingerprint. "
                    f"Consider: (1) residential proxy, (2) different entry point URL, "
                    f"(3) delegate_search_task for public data only.\n\n"
                    f"[Original worker output]\n{content[:500]}"
                )

            # --- Layer 3: captcha-triggered search fallback ----------------
            # If the browser worker failed specifically because of a captcha,
            # AND the original task classifier said search/hybrid was viable,
            # retry via the search worker with an auto-rewritten query.
            # Capped at one fallback per task via the `_captcha_fallback_done`
            # sentinel in kw so re-entry doesn't loop.
            if captcha_blocked and not kw.get("_captcha_fallback_done"):
                viable_for_search = classification["approach"] in ("search", "hybrid")
                if viable_for_search:
                    print(f"\n>> captcha block on {domain} — falling back to delegate_search_task")
                    rewritten = _rewrite_for_search(instructions, url)
                    try:
                        from superbrowser_bridge.search_tools import DelegateSearchTaskTool
                        search_tool = DelegateSearchTaskTool()
                        fallback_result = await search_tool.execute(
                            question=rewritten,
                            search_hints=f"Originally attempted via browser on {url or domain} but blocked by captcha. Use search snippets + public pages.",
                            force=True,  # don't warn-back; this is a rescue
                            _fallback_from_browser=True,
                        )
                        return (
                            f"[Captcha-blocked on {domain} — auto-fell-back to search]\n\n"
                            f"{fallback_result}\n\n"
                            f"[Original browser attempt summary]\n{content[:500]}"
                        )
                    except Exception as fallback_exc:
                        print(f"  [fallback search also failed: {fallback_exc}]")

            # --- Anti-fabrication guard (in-context reminder) --------------
            # When the worker failed to retrieve real data, append an explicit
            # reminder so the orchestrator's NEXT turn — the one where it
            # writes the user-facing answer — sees it fresh. SOUL.md rules
            # alone aren't enough; the helpfulness prior is strong and the
            # rule lives far from the decision point.
            if not success:
                failure_class = (
                    "CAPTCHA_BLOCKED" if captcha_blocked
                    else "NETWORK_BLOCKED" if net_blocked_early
                    else "GENERIC_FAILURE"
                )
                lower_i = (instructions or "").lower()
                looks_transactional = any(
                    m in lower_i for m in (
                        "price", "cost", "rate", "fare", "booking",
                        "availability", "in stock", "tonight", "tomorrow",
                        "check-in", "checkin",
                        "january", "february", "march", "april", "may",
                        "june", "july", "august", "september", "october",
                        "november", "december",
                    )
                )
                if looks_transactional:
                    content += (
                        f"\n\n[FABRICATION GUARD — failure_class={failure_class}]\n"
                        f"The worker did NOT retrieve live data. When you write your "
                        f"final answer to the user, you MUST NOT include:\n"
                        f"  - Any specific numeric price (USD, BDT, or otherwise)\n"
                        f"  - 'Estimated', 'typical', 'market data suggests', 'approximately',\n"
                        f"    'based on historical', or any hedge wrapping an invented number\n"
                        f"  - Price ranges presented as factual for this search\n"
                        f"  - Named prices attached to hotels/products the worker did not\n"
                        f"    actually read from the live site\n"
                        f"Instead, say the retrieval failed, name the cause in plain terms, "
                        f"and offer concrete next steps (try a different site, check "
                        f"manually, call the hotel). A hedged-sounding invented number is "
                        f"WORSE than 'I don't know' — the user cannot tell they're fake."
                    )

            # Reset the outer-loop attempt counter on success so a later
            # legitimate re-run of the same task isn't counted against the
            # old budget.
            if success:
                _DELEGATION_ATTEMPTS.pop(_dedup_key, None)
            else:
                # Phase 4: stash the worker's content for the substantive-
                # result guard. Next call will inspect this on the 2nd
                # attempt and refuse re-delegation if it contains verified
                # findings (prices/addresses/etc.) the orchestrator should
                # be presenting to the user instead of discarding.
                _entry = _DELEGATION_ATTEMPTS.get(_dedup_key)
                if _entry is not None:
                    _DELEGATION_ATTEMPTS[_dedup_key] = (
                        _entry[0],
                        _entry[1],
                        content[:8000],  # cap so memory stays bounded
                    )

            return content

        except Exception as e:
            if domain:
                _record_routing_outcome(domain, "browser", success=False)
            error_msg = (
                f"Browser worker failed: {e}\n\n"
                f"[FABRICATION GUARD] Worker crashed — you have NO live data. "
                f"Do not invent prices or estimates in your final answer. "
                f"Report the failure honestly and suggest the user check "
                f"manually at the source URL."
            )
            print(f"\n>> Worker error: {error_msg}")
            return error_msg


@tool_parameters(
    tool_parameters_schema(
        site=StringSchema("Site domain or URL to check learnings for (e.g., 'gozayaan.com')"),
        required=["site"],
    )
)
class CheckLearningsTool(Tool):
    """Check what we've learned about a site from past tasks."""

    name = "check_learnings"
    description = (
        "Learning reads are disabled in this deployment "
        "(LEARNING_READS_ENABLED=0). Skip calling this; proceed straight "
        "to delegate_browser_task or delegate_search_task."
        if not learning_reads_enabled()
        else (
            "Read past learnings for a website. Returns what worked and what failed. "
            "ALWAYS call this before delegate_browser_task to avoid repeating mistakes."
        )
    )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, site: str, **kw: Any) -> str:
        if not learning_reads_enabled():
            return (
                f"Learning reads disabled (LEARNING_READS_ENABLED=0). "
                f"No prior history consulted for {site}; proceed with the task."
            )
        domain = _domain_from_url(site)
        path = _learnings_path(domain)

        # Always surface the routing preference if we have one — even if
        # no markdown learnings exist yet, past search/browser outcomes
        # are useful for the orchestrator's next decision.
        sections: list[str] = []
        pref = _preferred_approach(domain)
        if pref:
            sections.append(
                f"## Routing preference for {domain}\n"
                f"- Preferred: **{pref['approach']}** (confidence {pref['confidence']:.2f})\n"
                f"- Reason: {pref['reason']}\n"
                f"- Action: call `delegate_{pref['approach']}_task` first. "
                f"The classifier will also enforce this automatically."
            )

        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    md = f.read().strip()
                if md:
                    sections.append(f"## Task learnings for {domain}\n{md}")
            except OSError:
                pass

        if not sections:
            return f"No learnings or routing history found for {domain}. This is the first task on this site."

        # Preface: past learnings are PATTERNS, not verbatim answers.
        # Without this guidance, the LLM copies concrete values (e.g.
        # `makes[]=bmw` from a BMW task) into an unrelated follow-up
        # query (e.g. a Mercedes task on the same site).
        preamble = (
            "## How to use these learnings\n"
            "These are PATTERNS learned from PAST tasks on this site. "
            "They are NOT the answer to the current task. When you see "
            "concrete values in a URL / selector / script (e.g. a "
            "specific brand, year range, price, ID), treat them as "
            "placeholders you MUST replace with values from the CURRENT "
            "user task. If the current task is about a DIFFERENT entity "
            "than a past learning mentions, the URL pattern still "
            "applies — just swap the query params. Do NOT echo concrete "
            "values from these learnings back to the user as if they "
            "were the current task's answer."
        )
        return "\n\n".join([preamble, *sections])


@tool_parameters(
    tool_parameters_schema(
        site=StringSchema("Site domain or URL"),
        learning=StringSchema(
            "What worked or failed. Be specific: include URLs, selectors, error messages. "
            "Format: '- WORKED: ...' or '- FAILED: ...'"
        ),
        required=["site", "learning"],
    )
)
class SaveLearningTool(Tool):
    """Save a learning about a site for future tasks."""

    name = "save_learning"
    description = (
        "Save ACTIONABLE, GENERALIZABLE learnings for a site. Future "
        "workers and future tasks (possibly about different "
        "entities/queries on the same site) will read these. Include:\n"
        "- WORKING: URL patterns, selectors, script patterns, wait strategies\n"
        "- FAILED: what was tried and WHY it failed, with 'DO NOT:' instructions\n"
        "CRITICAL — abstract task-specific values into placeholders.\n"
        "  BAD:  URL: /shopping/results/?makes[]=bmw&year_min=2010&list_price_min=25000\n"
        "  GOOD: URL: /shopping/results/?makes[]=<brand>&year_min=<year_min>&list_price_min=<min_price>\n"
        "Brands, model names, dates, prices, zip codes, user-entered "
        "queries are ALL task-specific — use placeholders. Structural "
        "bits (param names, selector paths, wait durations, DOM "
        "patterns) stay concrete. If a value is specific to the "
        "CURRENT task, do not encode it into a learning that the NEXT "
        "task on the same site will read.\n"
        "Read the [Worker Activity Log] to extract generalizable "
        "patterns. Write learnings as step-by-step instructions a "
        "worker can directly execute after substituting placeholders."
    )

    async def execute(self, site: str, learning: str, **kw: Any) -> str:
        domain = _domain_from_url(site)
        path = _learnings_path(domain)

        from datetime import datetime
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

        entry = f"\n### {timestamp}\n{learning}\n"

        with open(path, "a") as f:
            f.write(entry)

        return f"Learning saved for {domain}."


def register_orchestrator_tools(bot: "Nanobot") -> None:
    """Register orchestrator-specific tools (delegation + learnings + search)."""
    from superbrowser_bridge.search_tools import register_search_tools
    from superbrowser_bridge.antibot import (
        FetchArchiveTool,
        FetchAutoTool,
        FetchImpersonateTool,
        FetchUndetectedTool,
    )

    tools = [
        DelegateBrowserTaskTool(),
        CheckLearningsTool(),
        SaveLearningTool(),
        FetchAutoTool(),
        FetchImpersonateTool(),
        FetchUndetectedTool(),
        FetchArchiveTool(),
    ]
    for tool in tools:
        bot._loop.tools.register(tool)

    # Register the search delegation tool
    register_search_tools(bot)

    # Remove direct web search tools — orchestrator must delegate ALL web
    # research to the search worker (API-based) or browser worker (browser-based).
    for name in ("web_search", "web_fetch"):
        bot._loop.tools.unregister(name)
