"""DelegateBrowserTaskTool — orchestrator's primary entry into the
worker agent. ~1,400 lines of phase-orchestration: outer-loop circuit
breaker, pre-flight URL probe, classifier gate, fresh worker spawn,
captcha-learnings update, and post-execution block detection /
fallback to delegate_search_task.

Imports are kept maximal so the body of ``execute`` (preserved
verbatim from the legacy session_tools.py monolith) sees the same
namespace it always has.
"""

from __future__ import annotations

import hashlib
import json as _json
import os
import time as _time
import uuid
from pathlib import Path
from typing import Any

import httpx

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    ArraySchema,
    BooleanSchema,
    NumberSchema,
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)

from superbrowser_bridge.routing import (
    LEARNINGS_DIR,
    TACTIC_ALTERNATIVES,
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
    tactic_penalty_summary,
)

from .captcha_learnings import (
    _domain_needs_human_handoff,
    _update_captcha_learnings,
)
from .constants import (
    BROWSER_WORKSPACE,
    _DELEGATION_ATTEMPTS,
    _DELEGATION_MAX_ATTEMPTS,
    _DELEGATION_WINDOW_SEC,
)
from .delegation_registry import _result_is_substantive, _task_fingerprint
from .url_probe import _probe_url

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
        task_checklist=ArraySchema(
            description=(
                "Decomposed list of constraints / conditions extracted from "
                "the user query. REQUIRED for any query with 2+ filters, "
                "conditions, or sequenced steps — the worker uses this to "
                "track per-constraint progress and refuses to claim success "
                "while items remain open. The worker will see [BRIEF] / "
                "[FOCUS] / [CHECKLIST] blocks pinned to every tool result. "
                "Pass null / omit for single-condition tasks. See orchestrator "
                "SOUL.md 'Decomposing multi-condition queries' for the full "
                "predicate vocabulary and a worked example."
            ),
            items=ObjectSchema(
                label=StringSchema(
                    "Short human-readable label for the constraint, e.g. "
                    "'Oregon region', 'Price under $40', 'Pairs with fish'. "
                    "Shown to the worker on every tool result."
                ),
                kind=StringSchema(
                    "One of: filter | action | extraction | navigation | "
                    "verification. Defaults to 'filter'. Hint to the worker "
                    "about what flavor of progress the item demands.",
                    nullable=True,
                ),
                predicate=ObjectSchema(
                    description=(
                        "How the item auto-completes. ANY listed match flips "
                        "it to done. Keys: url_contains (list[str]), "
                        "url_param (object key→list[str]), page_text "
                        "(list[str]), vision_active_label (list[str]), "
                        "manual (bool). For action/extraction items use "
                        "{manual: true} and the worker calls "
                        "browser_brief_mark when verified."
                    ),
                    nullable=True,
                ),
                required=["label"],
            ),
            nullable=True,
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
        task_checklist: list[dict] | None = None,
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
        #
        # Decision rule:
        #   - hard_unreachable: genuine NXDOMAIN / SSL / origin 5xx -> abort.
        #   - soft_blocked:     WAF-fronted, edge dropped our datacenter IP
        #                       or served a block page. Spawn the Tier-3
        #                       worker anyway (residential proxy + patchright
        #                       has a real shot). Surface a warning hint.
        #   - ok:               proceed normally.
        _probe_warning: str | None = None
        if url:
            probe = await _probe_url(url)
            classification = probe.get("classification") or (
                "hard_unreachable" if probe.get("unreachable")
                else ("soft_blocked" if probe.get("blocked") else "ok")
            )
            if classification == "hard_unreachable":
                print(f"\n>> pre-validation: {url} is unreachable: {probe['error']}")
                return (
                    f"[URL_UNREACHABLE] {url} is not reachable: {probe['error']}. "
                    f"Do NOT delegate to browser — the site is down or the URL "
                    f"is wrong. Either fix the URL or use delegate_search_task."
                )
            if classification == "soft_blocked":
                protection = probe.get("protection") or "unknown"
                reason_bits = probe.get("reason") or probe.get("error") or "(no detail)"
                # Strong WAF classes warrant the full "use T3 + captcha"
                # nudge to the worker. Weak classes (`generic`, `structural`,
                # `empty`) just mean "the curl_cffi response was JS-heavy
                # / minimal" — almost always a normal SPA that any browser
                # tier renders fine. Pushing the worker toward T3 +
                # captcha-solver in those cases skips the cheaper T1 path
                # entirely and ends up stuck on Cloudflare interstitials
                # for sites that didn't need T3 to begin with.
                STRONG_WAF_CLASSES = (
                    "akamai", "akamai_suspected", "cloudflare", "datadome",
                    "imperva", "perimeterx", "kasada", "sucuri",
                    "rate_limited", "antibot_403",
                )
                is_strong_waf = protection in STRONG_WAF_CLASSES
                print(
                    f"\n>> pre-validation: soft-block "
                    f"(protection={protection}) — routing to worker"
                    f"{' [strong WAF]' if is_strong_waf else ' [weak signal — letting worker pick tier]'}"
                )
                if is_strong_waf:
                    remediation_hint = (
                        "Akamai sensor_data is NOT 2captcha-solvable — it needs the "
                        "humanized-mouse pass or human handoff."
                        if protection in ("akamai", "akamai_suspected")
                        else (
                            "Cloudflare Turnstile is 2captcha-solvable via "
                            "browser_solve_captcha(method='auto')."
                            if protection == "cloudflare"
                            else "Use browser_solve_captcha(method='auto') first; "
                                 "fall back to human handoff if that fails."
                        )
                    )
                    _probe_warning = (
                        f"\n## Pre-validation Notice\n"
                        f"Probe could not reach {url} cleanly "
                        f"(protection={protection}, status={probe.get('status', 0)}, "
                        f"reason={reason_bits}). This is the expected path for "
                        f"WAF-fronted sites; the worker uses residential proxy + "
                        f"patchright and has a real shot. {remediation_hint}"
                    )
                else:
                    # Weak signal: mention it without commanding T3. Let the
                    # worker default to tier="auto" so the in-call T1→T3
                    # auto-escalation can decide based on real status codes.
                    _probe_warning = (
                        f"\n## Pre-validation Notice\n"
                        f"Probe of {url} returned a thin/JS-heavy body "
                        f"(protection={protection}, status={probe.get('status', 0)}, "
                        f"reason={reason_bits}). This is most likely a normal "
                        f"JS-rendered site, NOT a WAF block. Use "
                        f"`browser_open(tier=\"auto\")` and let the runtime "
                        f"decide; do NOT force tier=\"t3\" or pre-emptively "
                        f"call browser_solve_captcha based on this notice."
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

        # Multi-condition query tracking. When the orchestrator passed a
        # decomposed checklist, attach a TaskBrief so the worker hook
        # can pin live [BRIEF]/[FOCUS]/[CHECKLIST] blocks onto every
        # tool result. When the orchestrator forgot to pass one — which
        # happens regularly because LLMs forget optional params — we
        # fall back to a heuristic decomposition. The heuristic uses
        # ``manual: true`` predicates so nothing auto-flips, but the
        # brain still gets a checklist + periodic task reminder, which
        # alone solves most "lost-condition" reports.
        effective_checklist = task_checklist
        checklist_source = "orchestrator"
        if not effective_checklist:
            try:
                from superbrowser_bridge.task_brief import heuristic_decompose
                effective_checklist = heuristic_decompose(instructions)
                if effective_checklist:
                    checklist_source = "heuristic"
            except Exception as exc:
                print(f"   [task_brief heuristic failed: {exc}]")
        if effective_checklist:
            try:
                worker_state.set_task_brief(instructions, effective_checklist)
                print(
                    f"   [task_brief attached source={checklist_source} "
                    f"constraints={len(worker_state.task_brief.constraints)} "
                    f"open={worker_state.task_brief.open_count()}]"
                )
                # Echo the constraints once so trace logs make it obvious
                # which list the worker is operating against.
                for c in worker_state.task_brief.constraints:
                    print(
                        f"     · #{c.id} [{c.kind}] {c.label[:80]} "
                        f"predicate={c.predicate}"
                    )
            except Exception as exc:
                print(f"   [task_brief failed to attach: {exc}]")
        else:
            print("   [task_brief: none — single-condition query or empty]")

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
            "(browser_screenshot, browser_click_at, browser_type_at, "
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
            "volume, any slider widget)? NEVER `browser_click_at` — clicks "
            "on slider thumbs do NOTHING. NEVER `browser_drag(x1,y1,x2,y2)` "
            "with guessed coords — it's open-loop and overshoots. Three "
            "tools, in order of preference:\n"
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
            "- **Stable CSS hook for a drag** (chess squares like `.square-54`, "
            "captcha drag handles)? `browser_drag_selectors` / "
            "`browser_drag_path` are the selector-anchored drag paths. "
            "Zero Gemini latency; pixel-exact centre.\n"
            "- **All clicks** go through `browser_click_at(vision_index=V_n)`. "
            "`V_n` MUST come from the most recent `browser_screenshot` "
            "reply — DOM-index `browser_click` and CSS-selector "
            "`browser_click_selector` were removed precisely so the brain "
            "always grounds on a fresh bbox before clicking."
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

        # When the orchestrator decomposed the query into a checklist,
        # render it into the system prompt so the brain reads the full
        # constraint list at task start. The worker hook re-pins the
        # live state on every tool result, but having it here too makes
        # the structure visible BEFORE the first action — important on
        # tasks where the very first click should already advance a
        # specific constraint.
        if worker_state.task_brief and worker_state.task_brief.constraints:
            parts.append(
                "\n## Constraints (every tool result re-pins live state)\n"
                + worker_state.task_brief.render_for_prompt()
                + "\n\nWork the [FOCUS] item. Filter constraints auto-flip "
                "to [done] when their predicate matches the URL or page "
                "text. For action / extraction items marked manual, call "
                "browser_brief_mark(constraint_id, status, evidence) when "
                "you have the evidence. Do NOT call done(success=True) "
                "while any constraint is still [open] or [active]."
            )

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
            "**`V_n` is a positional ID, not a ranking.** Bboxes are "
            "listed in spatial reading order (top-to-bottom, "
            "left-to-right). V1 is whatever element happens to sit at "
            "the top-left of the page — it is NOT 'the best target' or "
            "'what vision recommends'. **Read each bbox's label** and "
            "pick the V_n whose label most specifically matches the "
            "control your current step needs. Do NOT default to V1. "
            "If two bboxes are plausible, prefer the one whose label "
            "more specifically describes your target.\n"
            "\n"
            "Tool preference for element interaction:\n"
            "  1. **PRIMARY** `browser_click_at(vision_index=V_n)` — "
            "humanized cursor click on the bbox you picked by label. "
            "`V_n` MUST come from the most recent screenshot's vision "
            "output; do NOT invent indices or coords. This is the ONLY "
            "click pathway — DOM-index and CSS-selector clicks were "
            "removed so the brain always grounds on a fresh bbox.\n"
            "  2. **LAST RESORT** `browser_run_script(mutates=true)` — "
            "only when no bbox works or the action requires multi-step "
            "orchestration. JS clicks are isTrusted=false; many sites "
            "403 this; don't reach for it reflexively.\n"
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
            "`browser_get_markdown` (free) to inspect the page text. "
            "If a control is mentioned in the markdown but not yet "
            "labelled by vision, scroll it into view and re-screenshot "
            "so vision picks it up — there is no DOM-index fallback "
            "anymore, every click must come from a fresh `V_n`. "
            "`browser_image_region(bbox_json=...)` can grab a tight "
            "JPEG of a specific viewport region if you need to "
            "OCR/inspect something closely (captcha tiles, price text "
            "near a specific card).\n"
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
            "**Cascading dropdown filter forms (trade-in valuation, "
            "vehicle/laptop selectors, multi-step search filters): "
            "DEFAULT to `browser_form_plan` — DO NOT click your way "
            "through.** When you see a form with ≥2 dependent dropdowns "
            "(Brand → Model → Processor → RAM → Year, etc.), call:\n"
            "  `browser_form_plan(intent='...', fields=[\n"
            "      {label='Brand', value='Dell'},\n"
            "      {label='Processor Brand', value='Intel'},\n"
            "      {label='System Memory (RAM)', value='8 GB'},\n"
            "      ...])`\n"
            "The runtime label-anchors each pick (no DOM index, no V-"
            "index — both go stale between picks on these pages). On "
            "ambiguity, the tool returns the candidate list so you "
            "retry with a corrected `value` instead of clicking blind. "
            "For a SINGLE dropdown, use `browser_select_option(label="
            "'Brand', value='Dell')` — same label-anchored fast path.\n"
            "\n"
            "**IMPORTANT — when select_option/form_plan returns "
            "`trigger_not_found`:** the page has likely transitioned out "
            "of the dropdown stage (e.g. Best Buy trade-in: after Brand "
            "is picked, the page becomes a *results grid of specific "
            "models* — not more dropdowns). STOP retrying the same "
            "label. Call `browser_get_markdown` to inspect what's "
            "actually on the page now, then either:\n"
            "  • click a specific item from the result list with "
            "`browser_click_at(vision_index=V_n)` after a fresh screenshot, OR\n"
            "  • answer questions / type into text inputs as the new "
            "stage requires.\n"
            "Do NOT keep banging on browser_form_plan with new field "
            "names hoping a dropdown will appear — verify the page state "
            "first with markdown.\n"
            "\n"
            "Reach for `browser_select_option` / `browser_form_plan` "
            "BEFORE `browser_click_at` on any [role=combobox]/"
            "[role=listbox] trigger; these tools exist specifically to "
            "break the click-loop-on-shifting-DOM-indices failure that "
            "wastes tool budget on filter forms.\n"
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
            "`[type_at_failed:...]`, `[drag_selectors_failed]`) you got "
            "— if those captions haven't appeared, the lockout will refuse the script. "
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
- browser_click_at/type_at/wait_for — single actions, return updated elements.
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

            # Honest partial-completion surfacing. When the orchestrator
            # decomposed the query into a checklist, the brief tracks
            # which constraints were satisfied. If any remain open at
            # the end of the worker run, prepend an [INCOMPLETE_CHECKLIST]
            # block so the orchestrator can tell the user what's missing
            # rather than rubber-stamping a partial success.
            brief = getattr(worker_state, "task_brief", None)
            if brief is not None and not brief.is_complete():
                open_ct = brief.open_count()
                total = len(brief.constraints)
                if open_ct > 0:
                    content = (
                        f"[INCOMPLETE_CHECKLIST] {open_ct} of {total} "
                        f"constraints remained unverified after the worker "
                        f"finished. Open items:\n"
                        f"{brief.summary_open_items()}\n"
                        f"Original query: {brief.original_query}\n"
                        f"Treat the worker's reply below as PARTIAL — surface "
                        f"the missing constraints to the user, do not claim "
                        f"the task succeeded.\n\n"
                        + content
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
                                              "browser_get_markdown", "browser_click_at",
                                              "browser_type_at", "browser_type")
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
