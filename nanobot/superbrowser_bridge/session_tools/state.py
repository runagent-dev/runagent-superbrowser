"""BrowserSessionState — per-instance state for the worker bot.

Holds session id, screenshots taken, checkpoints, vision response
caches, repeat / regression / cursor-failure counters, and the methods
that read / mutate them. Each Nanobot instance that registers browser
tools owns its own state object; the orchestrator and worker bots are
isolated even when they share the same Python process.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from .constants import SCREENSHOT_DIR
from .telemetry import _compute_screenshot_budget
from .vision_sync import (
    _await_vision_required,
    _push_vision_bboxes,
    _read_image_dims,
)


@dataclass
class PageStateSnapshot:
    """Cheap structural snapshot of the page state at a moment in time.

    Captured at the top of every mutating tool's ``execute()`` and again
    from the tool's HTTP response payload. The diff between the two
    snapshots is rendered as ``[ACTION_DELTA]`` in the next tool result
    so the brain reads a one-line "what just changed because of my
    action" signal instead of having to re-derive it from a fresh
    screenshot.

    All fields use cached state — there are NO new HTTP roundtrips for
    snapshot capture. ``elem_count`` and ``sample_labels`` come from
    the elements text the prior tool's response already contained.
    """

    url: str = ""
    title: str = ""
    elem_count: int = 0
    dom_hash: str = ""
    fingerprint_keys: frozenset = field(default_factory=frozenset)
    sample_labels: tuple = field(default_factory=tuple)
    vision_bbox_count: int = 0
    captured_at_turn: int = 0
    target_index: Optional[int] = None
    target_fingerprint: str = ""


class BrowserSessionState:
    """Per-instance state for browser session tools.

    Each Nanobot instance that registers browser tools gets its own state.
    This prevents multi-agent setups from sharing globals.
    """

    # Default budget when no task context is supplied. Use
    # configure_budget() to switch to complexity-aware allocation.
    DEFAULT_SCREENSHOT_BUDGET = 6
    CAPTCHA_MODE_ITERATIONS = 15
    MAX_CLICK_AT = 3

    def __init__(self):
        self.max_screenshots = self.DEFAULT_SCREENSHOT_BUDGET
        self.screenshot_budget = self.max_screenshots
        self.vision_calls = 0
        self.text_calls = 0
        self.start_time = 0.0
        self.sessions_opened = 0
        self.activity_log: list[str] = []
        # Per-session (reset on each browser_open)
        self.step_counter = 0
        self.click_at_count = 0
        self.action_count = 0
        self.actions_since_screenshot = 0

        # Checkpoint & URL tracking
        self.task_id: str = ""
        self.checkpoints: list[dict] = []
        self.current_url: str = ""
        self.best_checkpoint_url: str = ""
        self.url_visit_counts: dict[str, int] = {}
        self.regression_count: int = 0
        # Dedupe key: (normalized_url, hash_of_content) — so a same URL with
        # changed content (e.g., after clicking "Load more") still allows a new
        # screenshot. Populated in mark_screenshot_taken().
        self.screenshotted_keys: set[tuple[str, str]] = set()
        self.last_screenshot_url: str = ""
        self.last_page_content_hash: str = ""
        self.step_history: list[dict] = []
        # Track consecutive click-type tool calls for loop detection
        self.consecutive_click_calls: int = 0
        # Hard guard against the brain re-clicking a target that produced
        # no DOM change. Cleared by `register_click_attempt` on a fresh
        # target; incremented when the same target re-fires AND the page
        # didn't change since the previous click. The guard refuses to
        # dispatch the (MAX_CONSECUTIVE_SAME_TARGET)th attempt — i.e.
        # one retry is allowed (some JS buttons genuinely need a second
        # click), but the third strike returns a structured error so the
        # brain is forced to switch tactic.
        self.last_click_target: str = ""
        self.last_click_dom_hash: str = ""
        self.consecutive_dead_clicks: int = 0
        self.MAX_CONSECUTIVE_SAME_TARGET = 3
        # Cross-index flail guard. consecutive_dead_clicks only catches
        # REPEATS of the same target. When the brain walks
        # [21]→[22]→[20] with every dispatch timing out, each looks like
        # a fresh target so that guard resets. Track HTTP timeouts
        # independently so two-in-a-row forces a re-screenshot.
        self.consecutive_click_timeouts: int = 0
        self.MAX_CONSECUTIVE_CLICK_TIMEOUTS = 2
        # Telemetry: how many times the TS-side snap-to-interactive
        # failed to find a clickable descendant inside the bbox we sent.
        # Incremented whenever a click response has snap.snapped=false.
        # Reset on every screenshot. Used to surface "vision bboxes are
        # habitually wrapping non-clickable containers" hints.
        self.snap_miss_count: int = 0
        # Active session ID (set by browser_open)
        self.session_id: str = ""
        # How many times has BrowserOpenTool had to refuse a redundant call?
        # The guard returns a stern message on the first few; if the LLM keeps
        # ignoring it past BLOCKED_BROWSER_OPEN_HARD_STOP, we raise to abort
        # the worker rather than silently drain its iteration budget.
        self.blocked_browser_open_count: int = 0

        # Captcha-mode: when a captcha is detected, relax the "no actions since
        # last screenshot" rule for CAPTCHA_MODE_ITERATIONS iterations. The
        # per-round counter `captcha_solve_round` is included in the dedup key
        # so each solve attempt gets its own screenshot allowance — preventing
        # the blanket bypass that used to let the worker screenshot the same
        # unchanged captcha 15 times.
        self.captcha_mode: bool = False
        self.captcha_mode_remaining: int = 0
        self.captcha_solve_round: int = 0

        # Cloudflare-interstitial navigation guard: set by BrowserNavigateTool
        # when the server reports block_class=cloudflare, cleared by a
        # successful browser_solve_captcha or navigation to a different URL.
        # While set, a repeat navigate to the same URL is refused with a
        # structured error telling the agent to solve first.
        self.last_nav_cf_blocked_url: str = ""
        self.nav_solve_called_since_block: bool = False

        # Per-index fingerprint cache. Populated on each /state fetch; read
        # by click/type tools to send `expected_fingerprint` along with the
        # request. Lets the TS side reject clicks that would land on a
        # different element than the LLM originally targeted (stale index).
        self.element_fingerprints: dict[int, str] = {}
        self.captcha_screenshots_used: int = 0
        # Hard cap on screenshots allowed within captcha mode (across rounds).
        # Kicks in only in captcha mode; the normal budget still caps overall.
        self.captcha_mode_screenshot_cap: int = 8

        # Network-layer block: set by browser_open/browser_navigate when the
        # target returns 4xx/5xx. Distinguishes "site blocks automated clients
        # before any page loads" from "page loaded but interaction failed" —
        # completely different failure classes needing different remediations
        # (IP/TLS/proxy vs. selector/timing/captcha).
        self.network_blocked: bool = False
        self.last_network_status: int | None = None

        # Human-in-the-loop handoff: when True, the TS server registers a
        # HumanInputManager for this session AND the captcha orchestrator
        # will fall back to human handoff after auto-strategies exhaust.
        # Orchestrator sets this before register_session_tools(). Default
        # False to preserve no-op behavior for workers that don't opt in.
        self.human_handoff_enabled: bool = False
        # Per-session budget — relayed to the TS server which enforces it.
        # 1 by default, overridable via SUPERBROWSER_MAX_HUMAN_HANDOFFS.
        self.human_handoff_budget: int = 1

        # Domain pinning: when set, BrowserNavigateTool rejects URLs
        # outside this domain (+ subdomains) and a small safe-list
        # (google.com, etc.). Prevents the worker LLM from hallucinating
        # to alternative sites when the target blocks it.
        self.pinned_domain: str = ""

        # Vision preprocessor bookkeeping. Populated inside
        # build_tool_result_blocks so tools that don't pass an intent
        # explicitly inherit the last one used — useful when the brain
        # fires a chained sequence (navigate → click → verify) with the
        # same underlying intent.
        self._last_intent: str = ""
        self._last_dom_hash: str = ""
        self._last_vision_summary: str = ""
        # Task context stamped by configure_budget() when the orchestrator
        # spawns a browser worker — piped into the vision prompt so
        # Gemini knows WHAT the user is trying to do before it picks
        # which bboxes to emit.
        self.task_instruction: str = ""
        self.task_target_url: str = ""
        # Cached last VisionResponse so browser_click_at can resolve a
        # vision-index reference (e.g. bbox=V3) back to the original
        # bbox without re-running the vision pass. Reset whenever a new
        # screenshot triggers a fresh vision call.
        # Typed as the actual schema when available, falls back to Any
        # so the import stays lazy for environments without vision_agent.
        try:
            from nanobot.vision_agent.schemas import VisionResponse as _VR  # noqa: F401
            self._last_vision_response: Optional["_VR"] = None  # type: ignore[assignment]
        except Exception:
            self._last_vision_response: Any = None  # type: ignore[assignment]
        # Freshness bookkeeping for the cached vision response. Mutating
        # tools read these to decide whether to piggyback
        # `_last_vision_response.as_brain_text()` onto their text reply —
        # that's the fast path that keeps bboxes in front of the brain
        # without a browser_screenshot round trip.
        self._last_vision_ts: float = 0.0
        self._last_vision_url: str = ""
        # Vision epoch — a frozen snapshot of the vision response that
        # was LAST emitted to the brain as screenshot text. Tools that
        # resolve `vision_index` (click_at, type_at, fix_text_at,
        # drag_slider_until) read this FIRST, falling back to
        # `_last_vision_response` only when the epoch is None. Needed
        # because background vision prefetches overwrite
        # `_last_vision_response` between screenshot-text-emit and
        # click-dispatch — without the epoch, the brain's `V_n`
        # picked from the screenshot resolves against a RENUMBERED
        # prefetch response and lands on the wrong element. Advanced
        # only when `BrowserScreenshotTool` emits fresh vision text;
        # cleared on reset_per_session and on successful navigate.
        self._vision_epoch_id: int = 0
        self._vision_epoch_response: Any = None
        # URL the epoch was captured on. F5 — when `current_url` no
        # longer matches this, the epoch is stale (page implicitly
        # navigated via Enter / form submit / button click) and
        # `vision_for_target_resolution` falls back to the live
        # `_last_vision_response` so the next click resolves V_n
        # against the new page's bbox list, not the prior page's.
        self._vision_epoch_url: str = ""

        # Dead-type guard state. Tracks the last browser_type call so we can
        # reject a second identical type to the same index — the pattern
        # that produces "khulnakhulna, bangladesh" when the LLM misses an
        # autocomplete dropdown and re-types the full phrase.
        self.last_type_index: int = -1
        self.last_type_text: str = ""
        self.last_type_at: float = 0.0

        # Brain-turn stamp of the last context-gathering action: a
        # browser_screenshot, browser_get_markdown, or browser_brief_mark.
        # Used by the navigate-deliberation gate to refuse rapid-fire
        # browser_navigate calls during a task_brief — the brain has to
        # ground itself in current state before each navigation, not
        # ricochet between URLs without seeing what's actually rendered.
        self.last_deliberation_turn: int = 0

        # Cached element bounds (rects + text) keyed by index. Populated
        # lazily by _fetch_elements_with_bounds before a click; consumed
        # by the DOM↔vision crosscheck so we can refuse browser_click
        # calls whose target rect doesn't overlap any vision bbox the
        # brain just saw. Cleared whenever the URL or DOM changes.
        # Shape: {int_index: {"bounds": {"x":..,"y":..,"w":..,"h":..}, "text": str}}
        self.elements_bounds: dict[int, dict] = {}

        # Cross-index repeat-type ledger. The same-index dead-type guard
        # above misses the cascade pattern observed in long traces: brain
        # types "40" into index=33 (no visible confirm), retries with
        # index=41, then vision_index=4 — three different addressers
        # pointing at the same price field. Each individual guard
        # passes; the user sees flailing. This ledger is index-agnostic:
        # it tracks (text, when) and refuses on the 3rd hit within
        # REPEAT_WINDOW_S so the brain is forced to take a screenshot
        # and verify what actually happened.
        self.recent_typed_values: list[tuple[str, float]] = []

        # Hierarchical perceive-plan-act state. Populated by the
        # screenshot tool after a vision pass; consumed by the click
        # ladder and the browser_plan_next_steps tool.
        #   _last_blockers       — DOM-derived blockers from ui_blockers.detect
        #   _last_action_queue   — ActionQueue from action_planner.plan
        #   _pending_postcondition — Postcondition dict the next click is
        #                            supposed to satisfy; verify_action
        #                            checks it after click_at returns.
        self._last_blockers: list = []  # list[BlockerInfo]
        self._last_action_queue: Any = None  # Optional[ActionQueue]
        self._pending_postcondition: Optional[dict] = None

        # Phase 3.1: cursor-failure ledger. Each cursor-based interaction
        # tool that returns a failure caption records its strategy here
        # so BrowserRunScriptTool(mutates=true) can refuse to run until
        # at least 2 distinct cursor strategies have been tried and
        # failed. Eliminates the brain's reflex of "click failed → run
        # JS to click" which trips Cloudflare/Akamai isTrusted=false
        # detection. `cursor_failure_strategies` records DISTINCT
        # strategies for the lockout decision; `cursor_failure_records`
        # keeps the last few entries for the prompt-side hint.
        self.cursor_failure_strategies: set[str] = set()
        self.cursor_failure_records: list[dict[str, Any]] = []

        # Phase 2: per-form orchestration. None when no form_begin has
        # been called; populated with a FormFillSession instance while
        # the brain is filling a multi-field form. The worker hook
        # injects a remaining-fields checklist into every tool result
        # while this is set, and form_commit verifies field values
        # before allowing submit.
        self.form_session: Any = None  # Optional[FormFillSession]

        # Multi-condition query tracking. Populated by the orchestrator
        # via set_task_brief() at delegation time, reconciled by the
        # worker hook on every iteration, and rendered as
        # [BRIEF]/[FOCUS]/[CHECKLIST] blocks into the next tool result.
        # None when the orchestrator didn't supply a checklist (e.g.
        # single-condition tasks); the hook then skips this branch and
        # behaves exactly as before.
        self.task_brief: Any = None  # Optional["TaskBrief"]
        # Last markdown body fetched by browser_get_markdown — fed into
        # task_brief.reconcile_from_page_state so page_text predicates
        # can flip without requiring a vision pass on text-heavy pages.
        self._last_markdown: str = ""

        # Action-delta snapshot. Captured at the top of every mutating
        # tool's execute() via capture_action_snapshot(); read inside
        # build_text_only() to compute the [ACTION_DELTA] block emitted
        # in the next tool result. Cleared after the delta is rendered
        # so a stale snapshot can't leak across tools.
        self.action_snapshot_pre: Optional[PageStateSnapshot] = None
        # Last computed action delta — populated by _emit_action_delta
        # at the end of each mutating tool. Read by the worker hook to
        # decide whether to reset the brief's stagnation counter on real
        # page progress that didn't (yet) flip a brief predicate.
        self.last_action_delta: Optional[dict] = None
        # Side-channel for tool-specific delta facts (e.g. browser_type's
        # before/after field values). Mutating tools may set this dict
        # before returning; build_text_only's renderer reads it to
        # produce a richer [ACTION_DELTA] than the structural diff alone
        # can express. Cleared together with action_snapshot_pre.
        self.action_snapshot_extras: dict = {}

        # URL-failure ledger. Populated when navigate response shows
        # status >= 400. Subsequent navigates to any URL in this dict
        # are refused with [URL_KNOWN_BAD] before any HTTP roundtrip.
        # Keyed by _normalize_url (so trailing-slash etc. variants
        # don't bypass the lockout).
        self.failed_navigation_urls: dict = {}

        # Per-focus navigate lockout. Set to the focus_id whose navigate
        # was just refused (filter_hack / detail_nav / deliberation_gate).
        # The next navigate against the same focus is rejected with a
        # stronger [NAV_LOCKED_FOR_FOCUS] message that points the brain
        # at browser_brief_mark or screenshot+click. Cleared when (a) the
        # focus advances, or (b) a screenshot / brief_mark / get_markdown
        # runs (the brain has re-grounded itself).
        self.last_navigate_refusal_focus_id: Optional[int] = None
        # One-shot flag set by browser_rewind_to_checkpoint. The worker
        # hook reads this and, if the brain's next call is anything other
        # than screenshot / get_markdown / brief_mark, injects a
        # high-priority [REWIND_NOT_OBSERVED] warning. Cleared by the
        # next deliberation tool.
        self.rewind_just_fired: bool = False

        # Phase 1: hard sync gate. Tracks the most recent prefetch task
        # so the NEXT mutating tool can wait for it before acting on
        # potentially-stale state. Replaces the soft 2s budget that
        # otherwise lets the brain proceed on cached vision when the
        # prefetch hasn't landed.
        self._pending_vision_task: Optional["asyncio.Task[Any]"] = None
        # DOM-dirty signal from the TS-side MutationObserver. The /state
        # endpoint returns `domDirty: true` when the DOM mutated since
        # the last /state read. The bridge sets this so the next
        # mutating tool's gate can force a fresh vision pass even if no
        # mutating tool fired (lazy-load, hover-revealed menu, JS animation).
        self._dom_dirty_at_last_state: bool = False
        # One-shot force-fresh flag. Set by browser_screenshot before
        # triggering a prefetch so the prefetch bypasses the agent's
        # cache and pays a fresh Gemini call. Reset after the prefetch
        # consumes it.
        self._force_vision_refresh: bool = False
        # Wall-clock + brain-turn stamp captured each time the screenshot
        # tool freezes a new vision epoch. Used by the freshness gate to
        # reject clicks against an epoch that's older than
        # VISION_MAX_AGE_TURNS brain turns. Counts MUTATING tool calls
        # rather than wall time so a 30s "thinking" pause doesn't
        # invalidate vision but two intermediate actions do.
        self._vision_epoch_taken_at: float = 0.0
        self._vision_epoch_turn: int = 0
        # Brain turn counter. Incremented at the top of every mutating
        # tool (click/type/click_at/scroll/navigate). Read by the
        # freshness gate to compute epoch age in turns.
        self._brain_turn_counter: int = 0

    @property
    def backend(self) -> str:
        """Tier of the active session. `t3` for patchright (undetected
        Chromium), `t1` for Puppeteer via the TS server. Derived from
        session_id prefix.
        """
        return "t3" if self.session_id.startswith("t3-") else "t1"

    # --- budget configuration ---------------------------------------------

    def configure_budget(
        self,
        task_instruction: str = "",
        target_url: str = "",
        is_research: bool = False,
    ) -> int:
        """Set screenshot budget based on task complexity. Returns new budget."""
        self.max_screenshots = _compute_screenshot_budget(
            task_instruction=task_instruction,
            target_url=target_url,
            is_research=is_research,
        )
        self.screenshot_budget = self.max_screenshots
        # Capture the task context so the vision agent can reason about
        # what the agent is trying to do on this site when picking which
        # regions to bbox. "Book a flight on trip.com" → the vision agent
        # should prioritize departure / destination / date / search
        # button bboxes, not navbar noise.
        # Keep the FULL task text — multi-condition queries routinely run
        # past 500 chars and the prior cap silently dropped half of them.
        # The vision prompt builder applies its own length budget at the
        # call site; truncating here was the wrong layer.
        self.task_instruction = task_instruction or ""
        self.task_target_url = target_url or ""
        return self.max_screenshots

    def set_task_brief(self, original_query: str, checklist: Any) -> None:
        """Attach a TaskBrief constructed from the orchestrator's checklist.

        ``checklist`` is a list of ``{label, kind, predicate}`` dicts as
        documented in the orchestrator SOUL.md. Empty / falsy input
        leaves ``self.task_brief`` as ``None`` so the worker hook skips
        the brief-injection branch and behaves like the pre-feature path.
        """
        if not checklist:
            self.task_brief = None
            return
        try:
            from superbrowser_bridge.task_brief import TaskBrief
        except Exception:
            # Defensive — if the module fails to import for any reason
            # we keep behaving like the legacy code path rather than
            # crashing the worker.
            self.task_brief = None
            return
        self.task_brief = TaskBrief(original_query, checklist)

    def enter_captcha_mode(self) -> None:
        """Relax screenshot limits for the next N iterations.

        Called when browser_detect_captcha returns a captcha. Captcha
        solving requires multiple screenshots per round (before drag,
        after drag, verify result) — normal budget would starve it.
        Resets per-round counters so a re-entry doesn't inherit stale
        dedup state from a previous challenge.
        """
        self.captcha_mode = True
        self.captcha_mode_remaining = self.CAPTCHA_MODE_ITERATIONS
        self.captcha_solve_round = 0
        self.captcha_screenshots_used = 0

    def tick_captcha_mode(self) -> None:
        """Decrement captcha_mode counter. Call once per agent iteration."""
        if not self.captcha_mode:
            return
        self.captcha_mode_remaining -= 1
        if self.captcha_mode_remaining <= 0:
            self.captcha_mode = False
            self.captcha_mode_remaining = 0

    def reset_per_session(self):
        """Reset per-session counters. Budget is NOT reset."""
        self.step_counter = 0
        self.click_at_count = 0
        self.action_count = 0
        self.actions_since_screenshot = 0
        # Epoch from a prior session is meaningless for the new one.
        self._vision_epoch_response = None
        self._vision_epoch_id = 0
        self._vision_epoch_url = ""
        self._vision_epoch_taken_at = 0.0
        self._vision_epoch_turn = 0
        # Drop any in-flight prefetch from the previous session — the
        # task references the old session_id and would write into
        # _last_vision_response under a context the new session doesn't
        # care about.
        if self._pending_vision_task is not None and not self._pending_vision_task.done():
            try:
                self._pending_vision_task.cancel()
            except Exception:
                pass
        self._pending_vision_task = None
        self._brain_turn_counter = 0

    def freeze_vision_epoch(self) -> None:
        """Snapshot `_last_vision_response` as the current epoch.

        Called by `BrowserScreenshotTool` right after it emits the
        vision bbox text to the brain. Subsequent `browser_click_at(
        vision_index=V_n)` / `browser_type_at` calls resolve `V_n`
        against THIS snapshot, not against the live
        `_last_vision_response` (which a background prefetch may have
        overwritten between screenshot-text-emit and click-dispatch —
        that's the V_n drift bug).

        Also captures the URL so `vision_for_target_resolution` can
        invalidate the epoch when the page implicitly navigates
        (browser_keys(Enter), button-clicks-that-submit-a-form, etc.)
        — `state.current_url` will no longer match `_vision_epoch_url`
        and the epoch falls through to the live response.
        """
        self._vision_epoch_response = self._last_vision_response
        self._vision_epoch_url = self._last_vision_url or self.current_url or ""
        self._vision_epoch_id += 1
        # Phase 1.3: stamp the epoch with wall + turn counter so the
        # freshness gate can reject clicks against an epoch that's older
        # than VISION_MAX_AGE_TURNS mutating actions ago. Reset epoch_turn
        # to current counter — the brain just saw this screenshot, so
        # zero turns elapsed since the snapshot it's reasoning on.
        self._vision_epoch_taken_at = time.time()
        self._vision_epoch_turn = self._brain_turn_counter

    def vision_for_target_resolution(self) -> Any:
        """Return the vision response V-index readers (click_at /
        type_at / fix_text_at / the slider family) should resolve
        against. Prefers the frozen epoch; falls back to the live
        `_last_vision_response` when:
          - no epoch has been captured yet (first turn / mocked test);
          - the epoch's URL no longer matches `current_url` (the page
            implicitly navigated since the brain saw the screenshot —
            F5 fix; otherwise the brain's V_n picked from page A
            resolves against page A's bbox list while the click lands
            on page B, with bboxes that no longer apply).
        """
        if self._vision_epoch_response is not None:
            epoch_url = self._normalize_url(self._vision_epoch_url or "")
            current_url = self._normalize_url(self.current_url or "")
            if epoch_url and current_url and epoch_url != current_url:
                # Page changed. Epoch is stale. Live response is the
                # post-mutation prefetch and matches the current page.
                return self._last_vision_response
            return self._vision_epoch_response
        return self._last_vision_response

    def record_cursor_failure(
        self, *, strategy: str, target: str, reason: str,
    ) -> None:
        """Phase 3.1: log that a cursor-based interaction returned a
        non-success caption. Bounded ledger (last 12 entries) with a
        distinct-strategies set used by the script lockout.
        """
        if not strategy:
            return
        self.cursor_failure_strategies.add(strategy)
        self.cursor_failure_records.append({
            "strategy": strategy,
            "target": target[:120] if target else "",
            "reason": reason[:120] if reason else "",
            "turn": self._brain_turn_counter,
        })
        if len(self.cursor_failure_records) > 12:
            self.cursor_failure_records = self.cursor_failure_records[-12:]

    def cursor_lockout_summary(self) -> str:
        """Render the current cursor-failure ledger for prompt hints."""
        if not self.cursor_failure_records:
            return ""
        last = self.cursor_failure_records[-3:]
        rows = [
            f"  - {r['strategy']}({r['target']!r}): {r['reason']}"
            for r in last
        ]
        return "\n".join(rows)

    async def ensure_vision_synced(self, *, reason: str = "pre_action") -> "str | None":
        """Phase 1.1 hard sync gate. Block until the most recent vision
        prefetch lands. Returns None on success (caller proceeds), or a
        structured error string the caller should return as its tool
        result so the brain re-tries on a fresh state.

        Skipped entirely when VISION_HARD_SYNC=0 — preserves the legacy
        soft-budget behavior for rollback.

        Page-type-aware timeout: if VISION_HARD_SYNC_PAGE_TYPE_OVERRIDES
        is a JSON dict and the last vision response's page_type matches
        a key, that timeout (ms) is used instead of the global default.
        Useful for slow form/search pages where 8s isn't enough.
        """
        if os.environ.get("VISION_HARD_SYNC", "1") in ("0", "false", "no", "False"):
            return None
        # DOM-dirty trigger: if the MutationObserver flagged a silent
        # DOM change since the last /state read, force a fresh prefetch
        # before this gate proceeds. The prefetch above will pick up the
        # current DOM and salt the cache key (force_vision_refresh) so
        # the agent re-runs Gemini rather than serving the stale entry.
        # Local import keeps state.py free of vision_sync circulars.
        if getattr(self, "_dom_dirty_at_last_state", False):
            self._dom_dirty_at_last_state = False
            self._force_vision_refresh = True
            try:
                from .vision_sync import _schedule_vision_prefetch
                refresh_task = _schedule_vision_prefetch(self, self.session_id)
                if refresh_task is not None:
                    self._pending_vision_task = refresh_task
            except Exception as exc:
                print(f"  [dom_dirty refresh skipped: {exc}]")
        task = self._pending_vision_task
        if task is None or task.done():
            return None
        timeout_ms: int | None = None
        try:
            overrides_raw = os.environ.get("VISION_HARD_SYNC_PAGE_TYPE_OVERRIDES")
            if overrides_raw:
                overrides = json.loads(overrides_raw)
                last_resp = self._last_vision_response
                page_type = getattr(last_resp, "page_type", "") if last_resp else ""
                if page_type and page_type in overrides:
                    timeout_ms = int(overrides[page_type])
        except Exception:
            timeout_ms = None
        await _await_vision_required(task, timeout_ms=timeout_ms)
        if not task.done():
            return (
                f"[vision_unavailable:{reason}] Vision prefetch from the "
                f"previous action did not land in time. Re-issue the same "
                f"tool call — the prefetch is still running and will "
                f"complete shortly. Do NOT proceed on stale vision."
            )
        return None

    def init_if_needed(self):
        if self.start_time == 0.0:
            self.start_time = time.time()

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Normalize URL for comparison (strip trailing slash, fragment)."""
        if not url:
            return ""
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(url)
        path = parsed.path.rstrip("/") or "/"
        return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, parsed.query, ""))

    def record_url(self, url: str) -> None:
        """Track a URL visit. Updates current_url and visit counts."""
        if not url:
            return
        norm = self._normalize_url(url)
        self.current_url = url
        self.url_visit_counts[norm] = self.url_visit_counts.get(norm, 0) + 1

    def record_checkpoint(self, url: str, title: str, action: str) -> None:
        """Record a progress checkpoint (successful meaningful step)."""
        self.checkpoints.append({
            "url": url, "title": title, "action": action,
            "time": datetime.now().strftime("%H:%M:%S"),
        })
        if url and url != self.best_checkpoint_url:
            self.best_checkpoint_url = url

    def is_regression(self, url: str) -> bool:
        """Check if navigating to url is going backward."""
        if not url or not self.best_checkpoint_url:
            return False
        norm = self._normalize_url(url)
        best_norm = self._normalize_url(self.best_checkpoint_url)
        # Regression = revisiting an earlier URL when we've been deeper
        if norm == best_norm:
            return False
        return self.url_visit_counts.get(norm, 0) > 0

    def should_allow_screenshot(
        self,
        url: str,
        content_hash: str = "",
    ) -> tuple[bool, str]:
        """Check if a screenshot should be allowed. Returns (allowed, reason).

        Captcha mode no longer blanket-bypasses dedup. Instead:
          - actions_since_screenshot check is relaxed (a captcha round might
            genuinely need multiple vision calls between tool actions)
          - captcha_solve_round is folded into the dedup key so each solve
            attempt gets its own allowance
          - a hard cap (captcha_mode_screenshot_cap) prevents runaway burn
            even if vision keeps failing
        """
        if self.screenshot_budget <= 0:
            return False, "[Screenshot budget exhausted] Use browser_get_markdown or browser_eval instead."
        if self.captcha_mode:
            if self.captcha_screenshots_used >= self.captcha_mode_screenshot_cap:
                return False, (
                    f"[Captcha-mode screenshot cap hit ({self.captcha_mode_screenshot_cap}). "
                    "The vision-based solve isn't converging. Call browser_ask_user for human help, "
                    "or browser_request_help to hand off to a fresh tactic.]"
                )
            norm = self._normalize_url(url)
            key = (norm, f"cap-round-{self.captcha_solve_round}:{content_hash or ''}")
            if norm and key in self.screenshotted_keys:
                return False, (
                    "[Captcha screenshot already taken for this solve round with no change. "
                    "Call browser_solve_captcha or browser_click_at a tile — don't re-screenshot the same state.]"
                )
            return True, ""
        if self.actions_since_screenshot == 0:
            return False, "[No actions since last screenshot — reuse previous. Use browser_get_markdown to re-read content.]"
        norm = self._normalize_url(url)
        # Dedupe on (url, content_hash) — if content changed since last shot
        # at this URL, allow a fresh screenshot.
        key = (norm, content_hash or "")
        if norm and key in self.screenshotted_keys:
            return False, "[Screenshot already exists for this URL + content. Use browser_get_markdown or browser_eval to read page state instead.]"
        return True, ""

    def mark_screenshot_taken(self, url: str, content_hash: str = "") -> None:
        """Record that a screenshot was taken for (url, content_hash).

        In captcha mode the key includes the current solve round so each
        solve attempt gets its own dedup allowance.
        """
        norm = self._normalize_url(url)
        if not norm:
            return
        if self.captcha_mode:
            self.captcha_screenshots_used += 1
            self.screenshotted_keys.add(
                (norm, f"cap-round-{self.captcha_solve_round}:{content_hash or ''}")
            )
        else:
            self.screenshotted_keys.add((norm, content_hash or ""))
        self.last_screenshot_url = norm
        self.last_page_content_hash = content_hash or ""

    @staticmethod
    def hash_page_content(text: str, scroll_y: int | None = None) -> str:
        """Structural fingerprint of a page for screenshot dedup.

        Replaces the old "SHA1 of first 500 chars" scheme, which was both
        insensitive to real changes (below-fold content, lazy loads) and
        over-sensitive to benign re-renders (React class-hash churn).

        Input is the `clickableElementsToString()` payload returned by the
        TS server — one line per interactive element, format roughly:
          `[0]<button aria-label="Sign in">Sign in</button>`

        Fingerprint inputs:
          - count of interactive elements (line count)
          - tag-name histogram (button/input/a/…)
          - top-N aria-label / placeholder / name values, normalized
          - scroll-Y bucketed to 100px when supplied

        All inputs are concatenated into a deterministic canonical string,
        then SHA1-hashed and truncated. Bucketing scroll keeps tiny scroll
        jitters from invalidating dedup while still distinguishing real
        scroll positions.
        """
        if not text:
            return ""
        import hashlib
        import re

        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        count = len(lines)

        # Tag histogram — parse `<tag` at the start of each element snippet.
        tag_counts: dict[str, int] = {}
        attr_pat = re.compile(r'(?:aria-label|placeholder|name)="([^"]+)"')
        attr_samples: list[str] = []
        for ln in lines[:40]:  # cap at 40 to keep bounded
            m = re.search(r"<([a-zA-Z][a-zA-Z0-9]*)", ln)
            if m:
                tag = m.group(1).lower()
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
            # Grab first meaningful attribute to anchor identity.
            a = attr_pat.search(ln)
            if a:
                val = a.group(1).strip().lower()
                # Normalize whitespace + drop digits (React IDs, counters).
                val = re.sub(r"\s+", " ", val)
                val = re.sub(r"\d+", "#", val)
                attr_samples.append(val[:40])

        hist = ",".join(f"{t}:{n}" for t, n in sorted(tag_counts.items()))
        # Use the first 20 normalized attributes — enough to differentiate
        # pages, few enough that a single added menu item doesn't flip the hash.
        attrs = "|".join(attr_samples[:20])
        scroll_bucket = ""
        if scroll_y is not None:
            scroll_bucket = f"s{int(scroll_y) // 100}"

        canonical = f"n={count}|h={hist}|a={attrs}|{scroll_bucket}"
        return hashlib.sha1(canonical.encode("utf-8", errors="ignore")).hexdigest()[:12]

    def record_step(self, tool_name: str, args_summary: str, result_summary: str) -> None:
        """Record a step in the structured step history."""
        self.step_history.append({
            "tool": tool_name,
            "args": args_summary,
            "result": result_summary[:200],
            "url": self.current_url,
            "time": datetime.now().strftime("%H:%M:%S"),
        })

    # --- Inter-action pacing ----------------------------------------------
    #
    # Wallclock pause inserted before every mutating tool dispatches.
    # 1500ms default — long enough for typical filter-reflow / lazy-load
    # delays (200–800ms) plus margin, AND long enough for the awaited
    # vision prefetch (when delta detects real change) to land before
    # the brain reads the next response. Configurable via
    # INTER_ACTION_DELAY_MS env var; set to 0 in CI / tests.
    INTER_ACTION_DELAY_MS_DEFAULT = 1500

    async def inter_action_pause(self) -> None:
        """Sleep INTER_ACTION_DELAY_MS before the current mutating tool
        dispatches. Called from each mutating tool's execute() right
        after capture_action_snapshot."""
        try:
            delay_ms = int(
                os.environ.get(
                    "INTER_ACTION_DELAY_MS",
                    self.INTER_ACTION_DELAY_MS_DEFAULT,
                )
            )
        except ValueError:
            delay_ms = self.INTER_ACTION_DELAY_MS_DEFAULT
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)

    # --- URL-failure ledger ------------------------------------------------
    #
    # Populated when navigate response returns status >= 400. Subsequent
    # navigates to a URL in this dict are refused with [URL_KNOWN_BAD]
    # before any HTTP roundtrip — kills the "404 → re-navigate to same
    # URL" loop observed in the wineaccess.com trace.

    def record_failed_navigation(self, url: str, status_code: int) -> None:
        """Mark a URL as known-bad for the rest of this session."""
        if not url:
            return
        norm = self._normalize_url(url)
        self.failed_navigation_urls[norm] = {
            "status": int(status_code),
            "when": time.time(),
        }

    # --- Action-delta snapshot ----------------------------------------------

    def capture_action_snapshot(
        self,
        target_index: Optional[int] = None,
    ) -> PageStateSnapshot:
        """Build a `PageStateSnapshot` from cached state — no HTTP.

        Called at the top of every mutating tool's ``execute()``. Stores
        the result on ``self.action_snapshot_pre`` so ``build_text_only``
        can compute a delta against the post-action state.

        ``target_index`` is the DOM selector-map index this action is
        about to address (None for tools that don't address by index,
        e.g. browser_navigate). Used to detect "the targeted element
        disappeared" — a strong "click consumed the target" signal.
        """
        # Sample labels from elements_bounds when available — that map
        # is populated by the most recent _fetch_elements_with_bounds /
        # _fetch_elements call. Empty when no bounds have been fetched
        # yet (e.g. first action in a fresh session).
        bounds_map = self.elements_bounds or {}
        sample_labels = tuple(
            (bounds_map[i].get("text") or "").strip()[:40]
            for i in sorted(bounds_map.keys())[:10]
            if isinstance(bounds_map.get(i), dict)
        )
        fp_keys = frozenset(self.element_fingerprints.keys())
        target_fp = ""
        if target_index is not None:
            target_fp = self.element_fingerprints.get(int(target_index), "") or ""
        vision_count = 0
        try:
            resp = self._last_vision_response
            if resp is not None:
                vision_count = len(getattr(resp, "bboxes", []) or [])
        except Exception:
            vision_count = 0
        snap = PageStateSnapshot(
            url=self.current_url or "",
            title="",  # title isn't tracked at the state level — derived from data on the post-side
            elem_count=len(fp_keys),
            dom_hash=self._last_dom_hash or "",
            fingerprint_keys=fp_keys,
            sample_labels=sample_labels,
            vision_bbox_count=vision_count,
            captured_at_turn=self._brain_turn_counter,
            target_index=target_index,
            target_fingerprint=target_fp,
        )
        self.action_snapshot_pre = snap
        # Reset extras for the new action.
        self.action_snapshot_extras = {}
        return snap

    def _capture_post_snapshot(self, data: dict) -> PageStateSnapshot:
        """Build the post-action snapshot from the tool's HTTP response.

        ``data`` is whatever the tool received back from the TS bridge —
        usually a dict with ``url``, ``title``, ``elements`` (one line
        per interactive element), and other tool-specific payload. We
        re-derive elem_count and sample_labels from ``elements`` text
        when present; fall back to the live state attributes otherwise.

        Title comes from ``data['title']`` because ``self`` doesn't
        track it.
        """
        elements_text = ""
        if isinstance(data, dict):
            raw_elements = data.get("elements") or ""
            if isinstance(raw_elements, str):
                elements_text = raw_elements
        # Elem count from the lines of the elements text. Falls back to
        # the live fingerprint map when the response didn't include
        # elements (some tools omit it on no-op).
        if elements_text:
            elem_lines = [ln for ln in elements_text.splitlines() if ln.strip()]
            elem_count = len(elem_lines)
            sample_labels = tuple(
                ln.strip()[:40] for ln in elem_lines[:10]
            )
        else:
            elem_count = len(self.element_fingerprints)
            bounds_map = self.elements_bounds or {}
            sample_labels = tuple(
                (bounds_map[i].get("text") or "").strip()[:40]
                for i in sorted(bounds_map.keys())[:10]
                if isinstance(bounds_map.get(i), dict)
            )
        post_url = (data.get("url") if isinstance(data, dict) else "") or self.current_url or ""
        post_title = (data.get("title") if isinstance(data, dict) else "") or ""
        # DOM hash recomputed from elements text — gives us a fresh
        # structural fingerprint that's directly comparable to the pre
        # snapshot (which read self._last_dom_hash, also derived this
        # way by prior actions).
        try:
            scroll_y = None
            if isinstance(data, dict):
                si = data.get("scrollInfo") or {}
                if isinstance(si, dict):
                    scroll_y = si.get("scrollY")
            new_hash = self.hash_page_content(elements_text, scroll_y) if elements_text else (self._last_dom_hash or "")
        except Exception:
            new_hash = self._last_dom_hash or ""
        vision_count = 0
        try:
            resp = self._last_vision_response
            if resp is not None:
                vision_count = len(getattr(resp, "bboxes", []) or [])
        except Exception:
            vision_count = 0
        return PageStateSnapshot(
            url=post_url,
            title=post_title,
            elem_count=elem_count,
            dom_hash=new_hash,
            fingerprint_keys=frozenset(self.element_fingerprints.keys()),
            sample_labels=sample_labels,
            vision_bbox_count=vision_count,
            captured_at_turn=self._brain_turn_counter,
        )

    def compute_action_delta(
        self,
        pre: PageStateSnapshot,
        post: PageStateSnapshot,
    ) -> dict:
        """Return a structured diff between pre/post page snapshots.

        Pure function — no I/O, no state mutation. Caller is expected
        to render this via ``render_action_delta`` and inject the
        result into the tool's response text.
        """
        added_indices = sorted(post.fingerprint_keys - pre.fingerprint_keys)[:8]
        removed_indices = sorted(pre.fingerprint_keys - post.fingerprint_keys)[:8]
        elem_delta = post.elem_count - pre.elem_count
        url_changed = bool(pre.url) and bool(post.url) and (pre.url != post.url)
        title_changed = (
            bool(pre.title or post.title)
            and pre.title != post.title
        )
        dom_changed = bool(pre.dom_hash) and bool(post.dom_hash) and (pre.dom_hash != post.dom_hash)
        vision_delta = post.vision_bbox_count - pre.vision_bbox_count
        target_disappeared = False
        target_changed = False
        if pre.target_index is not None:
            target_disappeared = pre.target_index not in post.fingerprint_keys
            if not target_disappeared and pre.target_fingerprint:
                new_fp = self.element_fingerprints.get(pre.target_index, "") or ""
                target_changed = bool(new_fp) and (new_fp != pre.target_fingerprint)
        # Sample labels of newly-appeared elements — read from the post
        # snapshot's sample_labels filtered to indices in added_indices
        # is too brittle (sample_labels is positional). Just take the
        # first 5 of post.sample_labels as a representative sample.
        added_label_samples = []
        if added_indices and post.sample_labels:
            added_label_samples = list(post.sample_labels[:5])
        return {
            "url_changed": url_changed,
            "url_from": pre.url,
            "url_to": post.url,
            "title_changed": title_changed,
            "title_from": pre.title,
            "title_to": post.title,
            "elem_delta": elem_delta,
            "added_indices": added_indices,
            "removed_indices": removed_indices,
            "added_label_samples": added_label_samples,
            "dom_changed": dom_changed,
            "vision_delta": vision_delta,
            "target_index": pre.target_index,
            "target_disappeared": target_disappeared,
            "target_changed": target_changed,
        }

    @staticmethod
    def _interpret_delta(tool_name: str, delta: dict, extras: dict) -> str:
        """One-line interpretation hint. Returns "" when the situation
        is ambiguous — better silent than misleading.
        """
        # Type-specific facts come from extras (set by the type tools).
        if tool_name in ("browser_type", "browser_type_at", "browser_fix_text_at"):
            if extras.get("changed") is False:
                return (
                    "field already contained that value; no change made — "
                    "do NOT retype, advance to the next constraint"
                )
            if extras.get("after") is not None:
                before = (extras.get("before") or "")[:30]
                after = (extras.get("after") or "")[:30]
                return f"field updated to {after!r} (was {before!r})"
            # Fall through to structural delta if no extras.
        if delta["url_changed"]:
            return (
                "page navigated — V_n indices and DOM [N] indices from "
                "the previous page no longer apply; next mutation should "
                "be preceded by browser_screenshot"
            )
        if delta["target_disappeared"]:
            return (
                f"the targeted element [{delta['target_index']}] is gone — "
                "click consumed it (modal opened, item removed, or "
                "section toggled). Re-read the elements list."
            )
        if delta["elem_delta"] >= 3 and not delta["url_changed"]:
            return (
                "click expanded a section / opened a dropdown — new "
                "interactive elements are now available; look for the "
                "next step's target among them"
            )
        if delta["elem_delta"] <= -3 and not delta["url_changed"]:
            return (
                "click collapsed a section / closed an overlay — fewer "
                "elements visible now"
            )
        if delta["dom_changed"] and delta["elem_delta"] == 0 and not delta["url_changed"]:
            return (
                "minor reflow — button toggled, value updated, or "
                "content swapped without changing element count"
            )
        # Nothing meaningful happened.
        if (
            not delta["url_changed"]
            and not delta["title_changed"]
            and not delta["dom_changed"]
            and delta["elem_delta"] == 0
            and not delta["target_disappeared"]
            and not delta["target_changed"]
        ):
            return (
                "no-op — your action did NOT change the page. The target "
                "may not be interactive, the click missed, or the field "
                "rejected your input. Re-screenshot before retrying."
            )
        return ""

    def render_action_delta(
        self,
        tool_name: str,
        target_summary: str,
        delta: dict,
        extras: Optional[dict] = None,
    ) -> str:
        """Format the [ACTION_DELTA] block for injection into a tool reply.

        Returns "" when the delta is empty AND the interpretation hint
        is empty — keeps no-op tool replies unchanged.
        """
        extras = extras or {}
        hint = self._interpret_delta(tool_name, delta, extras)
        bullets: list[str] = []
        if delta["elem_delta"] != 0:
            samples = delta.get("added_label_samples") or []
            sample_str = ""
            if samples and delta["elem_delta"] > 0:
                sample_str = " (samples: " + ", ".join(repr(s) for s in samples[:4]) + ")"
            sign = "+" if delta["elem_delta"] > 0 else ""
            bullets.append(
                f"  · elements: {sign}{delta['elem_delta']}{sample_str}"
            )
        if delta["url_changed"]:
            bullets.append(
                f"  · URL: {delta['url_from'][:60]} → {delta['url_to'][:60]}"
            )
        if delta["title_changed"]:
            bullets.append(
                f"  · title: {delta['title_from'][:50]!r} → {delta['title_to'][:50]!r}"
            )
        if delta["target_disappeared"]:
            bullets.append(
                f"  · target [{delta['target_index']}] disappeared from "
                f"selector map"
            )
        elif delta["target_changed"]:
            bullets.append(
                f"  · target [{delta['target_index']}] still present "
                f"(text or attrs changed)"
            )
        # Vision delta is a weaker signal — only mention when it's
        # substantial (>= 4) and url is unchanged (otherwise it's just
        # the natural reset on page change).
        if (
            not delta["url_changed"]
            and abs(delta["vision_delta"]) >= 4
        ):
            sign = "+" if delta["vision_delta"] > 0 else ""
            bullets.append(
                f"  · vision bboxes: {sign}{delta['vision_delta']}"
            )
        if not bullets and not hint:
            return ""
        header = (
            f"[ACTION_DELTA] last={tool_name}({target_summary[:40]})"
            if target_summary
            else f"[ACTION_DELTA] last={tool_name}"
        )
        parts = [header]
        parts.extend(bullets)
        if hint:
            parts.append(f"  → {hint}")
        return "\n".join(parts)

    # Tools that don't mutate page state — no [ACTION_DELTA] for these.
    _ACTION_DELTA_SKIP_TOOLS: frozenset = frozenset({
        "browser_screenshot",
        "browser_get_markdown",
        "browser_brief_mark",
        "browser_plan_next_steps",
        "browser_list_slider_handles",
        "browser_get_accessibility_tree",
        "browser_dom_search",
    })

    # --- Cross-index repeat-type guard ------------------------------------
    # Window in seconds during which two identical typed values count as a
    # "repeat". 30s comfortably covers the brain's normal think-act-think
    # cycle (~5-12s/turn) so we catch real cascades without false positives.
    REPEAT_TYPE_WINDOW_S = 30.0
    # Max distinct typing attempts of the same value before we refuse the
    # next one. 3 lets the brain retry once after a real failure (e.g. the
    # field was unfocused) but stops the price-40-six-times cascade.
    REPEAT_TYPE_REFUSE_AT = 3

    def check_repeat_type(self, text: str) -> str | None:
        """Cross-index variant of the dead-type guard. Returns a
        structured refusal string when the brain has typed ``text``
        ``REPEAT_TYPE_REFUSE_AT`` or more times in the last
        ``REPEAT_TYPE_WINDOW_S`` seconds, or None to allow the type.

        Stripping is normalized — surrounding whitespace and trailing
        currency-style punctuation don't disqualify a repeat.
        """
        norm = (text or "").strip().rstrip("$.,").lower()
        if not norm:
            return None
        now = time.time()
        # Garbage-collect old entries before checking.
        cutoff = now - self.REPEAT_TYPE_WINDOW_S
        self.recent_typed_values = [
            (t, ts) for (t, ts) in self.recent_typed_values if ts >= cutoff
        ]
        # Cap ledger size to avoid unbounded growth on long sessions.
        if len(self.recent_typed_values) > 50:
            self.recent_typed_values = self.recent_typed_values[-25:]
        same = sum(1 for (t, _) in self.recent_typed_values if t == norm)
        if same >= self.REPEAT_TYPE_REFUSE_AT - 1:
            # We're about to issue the 3rd identical type — refuse.
            return (
                f"[REPEAT_TYPE_REJECTED] You have already typed {text!r} "
                f"into {same + 1} different fields in the last "
                f"{int(self.REPEAT_TYPE_WINDOW_S)}s without verifying it "
                f"landed. Stop and call browser_screenshot now — read the "
                f"new V_n bbox list to see what's actually in each input. "
                f"If a field already shows the correct value, do NOT retype "
                f"it; advance to the next constraint. If the prior types "
                f"genuinely missed the target, narrate WHICH field you "
                f"intend to hit before the next type call (e.g., 'V4 is "
                f"the max-price input, V3 is the min-price input — "
                f"targeting V4')."
            )
        return None

    def record_typed_value(self, text: str) -> None:
        """Append to the cross-index type ledger after a successful type."""
        norm = (text or "").strip().rstrip("$.,").lower()
        if norm:
            self.recent_typed_values.append((norm, time.time()))

    def check_dead_click(self, click_target: str) -> str | None:
        """Pre-flight check before dispatching a click.

        Counts how many times this exact click target has been fired in
        a row without the page DOM changing in between. Once the count
        would reach `MAX_CONSECUTIVE_SAME_TARGET`, refuse with a
        structured error so the brain is forced to pick a different
        target. Different target OR a DOM change resets the count.

        DOM change is detected via `_last_dom_hash` (set by every state
        fetch).

        Returns a structured error string when blocking, or None when
        the click is allowed to proceed.
        """
        same_target = (click_target == self.last_click_target)
        same_dom = (
            bool(self._last_dom_hash)
            and self._last_dom_hash == self.last_click_dom_hash
        )
        if same_target and same_dom:
            # Nth consecutive dead attempt at the same target.
            self.consecutive_dead_clicks += 1
        else:
            # Fresh attempt at this target (different target OR page moved).
            self.consecutive_dead_clicks = 1
        if self.consecutive_dead_clicks >= self.MAX_CONSECUTIVE_SAME_TARGET:
            # Reset so the brain picking a new target next round clears
            # the strike count cleanly.
            self.consecutive_dead_clicks = 0
            self.last_click_target = ""
            return (
                f"[dead_click_blocked] {click_target} has been clicked "
                f"{self.MAX_CONSECUTIVE_SAME_TARGET} times in a row with "
                "no DOM change. The previous clicks did not move the "
                "page. Switch tactic: call browser_screenshot to "
                "re-observe, then pick a different V_n, try a "
                "different role (e.g., the form's submit button instead "
                "of the input), or browser_wait_for content you expect "
                "to appear. Do NOT retry this exact target, and do NOT "
                "synthesize clicks via browser_run_script — JS clicks "
                "are isTrusted=false and bot-detected."
            )
        return None

    def register_click_attempt(self, click_target: str) -> None:
        """Stamp the current click target + DOM hash so the next call to
        `check_dead_click` can compare against them."""
        self.last_click_target = click_target
        self.last_click_dom_hash = self._last_dom_hash

    def advance_observation_token(self, source: str = "") -> None:
        """No-op shim retained so kept tools (click_selector,
        rewind_to_checkpoint, scroll_until, drag_slider_until) that
        were ported forward from the validator era can still call it
        without blowing up. The token machinery was part of the
        deleted validator subsystem; in the reverted architecture the
        in-tool freshness/blocker/confidence gates in click_at do
        the same job."""
        pass

    def get_last_checkpoint(self) -> dict | None:
        """Return the most recent checkpoint."""
        return self.checkpoints[-1] if self.checkpoints else None

    def export_step_history(self) -> str:
        """Export structured step history and checkpoint to disk.

        Writes TWO formats:
          - step_history.md  — human-readable markdown log
          - step_history.json — structured data the orchestrator parses
            to build domain-keyed captcha learnings and to inject prior
            context into subsequent tasks.
        """
        lines = ["## Step History"]
        for i, step in enumerate(self.step_history, 1):
            lines.append(f"{i}. [{step['time']}] {step['tool']}({step['args']}) → {step['result']}")
            if step.get("url"):
                lines.append(f"   URL: {step['url']}")

        if self.checkpoints:
            lines.append("\n## Checkpoints (progress markers)")
            for cp in self.checkpoints:
                lines.append(f"- [{cp['time']}] {cp['action']} → {cp['url']}")

        lines.append(f"\n## Best checkpoint URL: {self.best_checkpoint_url or 'none'}")
        lines.append(f"## Regressions detected: {self.regression_count}")

        content = "\n".join(lines)

        # Write to task-specific directory
        task_dir = f"/tmp/superbrowser/{self.task_id}" if self.task_id else "/tmp/superbrowser"
        os.makedirs(task_dir, exist_ok=True)
        step_path = os.path.join(task_dir, "step_history.md")
        with open(step_path, "w") as f:
            f.write(content)
        print(f"  [step history saved: {step_path}]")

        # Structured JSON export for orchestrator consumption.
        import json as _json_export
        structured = {
            "task_id": self.task_id,
            "sessions_opened": self.sessions_opened,
            "current_url": self.current_url,
            "best_checkpoint_url": self.best_checkpoint_url,
            "regression_count": self.regression_count,
            "checkpoints": self.checkpoints,
            "vision_calls": self.vision_calls,
            "text_calls": self.text_calls,
            "max_screenshots": self.max_screenshots,
            "screenshots_used": self.max_screenshots - self.screenshot_budget,
            "steps": self.step_history,
            "activity_log": self.activity_log,
        }
        json_path = os.path.join(task_dir, "step_history.json")
        try:
            with open(json_path, "w") as f:
                _json_export.dump(structured, f, indent=2, default=str)
            print(f"  [structured history saved: {json_path}]")
        except Exception as exc:  # pragma: no cover - best-effort persistence
            print(f"  [structured history save failed: {exc}]")

        # Save checkpoint as JSON for re-delegation
        if self.best_checkpoint_url:
            import json as _json
            checkpoint_data = {
                "url": self.best_checkpoint_url,
                "title": self.checkpoints[-1].get("title", "") if self.checkpoints else "",
                "regressions": self.regression_count,
            }
            cp_path = os.path.join(task_dir, "checkpoint.json")
            with open(cp_path, "w") as f:
                _json.dump(checkpoint_data, f)
            print(f"  [checkpoint saved: {cp_path}]")

        return content

    def log_activity(self, action: str, result: str = "ok"):
        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {action}"
        if result != "ok":
            entry += f" → {result}"
        self.activity_log.append(entry)
        if len(self.activity_log) > 30:
            self.activity_log.pop(0)

    def get_activity_summary(self) -> str:
        if not self.activity_log:
            return ""
        lines = "\n".join(self.activity_log[-15:])
        return (
            f"\n--- Previous activity (DO NOT repeat failed approaches) ---\n"
            f"{lines}\n"
            f"--- Screenshots remaining: {self.screenshot_budget}/{self.max_screenshots} | Sessions opened: {self.sessions_opened} ---"
        )

    def print_summary(self):
        elapsed = time.time() - self.start_time if self.start_time else 0
        used = self.max_screenshots - self.screenshot_budget
        print(f"\n  [Session Summary]")
        print(f"  Duration: {elapsed:.1f}s | Sessions: {self.sessions_opened}")
        print(f"  Vision calls: {self.vision_calls} | Text calls: {self.text_calls} | Screenshots: {used}/{self.max_screenshots}")
        est = self.vision_calls * 0.03 + self.text_calls * 0.002
        print(f"  Estimated cost: ~${est:.3f}")

    def export_activity_log(self) -> str:
        """Export structured activity log to disk for the orchestrator to read."""
        elapsed = time.time() - self.start_time if self.start_time else 0
        used = self.max_screenshots - self.screenshot_budget

        lines = [
            f"## Browser Worker Activity",
            f"Duration: {elapsed:.1f}s | Screenshots: {used}/{self.max_screenshots} | Tool calls: {self.vision_calls + self.text_calls}",
            "",
            "### Actions",
        ]
        lines.extend(self.activity_log)
        content = "\n".join(lines)

        # Write to disk so orchestrator/subagent can read it
        activity_path = "/tmp/superbrowser/last_activity.md"
        os.makedirs(os.path.dirname(activity_path), exist_ok=True)
        with open(activity_path, "w") as f:
            f.write(content)
        print(f"  [activity log saved: {activity_path}]")
        return content

    def save_screenshot(self, b64: str, label: str = "") -> str:
        self.step_counter += 1
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
        fn = f"{self.step_counter:03d}-{label}.jpg" if label else f"{self.step_counter:03d}.jpg"
        path = os.path.join(SCREENSHOT_DIR, fn)
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))
        print(f"  [screenshot saved: {path}]")
        return path

    async def build_tool_result_blocks(
        self,
        b64: str,
        caption: str,
        *,
        intent: str | None = None,
        url: str | None = None,
        elements: str | None = None,
        elements_with_bounds: list[dict] | None = None,
        device_pixel_ratio: float = 1.0,
    ) -> list[dict] | str:
        """Async dispatch between the vision-preprocessor path and the
        legacy image-blocks path.

        When `VISION_ENABLED=1` the screenshot is sent to the dedicated
        vision agent (cheap model) and the brain only sees its textual
        summary + bboxes + flags. Otherwise we fall through to the
        legacy `build_image_blocks` that embeds the JPEG directly.

        `intent` and `url` are optional hints used solely by the vision
        path. `elements` is the DOM element listing — hashed as a cache
        key so a re-screenshot on the same URL with identical DOM hits
        the cache.
        """
        # Lazy import: keeps the vision package optional at import time
        # so a broken VISION_API_KEY doesn't blow up sessions that never
        # enable the feature.
        try:
            from vision_agent import (
                dom_hash_of,
                get_vision_agent,
                vision_agent_enabled,
            )
            try:
                from vision_agent import dom_text_hash_of
            except ImportError:
                dom_text_hash_of = None  # type: ignore[assignment]
        except ImportError:
            vision_agent_enabled = lambda: False  # type: ignore[assignment]
            get_vision_agent = None  # type: ignore[assignment]
            dom_hash_of = None  # type: ignore[assignment]
            dom_text_hash_of = None  # type: ignore[assignment]

        if vision_agent_enabled() and get_vision_agent is not None:
            dh = dom_hash_of(elements) if dom_hash_of else ""
            if dh:
                self._last_dom_hash = dh
            # Phase 1.2: viewport-aware secondary key — left empty here
            # because build_tool_result_blocks doesn't receive scroll
            # info. The prefetch path in _schedule_vision_prefetch
            # populates it from the live /state response. Empty string
            # falls through to legacy 5-tuple-equivalent caching, which
            # is correct (just less granular than the prefetch path).
            dth = ""
            if intent:
                self._last_intent = intent
            effective_intent = intent or self._last_intent or "observe page"
            effective_url = url or self.current_url
            try:
                agent = get_vision_agent()
                # Read screenshot dims off the bytes once — Gemini emits
                # box_2d in [0, 1000] space; downstream click dispatch
                # needs the source pixel dims to convert back to viewport
                # coordinates accurately.
                img_w, img_h = _read_image_dims(b64)
                resp = await agent.analyze(
                    screenshot_b64=b64,
                    intent=effective_intent,
                    session_id=self.session_id,
                    url=effective_url,
                    dom_hash=dh or self._last_dom_hash,
                    dom_text_hash=dth,
                    previous_summary=self._last_vision_summary or None,
                    image_width=img_w,
                    image_height=img_h,
                    task_instruction=self.task_instruction or None,
                )
                self._last_vision_summary = resp.summary
                self._last_vision_response = resp
                self._last_vision_ts = time.time()
                self._last_vision_url = effective_url or self.current_url or ""
                self.vision_calls += 1
                self.actions_since_screenshot = 0
                # Freeze this response as the current epoch. The brain
                # is about to see `as_brain_text()` output — subsequent
                # V_n references MUST resolve to this snapshot, not to
                # whatever background prefetch writes into
                # `_last_vision_response` before the brain's next turn.
                self.freeze_vision_epoch()
                label = (caption or "").split("\n")[0][:30].replace(" ", "-")
                # Still save the raw screenshot locally for debugging —
                # doesn't leave the box, doesn't reach the brain.
                self.save_screenshot(b64, label)
                # Fire-and-forget push of detected bboxes to live viewers.
                # Lets the user see what Gemini "saw" (full set, color-
                # coded by role) for ~1.5s before the next click. Failure
                # is non-fatal — vision still works, just no overlay.
                try:
                    asyncio.create_task(_push_vision_bboxes(
                        self.session_id, resp,
                        url=self._last_vision_url,
                    ))
                except Exception as exc:
                    print(f"  [vision-overlay push failed: {exc}]")

                # Hierarchical planner pass — DOM-side blocker scan +
                # action sequencing. Only runs for t3 sessions (t1
                # Puppeteer sessions would need a TS-side blocker
                # endpoint; deferred). Soft-fails: any exception here
                # falls back to the vision-only caption.
                plan_text = ""
                if self.session_id.startswith("t3-") and \
                        os.environ.get("ACTION_PLANNER_AUTO", "1") != "0":
                    try:
                        from superbrowser_bridge.antibot import interactive_session as _t3mgr
                        from superbrowser_bridge.antibot.ui_blockers import detect as _detect_blockers
                        from superbrowser_bridge.action_planner import plan as _plan_actions
                        mgr = _t3mgr.default()
                        blockers = await _detect_blockers(mgr, self.session_id)
                        self._last_blockers = blockers
                        queue = _plan_actions(
                            vresp=resp,
                            blockers=blockers,
                            task_instruction=self.task_instruction or "",
                            url=effective_url or "",
                            recent_steps=self.step_history[-8:] if self.step_history else [],
                        )
                        self._last_action_queue = queue
                        plan_text = queue.to_brain_text()
                    except Exception as exc:
                        print(f"  [action-planner: skipped — {exc}]")

                brain_text = resp.as_brain_text()
                if plan_text:
                    brain_text = f"{brain_text}\n\n{plan_text}"
                text = f"{caption}\n\n{brain_text}" if caption else brain_text
                return [{"type": "text", "text": text}]
            except Exception as exc:
                # Never let a vision-layer failure break a tool result —
                # fall through to the legacy image path.
                print(f"  [vision-agent: falling back to image blocks — {exc}]")

        return self.build_image_blocks(
            b64,
            caption,
            elements_with_bounds=elements_with_bounds,
            device_pixel_ratio=device_pixel_ratio,
        )

    def build_image_blocks(
        self,
        b64: str,
        caption: str,
        elements_with_bounds: list[dict] | None = None,
        device_pixel_ratio: float = 1.0,
    ) -> list[dict]:
        """Build a vision-message-ready payload (text + image).

        If `elements_with_bounds` is provided, paint dashed bbox overlays +
        index labels on the screenshot so the LLM can ground on [index]
        instead of guessing pixel coordinates. Silently falls back to the
        raw screenshot if PIL is unavailable or overlay fails.
        """
        self.vision_calls += 1
        self.actions_since_screenshot = 0
        label = caption.split("\n")[0][:30].replace(" ", "-").replace("/", "_")

        final_b64 = b64
        if elements_with_bounds:
            try:
                from superbrowser_bridge.highlights import build_highlighted_screenshot
                final_b64 = build_highlighted_screenshot(
                    b64, elements_with_bounds, device_pixel_ratio,
                )
            except Exception:
                final_b64 = b64

        # Clamp the final payload to ≤ 2MB / ≤1568px side. Runs AFTER the
        # highlight overlay pass so overlay bloat can't tip a 1.8MB raw
        # screenshot over Gemini's 1.5MB-ish reject threshold.
        try:
            from superbrowser_bridge.image_safety import sanitize_image_b64
            final_b64 = sanitize_image_b64(final_b64)
        except Exception as e:
            print(f"  [image-safety: sanitize failed, sending raw: {e}]")

        self.save_screenshot(final_b64, label)
        return [
            {"type": "text", "text": caption},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{final_b64}"}},
        ]

    def build_text_only(self, data: dict, prefix: str = "") -> str:
        self.action_count += 1
        self.text_calls += 1
        self.actions_since_screenshot += 1
        # Phase 1.2: pick up implicit navigations. The TS bridge reports
        # the live URL on every action response; if the page navigated
        # without us calling browser_navigate (form submit, JS redirect,
        # history.pushState), record it here so the freshness logic can
        # invalidate the vision epoch.
        actual_url = data.get("url") or ""
        if actual_url and actual_url != self.current_url:
            self.record_url(actual_url)
        parts = [prefix]
        if data.get("url"):
            parts.append(f"Page: {data['url']}")
        if data.get("title"):
            parts.append(f"Title: {data['title']}")
        result = " | ".join(p for p in parts if p)
        # Auto-include interactive elements so agent knows what's on page
        # (BrowserOS pattern: every action returns updated element snapshot)
        if data.get("elements"):
            result += f"\n\nInteractive elements:\n{data['elements']}"
        if data.get("consoleErrors"):
            result += f"\nConsole errors: {data['consoleErrors']}"
        if data.get("pendingDialogs"):
            result += f"\nPending dialogs: {data['pendingDialogs']}"
        # --- [ACTION_DELTA] block --------------------------------------
        # Tells the brain what changed because of its last action.
        # Without this signal the brain has to re-derive page state
        # from scratch every iteration; with it, multi-step UI flows
        # (nested filter trees, sliders, dropdowns) become tractable.
        # Only emits when capture_action_snapshot was called at the
        # top of the current tool's execute() — read-only tools and
        # legacy tools that don't capture get nothing.
        delta_block = self._emit_action_delta(data)
        if delta_block:
            result += f"\n\n{delta_block}"
        # Piggyback cached vision if it's still fresh — gives the brain
        # up-to-date bboxes after a mutating tool WITHOUT a screenshot
        # round trip + Gemini call. "Fresh" = same URL as the action's
        # response AND less than FRESH_VISION_SECONDS old. The brain can
        # then call browser_click_at(vision_index=V_n) immediately on the
        # next turn, skipping a 2-5s vision pass.
        cached = self._fresh_vision_text(data.get("url", ""))
        if cached:
            result += f"\n\n{cached}"
        if self.action_count >= 5:
            result += (
                "\n\n[HINT: Keep using browser_click_at(vision_index=V_n) / "
                "browser_type_at for every interaction — each fires a "
                "real CDP mouse event with humanized motion, which "
                "avoids bot-detection. Do NOT batch steps into "
                "browser_run_script; JS clicks are isTrusted=false and "
                "frequently rejected by WAF-protected sites.]"
            )
        return result

    def _emit_action_delta(self, data: dict) -> str:
        """Compute + render [ACTION_DELTA] for the current build_text_only
        call. Returns empty string when no pre-snapshot was captured,
        when the tool is in the read-only skip list, or when the diff
        is uninteresting and the interpretation hint is empty.

        Always clears ``action_snapshot_pre`` after running so a stale
        snapshot can't leak into a subsequent tool's response.
        """
        pre = self.action_snapshot_pre
        if pre is None:
            return ""
        # Staleness guard. If the snapshot was captured more than 1
        # brain turn ago, the calling tool didn't capture and we're
        # picking up a leak from a prior refused tool. Drop it.
        if pre.captured_at_turn < (self._brain_turn_counter - 1):
            self.action_snapshot_pre = None
            self.action_snapshot_extras = {}
            return ""
        # Identify the current tool from step_history. Most tools call
        # record_step before build_text_only; this gives us the name +
        # args summary without threading a tool_name parameter through
        # every call site.
        last_step = self.step_history[-1] if self.step_history else None
        tool_name = ""
        target_summary = ""
        if isinstance(last_step, dict):
            tool_name = last_step.get("tool") or ""
            target_summary = (last_step.get("args") or "")[:60]
        if tool_name in self._ACTION_DELTA_SKIP_TOOLS:
            self.action_snapshot_pre = None
            self.action_snapshot_extras = {}
            return ""
        try:
            post = self._capture_post_snapshot(data)
            delta = self.compute_action_delta(pre, post)
            extras = self.action_snapshot_extras or {}
            block = self.render_action_delta(
                tool_name or "(unknown tool)",
                target_summary,
                delta,
                extras=extras,
            )
            # Stash the delta + tool name on state so the worker hook
            # can read "did this iteration actually change the page?"
            # without re-parsing the rendered block. Used to reset the
            # brief stagnation counter when real progress happens that
            # didn't (yet) flip a brief predicate.
            self.last_action_delta = {
                "tool": tool_name,
                "delta": delta,
                "extras": extras,
                "captured_at_turn": self._brain_turn_counter,
            }
        except Exception as exc:
            print(f"[action_delta_error] {exc}")
            block = ""
            self.last_action_delta = None
        finally:
            self.action_snapshot_pre = None
            self.action_snapshot_extras = {}
        return block

    # How old a cached vision response can be before we stop piggybacking
    # it onto mutating-tool replies. Short enough that the brain doesn't
    # click stale bboxes; long enough to cover a rapid click-scroll-click
    # sequence where no fresh vision has landed yet.
    FRESH_VISION_SECONDS = 10.0

    def _fresh_vision_text(self, tool_url: str) -> str:
        """Return cached vision's brain_text when safe to attach, else "".

        Safe means: we have a cached VisionResponse, its URL matches the
        URL this tool response is reporting (so the brain doesn't mistake
        pre-navigation bboxes for post-navigation state), and it's young
        enough (FRESH_VISION_SECONDS) to still reflect the page.

        Rendering is deliberately cheap — `as_brain_text()` is pure
        Python string formatting, no I/O.
        """
        resp = self._last_vision_response
        if resp is None:
            return ""
        if (time.time() - self._last_vision_ts) > self.FRESH_VISION_SECONDS:
            return ""
        # URL match — normalize to just scheme+host+path (ignore query
        # churn that doesn't meaningfully change the page).
        def _strip_query(u: str) -> str:
            if not u:
                return ""
            return u.split("?", 1)[0].split("#", 1)[0]
        if tool_url and _strip_query(tool_url) != _strip_query(self._last_vision_url):
            return ""
        try:
            return "[CACHED VISION — bboxes still valid; use vision_index=V_n to click]\n" + resp.as_brain_text()
        except Exception:
            return ""

