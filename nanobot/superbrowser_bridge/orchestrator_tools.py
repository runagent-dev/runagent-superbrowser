"""
Orchestrator tools for the two-agent architecture.

The orchestrator delegates browser work to a fresh browser worker instance,
manages site-specific learnings, and never touches browser tools directly.
"""

from __future__ import annotations

import json as _json
import os
import uuid
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema

from superbrowser_bridge.routing import (
    LEARNINGS_DIR,
    _captcha_learnings_path,
    _classify_task,
    _domain_from_url,
    _learnings_path,
    _preferred_approach,
    _record_routing_outcome,
    _rewrite_for_search,
    _routing_path,
)

# Where the browser worker workspace lives (relative to this file)
_BASE = Path(__file__).resolve().parent.parent
BROWSER_WORKSPACE = str(_BASE / "workspace_browser")




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
    if os.path.exists(path):
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
        bucket = per_method.setdefault(method, {"attempts": 0, "solved": 0, "durations": []})
        bucket["attempts"] += 1
        if a.get("solved"):
            bucket["solved"] += 1
            if a.get("durationMs"):
                bucket["durations"].append(int(a["durationMs"]))

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
    summary = {
        "domain": domain,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "winning_method": best_method,
        "winning_success_rate": round(best_rate, 3) if best_rate >= 0 else None,
        "winning_median_ms": None if best_duration == float("inf") else int(best_duration),
        "success_rate_last_10": round(last10_success / len(last10), 3) if last10 else None,
        "cold": cold,
        "per_method": {
            m: {
                "attempts": b["attempts"],
                "solved": b["solved"],
                "success_rate": round(b["solved"] / b["attempts"], 3) if b["attempts"] else 0.0,
                "median_ms": int(statistics.median(b["durations"])) if b["durations"] else None,
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

    async def execute(self, instructions: str, url: str | None = None, force: bool = False, **kw: Any) -> str:
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
        from superbrowser_bridge.session_tools import BrowserSessionState, register_session_tools
        from superbrowser_bridge.worker_hook import BrowserWorkerHook

        task_id = uuid.uuid4().hex[:8]
        session_key = f"worker:{task_id}"

        # Research tasks need more iterations: search + visit 3-5 pages + refine
        _research_kw = ("search", "research", "find information", "google", "look up", "investigate")
        is_research = any(k in instructions.lower() for k in _research_kw)
        max_iterations = 40 if is_research else 25

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
        register_session_tools(worker, worker_state)

        # Create mid-session guardrail hook
        worker_hook = BrowserWorkerHook(worker_state, max_iterations=max_iterations)

        # 25 iterations — enough for: open + inspect + script + fail + retry + verify + close
        worker._loop.max_iterations = max_iterations

        # Build the worker prompt with enforced workflow structure
        parts = []

        if url:
            parts.append(f"Target URL: {url}")

        parts.append(f"\n## Task\n{instructions}")

        # Auto-inject learnings so the worker follows known patterns
        domain = _domain_from_url(url) if url else None
        if domain:
            lpath = _learnings_path(domain)
            if os.path.exists(lpath):
                with open(lpath) as f:
                    learnings = f.read().strip()
                if learnings:
                    parts.append(f"\n## Site Learnings (from past tasks — FOLLOW THESE)\n{learnings}")

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
        # Only resume if the checkpoint domain matches the current task domain
        # to prevent stale checkpoints from unrelated tasks.
        last_checkpoint_path = "/tmp/superbrowser/last_checkpoint.json"
        if os.path.exists(last_checkpoint_path):
            try:
                with open(last_checkpoint_path) as f:
                    checkpoint = _json.load(f)
                cp_url = checkpoint.get("url", "")
                cp_domain = _domain_from_url(cp_url) if cp_url else ""
                # Only inject checkpoint if it's for the same domain as the current task
                if cp_url and domain and cp_domain == domain:
                    parts.append(
                        f"\n## Resume From Checkpoint\n"
                        f"A previous worker made progress to: {cp_url}\n"
                        f"Start from this URL instead of the beginning. "
                        f"Call browser_open with this URL directly.\n"
                        f"Do NOT repeat the steps that led to this point."
                    )
                else:
                    # Stale checkpoint from a different domain — remove it
                    os.remove(last_checkpoint_path)
            except (ValueError, KeyError):
                pass

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
- You have {max_iter} iterations total. Use them to obtain REAL data — do not pre-announce a bail-out.
- If you see [GUIDANCE: ...] messages, follow them IMMEDIATELY.
- Return ALL findings with source URLs.
- NEVER invent, estimate, or guess values. If a data point genuinely cannot be retrieved, say so explicitly and return done(success=False) with a brief honest reason. Fabricated numbers with plausible-sounding disclaimers are a FAILURE.""".format(max_iter=max_iterations))
        else:
            parts.append("""
## Required Execution Plan (follow this order)
Step 1: browser_open(url) — open the target page (returns screenshot + interactive elements list)
Step 2: Read the elements list from Step 1 — you already have the selectors, no need for a separate browser_eval
Step 3: browser_run_script — write ONE script that does ALL form filling + submission + data extraction
  Use the element indices/selectors from Step 1. Use browser_wait_for instead of blind helpers.sleep().
  For autocomplete: page.type(field, text, {{delay:100}}) then browser_wait_for(text="suggestion") then click suggestion
Step 4: If script failed, read the error AND the updated elements list in the error response. Fix the script and run browser_run_script again IN THE SAME SESSION — do NOT navigate back to the start
Step 5: browser_get_markdown or browser_eval — read the results (or just read the elements returned by Step 3)
Step 6: browser_close

KEY FEATURES:
- Every action (click, type, scroll, run_script) returns updated interactive elements automatically — you don't need screenshots to see what changed
- browser_wait_for(session_id, text="...", timeout=10) waits for content to appear — MUCH better than helpers.sleep()
- browser_click has JS fallback if standard click fails

CRITICAL RULES:
- Prefer browser_run_script for multi-step interactions. Use browser_click/browser_type only for simple one-off actions.
- If a script fails, FIX IT and retry on the current page. Do NOT navigate backward.
- You have {max_iter} iterations total. After iteration 15, prefer browser_run_script to batch remaining work. Keep extracting until you have the real data.
- If you see [GUIDANCE: ...] messages, follow them IMMEDIATELY.
- NEVER invent, estimate, or guess values. If data genuinely cannot be extracted, return done(success=False) with a brief honest reason. Fabricating plausible numbers is a FAILURE.""".format(max_iter=max_iterations))

        prompt = "\n".join(parts)

        try:
            result = await worker.run(prompt, session_key=session_key, hooks=[worker_hook])
            content = result.content

            print(f"\n>> Worker result ({len(content)} chars): {content[:200]}...")

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

            # Copy checkpoint for potential re-delegation
            cp_path = os.path.join(task_dir, "checkpoint.json")
            if os.path.exists(cp_path):
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
            # like a pure failure AND captcha didn't block us.
            lower_content = (content or "").lower()
            looks_failed = (
                "browser worker failed" in lower_content
                or "captcha_unsolved" in lower_content
                or captcha_blocked
            )
            success = bool(content) and not looks_failed

            if domain:
                _record_routing_outcome(domain, "browser", success=success)

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

            return content

        except Exception as e:
            if domain:
                _record_routing_outcome(domain, "browser", success=False)
            error_msg = f"Browser worker failed: {e}"
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
        "Read past learnings for a website. Returns what worked and what failed. "
        "ALWAYS call this before delegate_browser_task to avoid repeating mistakes."
    )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, site: str, **kw: Any) -> str:
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

        return "\n\n".join(sections)


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
        "Save ACTIONABLE learnings for a site. Future workers will follow these directly. Include:\n"
        "- WORKING: exact script patterns (browser_run_script code), URL patterns, selectors, wait strategies\n"
        "- FAILED: what was tried and WHY it failed, with 'DO NOT:' instructions\n"
        "Read the [Worker Activity Log] to extract successful patterns. "
        "Write learnings as step-by-step instructions a worker can directly execute."
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

    tools = [
        DelegateBrowserTaskTool(),
        CheckLearningsTool(),
        SaveLearningTool(),
    ]
    for tool in tools:
        bot._loop.tools.register(tool)

    # Register the search delegation tool
    register_search_tools(bot)

    # Remove direct web search tools — orchestrator must delegate ALL web
    # research to the search worker (API-based) or browser worker (browser-based).
    for name in ("web_search", "web_fetch"):
        bot._loop.tools.unregister(name)
