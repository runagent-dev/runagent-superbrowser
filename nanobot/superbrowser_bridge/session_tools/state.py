"""Per-instance browser session state.

Encapsulates ~70 fields used by the tool family — budget, vision cache,
form session, cursor-failure ledger, dead-click guard, etc. Each Nanobot
instance that registers browser tools gets its own state, so multi-agent
setups (orchestrator + browser worker in the same process) don't share
globals.

The class is intentionally NOT split across files: the methods coordinate
many fields together (epoch / vision-resolution / dead-click), and the
encapsulation is the point.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from datetime import datetime
from typing import Any, Optional

from .effects import BLOCKED_BROWSER_OPEN_HARD_STOP  # re-imported for state.py module surface
from .formatting import _fetch_elements, _format_state
from .http_client import SCREENSHOT_DIR
from .telemetry import _compute_screenshot_budget
from .vision_pipeline import (
    _append_fresh_vision,
    _await_vision_required,
    _push_vision_bboxes,
    _read_image_dims,
    _schedule_vision_prefetch,
)


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
        # Wall-clock seconds of the most recent successful screenshot.
        # Used by the autocomplete-pending click guard to refuse stale
        # V_n / selector clicks that fire before the brain re-screenshots
        # to see the dropdown. 0.0 means "never screenshotted yet".
        self.last_screenshot_at: float = 0.0
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
        # v4 — toggle-aware dead-click exemption + just_toggled
        # detection. When the brain clicks a filter chip / checkbox to
        # un-apply it (legitimate undo), the same V_n fires twice in a
        # row. Without this, the dead-click guard would block the
        # un-toggle. We record the bbox's `is_active` state at click
        # dispatch time; if the next click on the same target sees a
        # FLIPPED is_active, that's a successful toggle, not flailing,
        # and the consecutive counter resets.
        # `last_click_target_label` + `last_click_target_box_2d` give
        # the post-click vision pipeline enough info to find the same
        # bbox in the new response and stamp `just_toggled` on it.
        self.last_click_target_active_state: bool | None = None
        self.last_click_target_label: str = ""
        self.last_click_target_box_2d: list[int] | None = None
        # Cross-tool element identity. Resolved DOM index of the last
        # click target — populated by every click path that knows the
        # index (BrowserClickTool from `index` arg; BrowserClickAtTool
        # from `data.snap.dom_index` post-response). When set, the
        # dead-click guard catches a follow-up `browser_click(N)` on
        # the SAME element after a `browser_click_at(V_n)` toggled it,
        # by comparing this against the new tool's `dom_index`. Resets
        # to None on session reset and on any explicit re-screenshot
        # (which means the brain has re-observed and a deliberate
        # toggle-off is permitted).
        self.last_click_dom_index: int | None = None
        # --- Surgical undo ring (Problem 1) -------------------------------
        # Bounded LIFO ring of recent reversible clicks. Each entry is a
        # dict (see begin_click_record / finalize_click_record for the
        # shape). Filled in two phases — `begin_click_record` is called
        # before HTTP dispatch with what we know up-front (target, label,
        # pre_active, reversibility class); `finalize_click_record` folds
        # in the click response (effect, snap, post_url) and pushes onto
        # the ring. The misclick auto-detector in vision_pipeline reads
        # the top entry to mark misclick_flag + misclick_evidence after
        # the post-click vision lands.
        self._undo_ring: list[dict] = []
        self.UNDO_RING_MAX = 4
        # The pending entry being assembled across the click → vision
        # boundary. Set by begin_click_record, consumed by
        # finalize_click_record. Should be None outside a click flight.
        self._pending_undo_entry: dict | None = None
        # One-shot advisory queue. The misclick detector appends
        # human-readable strings here; the worker hook drains them into
        # the next tool result (cap 2/turn) and clears the list. Plain
        # FIFO list — the cap is enforced on render, not on insert.
        self._misclick_advisory: list[str] = []
        # Snapshot of the previous vision response's is_active map
        # keyed by (label, box_centroid_bucket) so the misclick detector
        # can identify which bbox flipped. Repopulated once per prefetch.
        self._prev_active_map: dict[tuple[str, int, int], bool] = {}
        # v4 D3 — failure ledger for browser_run_script. Tracks
        # (success: bool) for the last 5 calls. Worker_hook fires a
        # [RUN_SCRIPT_FAILING] redirect when 3+ of the last 5 failed.
        self.recent_run_script_outcomes: list[bool] = []
        # v5 — visual-stability gate. Set by navigation tools after a
        # successful nav; consumed by the next vision prefetch (which
        # passes `?settle=true` to /state, triggering the TS-side
        # waitForVisualStable that catches font swap + image load +
        # layout-shift before the screenshot). Without this, the
        # FIRST vision pass after a cold navigate captures bboxes
        # against pre-settled positions, producing the bbox-above-text
        # offset bug.
        self._needs_visual_settle: bool = False
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
        # Per-session asyncio.Lock that serializes cursor-driven slider
        # operations (set_slider_at, drag_slider_until). The CDP /
        # patchright mouse cursor is session-scoped — concurrent drags
        # clobber each other. Lazily created by the slider tools the
        # first time they run, but pre-declared here so attribute
        # access doesn't AttributeError before the first call.
        self.slider_drag_lock: Optional[asyncio.Lock] = None
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
        # Set True when the post-type autocomplete scan found ≥1
        # suggestion. `last_type_anchor_label` holds the human-readable
        # label of the typed-into field (e.g. "V3" or "[42]"). The next
        # type tool refuses if the target is a DIFFERENT field while
        # this flag is set — forces the brain to commit the suggestion
        # (click V_n) before moving on. Cleared on the next successful
        # click of any kind, on screenshot, or on timeout (30s).
        self.last_type_had_suggestions: bool = False
        self.last_type_anchor_label: str = ""
        # Most recent autocomplete-scan output (list of {text, x, y}).
        # Stashed verbatim from `_scan_autocomplete_suggestions` so the
        # worker_hook can map each suggestion's pixel center to a V_n
        # bbox in the next vision response — keeps the brain on the
        # bbox path even when the vision agent misses the
        # `autocomplete_open` flag.
        self.last_autocomplete_suggestions: list = []

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

        # Phase 1: hard sync gate. Tracks the most recent prefetch task
        # so the NEXT mutating tool can wait for it before acting on
        # potentially-stale state. Replaces the soft 2s budget that
        # otherwise lets the brain proceed on cached vision when the
        # prefetch hasn't landed.
        self._pending_vision_task: Optional["asyncio.Task[Any]"] = None
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

        # Anti-pattern detection: maps stamped id (e.g., 'my-companion-btn')
        # to _brain_turn_counter at the moment it was stamped via
        # browser_eval / browser_run_script. When a follow-up
        # browser_click_selector('#<id>') fires within MAX_STAMP_AGE
        # turns, we emit [anti_pattern_detected] so the brain learns
        # the stamp-then-click pattern is recognized and discouraged.
        self._recently_stamped_ids: dict[str, int] = {}

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
        self.task_instruction = (task_instruction or "")[:500]
        self.task_target_url = target_url or ""
        return self.max_screenshots

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
        # Drop cross-tool element identity tracking — a new session
        # starts with no recorded last click.
        self.last_click_dom_index = None

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
        # Cross-tool same-element guard releases on any fresh vision
        # epoch — the brain has re-observed the page, so a deliberate
        # toggle-off via DOM-index click on the previously-bbox-clicked
        # element is now permitted. Without this clear, the brain would
        # be permanently blocked from re-clicking after screenshot.
        self.last_click_dom_index = None

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

        Returns None when the epoch was previously frozen
        (`_vision_epoch_id > 0`) but has since been invalidated
        (`_vision_epoch_response is None`). This catches the
        mid-session staleness cascade: a previous click's mutation
        cleared the epoch, the background prefetch refreshed
        `_last_vision_response` with new bboxes the brain has not
        yet observed, and the brain's V_n indices were picked from
        the OLD bbox list. Returning None forces the consumer to
        emit its existing `no_vision` / `epoch_invalidated` error
        so the brain re-screenshots before clicking.
        """
        if self._vision_epoch_response is not None:
            epoch_url = self._normalize_url(self._vision_epoch_url or "")
            current_url = self._normalize_url(self.current_url or "")
            if epoch_url and current_url and epoch_url != current_url:
                # Page changed. Epoch is stale. Live response is the
                # post-mutation prefetch and matches the current page.
                return self._last_vision_response
            return self._vision_epoch_response
        # Epoch was previously frozen but is now None — invalidated
        # mid-session. Refuse to fall through to a prefetch the brain
        # hasn't seen. Allow `_vision_epoch_id == 0` (first turn /
        # tests) to keep falling through to the live response.
        if self._vision_epoch_id > 0:
            return None
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
                    "Call browser_solve_captcha or browser_click a tile — don't re-screenshot the same state.]"
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
        # Wall-clock for the click-pending-screenshot guard.
        import time as _t
        self.last_screenshot_at = _t.time()

    # --- stamp-id anti-pattern detection ----------------------------------
    #
    # The brain has been observed inventing this workaround when click_at
    # fails repeatedly:
    #     browser_eval(`document.querySelectorAll(...)[N].id = 'my-id'`)
    #     browser_click_selector('#my-id')
    # That bypasses the cursor ladder, stamps a synthetic id onto an
    # element via eval (isTrusted=false adjacent), and then clicks via
    # selector. It's bot-detectable on hardened sites and a sign the
    # brain is fighting the click tools instead of using them properly.
    # We watch for it at runtime and emit a one-shot [anti_pattern]
    # advisory the next time the brain reaches for a selector that
    # matches a recently-stamped id.

    _STAMP_ID_PATTERN = None  # lazy-init compiled regex (per-class cache)
    _STAMP_MAX_AGE_TURNS = 4

    def record_stamped_ids_from_script(self, script: str) -> None:
        """Scan a script for `.id = 'X'` or `setAttribute('id', 'X')`
        and remember X with the current brain_turn_counter. Always
        runs eviction (even on empty script) so stale stamps don't
        outlive their MAX_STAMP_AGE window when called from
        check_anti_pattern_selector's call sites.
        """
        cls = type(self)
        # Always evict stale entries first — keeps the window honest
        # regardless of how this method is reached.
        cutoff = self._brain_turn_counter - cls._STAMP_MAX_AGE_TURNS
        if self._recently_stamped_ids:
            self._recently_stamped_ids = {
                k: v for k, v in self._recently_stamped_ids.items()
                if v >= cutoff
            }
        if not script:
            return
        import re
        if cls._STAMP_ID_PATTERN is None:
            cls._STAMP_ID_PATTERN = re.compile(
                r"""\.id\s*=\s*['"]([\w\-]+)['"]"""
                r"""|setAttribute\(\s*['"]id['"]\s*,\s*['"]([\w\-]+)['"]"""
            )
        for m in cls._STAMP_ID_PATTERN.finditer(script):
            stamped = m.group(1) or m.group(2)
            if stamped:
                self._recently_stamped_ids[stamped] = self._brain_turn_counter

    def check_anti_pattern_selector(self, selector: str) -> str | None:
        """If `selector` is an id-selector (`#<id>`) AND <id> was stamped
        via eval / run_script within the last _STAMP_MAX_AGE_TURNS turns,
        return an advisory string. Else None. Only fires on an EXACT
        id-selector head — `#foo .bar` won't trip on a stamped `foo`
        because the brain is targeting a descendant, which suggests it
        had a reason. The pure `#stamped-id` case is the smoking gun.
        """
        s = (selector or "").strip()
        if not s.startswith("#"):
            return None
        # Extract candidate id: everything between `#` and the next
        # combinator / pseudo / attribute char. `#foo`, `#foo:hover`,
        # `#foo[disabled]`, `#foo > .bar` all extract `foo`.
        candidate = s[1:]
        for sep in (" ", ">", "+", "~", ":", "[", ".", "#"):
            if sep in candidate:
                candidate = candidate.split(sep, 1)[0]
        if not candidate or candidate not in self._recently_stamped_ids:
            return None
        # Inline staleness check so the advisory never fires past the
        # MAX_AGE window even if no eval/run_script call has come
        # through to trigger a fresh eviction pass.
        stamped_at = self._recently_stamped_ids[candidate]
        if self._brain_turn_counter - stamped_at > type(self)._STAMP_MAX_AGE_TURNS:
            del self._recently_stamped_ids[candidate]
            return None
        return (
            f"\n[anti_pattern_detected] id='{candidate}' was stamped via "
            f"browser_eval / browser_run_script in the last few actions "
            f"and you're now clicking it via selector. This pattern is "
            f"bot-detectable on hardened sites and a sign you're working "
            f"around click_at(V_n) / click([N]) instead of fixing the "
            f"underlying issue. Next time: re-screenshot to refresh the "
            f"V_n vision pass and use browser_click_at on the bbox, or "
            f"target the element via its existing CSS hooks (data-*, "
            f"aria-label, role) rather than a custom id you stamped."
        )

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

    def check_dead_click(
        self,
        click_target: str,
        current_active_state: bool | None = None,
        dom_index: int | None = None,
    ) -> str | None:
        """Pre-flight check before dispatching a click.

        Counts how many times this exact click target has been fired in
        a row without the page DOM changing in between. Once the count
        would reach `MAX_CONSECUTIVE_SAME_TARGET`, refuse with a
        structured error so the brain is forced to pick a different
        target. Different target OR a DOM change resets the count.

        DOM change is detected via `_last_dom_hash` (set by every state
        fetch).

        v4 C3 — toggle-aware exemption. When the bbox has `is_active`
        flipped between the previous click and this one (was True, now
        False, or vice versa), the prior click was a successful toggle
        (filter applied/removed), and this click is a legitimate undo
        — NOT flailing. Reset the counter so the brain can re-click to
        un-toggle without being blocked. `current_active_state` is the
        bbox's is_active right NOW (read from the most recent vision
        response); compared against `last_click_target_active_state`
        recorded at the previous click's dispatch.

        Cross-tool same-element guard. The previous dead-click guard
        compared `click_target` strings: `click[38]` vs `click_at(V7)`
        differ even when both reference the SAME DOM element. So a
        DOM-index click immediately after a bbox click on the same
        checkbox passed silently, un-toggling the brain's correct
        toggle. Now that bbox clicks plumb their resolved `dom_index`
        back through `clickInBbox` and stash it in
        `last_click_dom_index`, this method accepts the new tool's
        dom_index and hard-blocks when:
          - the indices match (same DOM element);
          - the page hasn't been re-observed since (same _last_dom_hash);
          - and the click isn't a deliberate toggle (toggle_exempt
            releases legitimate undo-via-click cases).
        On block, returns a `[same_element_blocked]` structured error
        instructing the brain to call `browser_screenshot` first.

        Returns a structured error string when blocking, or None when
        the click is allowed to proceed.
        """
        import os as _os
        same_target = (click_target == self.last_click_target)
        same_dom = (
            bool(self._last_dom_hash)
            and self._last_dom_hash == self.last_click_dom_hash
        )
        # Toggle-flip detection: only meaningful when both states are
        # known (not None) AND they differ.
        toggle_exempt = False
        if (
            _os.environ.get("BBOX_TOGGLE_DEADCLICK_EXEMPT", "1")
                not in ("0", "false", "no")
            and same_target
            and self.last_click_target_active_state is not None
            and current_active_state is not None
            and bool(self.last_click_target_active_state)
                != bool(current_active_state)
        ):
            toggle_exempt = True
        # Cross-tool same-element check. Fires BEFORE the existing
        # same-target/same-dom counter logic so the structured error
        # the brain sees says "you already clicked this element, take
        # a screenshot first" rather than "dead_click_blocked".
        if (
            _os.environ.get("CROSS_TOOL_SAME_ELEMENT_BLOCK", "1")
                not in ("0", "false", "no")
            and dom_index is not None
            and self.last_click_dom_index is not None
            and dom_index == self.last_click_dom_index
            and same_dom
            and not toggle_exempt
            # Don't fire when the brain is targeting the same element
            # via the same tool family (e.g., click[38] then click[38]
            # again) — the existing same-target counter handles that.
            and not same_target
        ):
            prior = self.last_click_target or "(prior click)"
            return (
                f"[same_element_blocked dom_index={dom_index}] You "
                f"already clicked this element via {prior} and the "
                f"page hasn't been re-observed since. The previous "
                f"click toggled it; clicking again will REVERSE that "
                f"toggle (un-applying the filter / un-checking the "
                f"checkbox). If that's what you want, call "
                f"browser_screenshot first to confirm the new state, "
                f"then retry. If not, pick a different target."
            )
        if same_target and same_dom and not toggle_exempt:
            # Nth consecutive dead attempt at the same target.
            self.consecutive_dead_clicks += 1
        elif toggle_exempt:
            # Successful toggle/un-toggle — fresh strike count.
            self.consecutive_dead_clicks = 1
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
                "re-observe, then pick a different [V_n]/[index], try a "
                "different role (e.g., the form's submit button instead "
                "of the input), try browser_click_selector with a stable "
                "CSS hook, or browser_wait_for content you expect to "
                "appear. Do NOT retry this exact target, and do NOT "
                "synthesize clicks via browser_run_script — JS clicks "
                "are isTrusted=false and bot-detected."
            )
        return None

    def register_click_attempt(
        self,
        click_target: str,
        *,
        target_label: str = "",
        target_active_state: bool | None = None,
        target_box_2d: list[int] | None = None,
        target_dom_index: int | None = None,
    ) -> None:
        """Stamp the current click target + DOM hash so the next call to
        `check_dead_click` can compare against them.

        v4: also record the bbox's label, current is_active state, and
        normalized box_2d. The next vision pass uses these to:
          - detect toggle flips (C3 dead-click exemption)
          - find the same bbox in the post-click response and stamp
            `just_toggled='on'`/`'off'` on it (C6).

        Cross-tool: also accept `target_dom_index`. DOM-index clicks
        pass the index directly; bbox clicks pass the resolved
        `data.snap.dom_index` from the post-click response (see
        BrowserClickAtTool's success branch). When set, the next
        `check_dead_click(dom_index=N)` from a different tool family
        recognizes the same DOM element and hard-blocks.
        """
        self.last_click_target = click_target
        self.last_click_dom_hash = self._last_dom_hash
        self.last_click_target_label = target_label
        self.last_click_target_active_state = target_active_state
        self.last_click_target_box_2d = (
            list(target_box_2d) if target_box_2d else None
        )
        if target_dom_index is not None:
            self.last_click_dom_index = int(target_dom_index)

    def advance_observation_token(self, source: str = "") -> None:
        """No-op shim retained so kept tools (click_selector,
        rewind_to_checkpoint, scroll_until, drag_slider_until) that
        were ported forward from the validator era can still call it
        without blowing up. The token machinery was part of the
        deleted validator subsystem; in the reverted architecture the
        in-tool freshness/blocker/confidence gates in click_at do
        the same job."""
        pass

    # --- Surgical undo ring (Problem 1) -------------------------------

    _IRREVERSIBLE_LABEL_RE = re.compile(
        r"^\s*(?:please\s+)?(delete|remove|cancel\s+order|submit"
        r"|place\s+order|buy|pay|confirm|send|checkout|purchase"
        r"|sign\s+out|log\s*out)\b",
        re.IGNORECASE,
    )

    def begin_click_record(
        self,
        *,
        tool: str,
        target_key: str,
        vision_index: int | None,
        label: str,
        box_2d: list[int] | None,
        pre_active: bool | None,
        expected_url_change: bool = False,
        is_form_submit: bool = False,
    ) -> None:
        """Start a pending undo entry.

        Called BEFORE HTTP dispatch by each click tool. Decides the
        reversibility class up-front because by the time the click
        response arrives, the page may already have unloaded (we'd
        have lost the chance to label this click as e.g. nav vs
        toggle). `finalize_click_record` later refines the class
        when the response comes back (e.g. demotes toggle→nav when
        the response shows url_changed=True).
        """
        label_clean = (label or "")[:200]
        if is_form_submit:
            kind = "irreversible"
        elif expected_url_change:
            kind = "nav"
        elif label_clean and self._IRREVERSIBLE_LABEL_RE.match(label_clean):
            kind = "irreversible"
        elif pre_active is not None:
            kind = "toggle"
        else:
            kind = "toggle"
        self._pending_undo_entry = {
            "kind": kind,
            "tool": tool,
            "target_key": target_key,
            "vision_index": vision_index,
            "label": label_clean,
            "box_2d": list(box_2d) if box_2d else None,
            "pre_active": pre_active,
            "pre_url": self.current_url or "",
            "pre_dom_hash": self._last_dom_hash or "",
            "post_url": "",
            "url_changed": False,
            "mutation_delta": 0,
            "post_active": None,
            "session_id": self.session_id,
            "ts": time.time(),
            "turn": self._brain_turn_counter,
            "misclick_flag": False,
            "misclick_evidence": None,
            "consumed": False,
        }

    def finalize_click_record(
        self,
        *,
        response: dict | None,
        pre_url: str = "",
        pre_dom_hash: str = "",
    ) -> None:
        """Fold the click response into the pending entry and push it
        onto the ring. Called AFTER the click response lands.

        Demotes `toggle → nav` when `effect.url_changed=True` so a
        later undo of this entry uses history.back() instead of a
        re-click.
        """
        entry = self._pending_undo_entry
        self._pending_undo_entry = None
        if entry is None:
            return
        if pre_url and not entry.get("pre_url"):
            entry["pre_url"] = pre_url
        if pre_dom_hash and not entry.get("pre_dom_hash"):
            entry["pre_dom_hash"] = pre_dom_hash
        data = response or {}
        effect = data.get("effect") if isinstance(data, dict) else None
        if isinstance(effect, dict):
            entry["url_changed"] = bool(effect.get("url_changed"))
            try:
                entry["mutation_delta"] = int(effect.get("mutation_delta") or 0)
            except (TypeError, ValueError):
                entry["mutation_delta"] = 0
        entry["post_url"] = (
            (data.get("url") if isinstance(data, dict) else None)
            or self.current_url
            or ""
        )
        # Demote toggle → nav when the click actually navigated. A nav
        # is only undoable via history.back; re-clicking the same target
        # on a navigated page would be wrong.
        if entry["kind"] == "toggle" and entry.get("url_changed"):
            entry["kind"] = "nav"
        self._undo_ring.append(entry)
        if len(self._undo_ring) > self.UNDO_RING_MAX:
            # Drop the oldest entries first.
            self._undo_ring = self._undo_ring[-self.UNDO_RING_MAX:]

    def latest_undo_candidate(self) -> dict | None:
        """Return the top unconsumed ring entry, or None if the ring
        is empty / all entries are consumed.
        """
        for entry in reversed(self._undo_ring):
            if not entry.get("consumed"):
                return entry
        return None

    def pop_undo_candidates(self, steps: int) -> list[dict]:
        """Pop up to N unconsumed entries from the top of the ring.

        Stops at the first `irreversible` entry (which is RETURNED as
        the first element if it sits on top — caller decides whether
        to refuse). Otherwise returns up to `steps` entries newest-
        first.
        """
        if steps <= 0:
            return []
        out: list[dict] = []
        for entry in reversed(self._undo_ring):
            if entry.get("consumed"):
                continue
            out.append(entry)
            if entry.get("kind") == "irreversible":
                break
            if len(out) >= steps:
                break
        return out

    def mark_undone(self, entry: dict) -> None:
        """Mark a ring entry as consumed."""
        entry["consumed"] = True

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
        selector_entries: list[dict] | None = None,
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
                # v2-C: compound-row sub-bbox split. Safety net for when
                # vision merged a parent control + chevron into one
                # bbox — the selectorEntries from the screenshot tool
                # know where the chevron sits, so we can inject a
                # dedicated V_n. No-op when vision split it itself.
                try:
                    from .vision_pipeline import _apply_compound_row_split
                    _apply_compound_row_split(
                        resp,
                        selector_entries or [],
                        img_w,
                        img_h,
                        device_pixel_ratio,
                        self.task_instruction,
                    )
                except Exception as exc:
                    print(f"  [compound_row_split build_blocks failed: {exc}]")
                # B3: attach DOM-derived metadata to each bbox so the
                # brain sees parent/child/expanded-state/disabled/active
                # context (the "California is under United States" chain).
                try:
                    from .vision_pipeline import (
                        _enrich_bboxes_with_dom_metadata,
                    )
                    _enrich_bboxes_with_dom_metadata(
                        resp,
                        selector_entries or [],
                        img_w,
                        img_h,
                        device_pixel_ratio,
                        self.task_instruction,
                    )
                except Exception as exc:
                    print(f"  [dom_enrichment build_blocks failed: {exc}]")
                # v4 C6 — stamp just_toggled on the bbox the brain
                # just clicked, when is_active flipped vs. what was
                # recorded at click dispatch. Surfaces as
                # `active=true just_toggled=on` in brain text so the
                # brain can re-click to un-toggle filter mistakes.
                try:
                    from .vision_pipeline import _apply_just_toggled_marker
                    _apply_just_toggled_marker(resp, self)
                except Exception as exc:
                    print(f"  [just_toggled build_blocks failed: {exc}]")
                try:
                    from .vision_pipeline import _detect_misclick_flip
                    _detect_misclick_flip(resp, self)
                except Exception as exc:
                    print(f"  [misclick_detect build_blocks failed: {exc}]")
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
        # Surface scroll geometry the TS bridge reported on this action
        # response. Wires up the [SCROLL_STATE …] contract the vision
        # system prompt already references — vision can suggest a
        # `scroll` action with target text when reached_bottom=false,
        # and the worker_hook scroll-stagnation guard reads the same
        # signal. Best-effort; never fails the result string.
        try:
            tel = getattr(self, "scroll_telemetry", None) or {}
            if tel:
                pos = int(tel.get("scrollY", 0) or 0)
                h = int(tel.get("scrollHeight", 0) or 0)
                vp = int(tel.get("viewportHeight", 0) or 0)
                flags: list[str] = []
                if tel.get("reached_top"):
                    flags.append("reached_top=true")
                if tel.get("reached_bottom"):
                    flags.append("reached_bottom=true")
                hist = list(tel.get("direction_history") or [])[-3:]
                if hist:
                    flags.append("last_dirs=" + ",".join(hist))
                tail = (" " + " ".join(flags)) if flags else ""
                result += f"\n[SCROLL_STATE pos={pos}/{h} vp={vp}{tail}]"
        except Exception:
            pass
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
