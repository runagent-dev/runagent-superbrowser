"""
Mid-session guardrails for the browser worker agent.

Uses the nanobot AgentHook lifecycle to inject corrective guidance
into the conversation when the worker goes off-track (click-screenshot
loops, regression navigation, stagnation, iteration budget pressure).

Guidance is injected by appending text to the last tool result message,
preserving the assistant/tool message alternation expected by LLM APIs.
"""

from __future__ import annotations

import os
import re

from nanobot.agent.hook import AgentHook, AgentHookContext

from superbrowser_bridge.session_tools import BrowserSessionState
from superbrowser_bridge.loop_detector import LoopDetector

# Phase 3.3 helpers for chevron-focus guidance.
_CHEVRON_CHARS_SET = set("▼▶◀▲►◄⌃⌄⋮")
_CHEVRON_EXPAND_PATTERN = re.compile(r"^expand\s+(.+?)$", re.IGNORECASE)
_CHEVRON_TASK_STOPWORDS = frozenset({
    "this", "that", "with", "from", "into", "open", "click", "find",
    "select", "filter", "show", "tell", "what", "which", "where",
    "when", "page", "site", "link", "button", "search", "result",
    "results", "item", "items", "list", "menu", "option", "options",
})


def _replicate_bbox_rank(bbox: object) -> tuple[int, int, int, float]:
    """Mirror VisionResponse._rank used by as_brain_text + get_bbox.

    Kept inline so we don't reach into a private function on the
    schemas module. If the upstream ranking changes, update both.
    """
    role_in_scene = getattr(bbox, "role_in_scene", "") or ""
    if role_in_scene == "blocker":
        role_rank = 0
    elif role_in_scene == "target":
        role_rank = 1
    else:
        role_rank = 2
    try:
        confidence = float(getattr(bbox, "confidence", 0.5) or 0.5)
    except (TypeError, ValueError):
        confidence = 0.5
    return (
        role_rank,
        0 if getattr(bbox, "intent_relevant", False) else 1,
        0 if getattr(bbox, "clickable", False) else 1,
        -confidence,
    )


class BrowserWorkerHook(AgentHook):
    """Injects mid-loop corrective guidance based on worker state."""

    def __init__(self, state: BrowserSessionState, max_iterations: int = 50):
        self.state = state
        self.max_iterations = max_iterations
        self._last_budget_warning_at: int = -1  # iteration of last warning
        self._captcha_guidance_given: bool = False
        self._captcha_solve_attempts: int = 0
        self._captcha_escalation_pending: bool = False
        self._captcha_escalation_turns: int = 0
        # Generic loop + stagnation detector (replaces consecutive_click_calls +
        # ad-hoc _stagnation_url/_stagnation_count logic).
        self._loop = LoopDetector()
        # Tier-auto-escalation: fires at most once per session to avoid
        # loop-cascading the LLM into repeated escalations.
        self._auto_escalated: bool = False

    async def after_iteration(self, context: AgentHookContext) -> None:
        """Inject guidance after each tool execution round."""
        guidance_parts: list[str] = []

        # Decrement captcha_mode counter once per iteration. Lets the
        # screenshot-budget limiter automatically re-engage after solving.
        self.state.tick_captcha_mode()

        # --- Generic action-repetition detection ---
        # Inspect the last recorded step (added by the tool that just ran)
        # and feed it to the loop detector.
        last_step = self.state.step_history[-1] if self.state.step_history else None
        if last_step:
            tool = last_step.get("tool") or ""
            args = {"args_summary": last_step.get("args", "")}
            action_nudge = self._loop.record_action(tool, args)
            if action_nudge:
                guidance_parts.append(action_nudge)

            # Stagnation by (url, page-fingerprint). We proxy "page content"
            # with the truncated result string; it's good enough to detect
            # "same page, same elements" unchanged across iterations.
            stag_nudge = self._loop.record_page_state(
                last_step.get("url") or "",
                last_step.get("result") or "",
            )
            if stag_nudge:
                guidance_parts.append(stag_nudge)

        # --- Iteration budget warnings ---
        iteration = context.iteration
        remaining = self.max_iterations - iteration - 1

        if remaining <= int(self.max_iterations * 0.2) and self._last_budget_warning_at != iteration:
            # 20% or less remaining — prioritize, don't panic.
            self._last_budget_warning_at = iteration
            guidance_parts.append(
                f"[GUIDANCE: {remaining} iterations left out of {self.max_iterations}. "
                "Prioritize extracting the real data with browser_get_markdown. "
                "Do NOT fabricate values — if the data cannot be obtained, report it "
                "honestly via done(success=False).]"
            )
        elif remaining <= int(self.max_iterations * 0.4) and self._last_budget_warning_at != iteration:
            # 40% or less remaining
            self._last_budget_warning_at = iteration
            guidance_parts.append(
                f"[GUIDANCE: {remaining} iterations left out of "
                f"{self.max_iterations}. Switch to browser_run_script NOW "
                "to batch all remaining work into one script call.]"
            )

        # --- Detect regression (already handled at tool level, reinforce here) ---
        if self.state.regression_count > 0 and self.state.best_checkpoint_url:
            # Only inject if regression happened this iteration
            recent_steps = self.state.step_history[-2:] if len(self.state.step_history) >= 2 else []
            for step in recent_steps:
                if step["tool"] == "browser_navigate" and "FAILED" not in step.get("result", ""):
                    step_url = step.get("url", "")
                    if step_url and self.state.is_regression(step_url):
                        guidance_parts.append(
                            "[GUIDANCE: You navigated backward instead of fixing "
                            "your approach on the current page. Your best progress "
                            f"was at: {self.state.best_checkpoint_url}. "
                            "Do NOT restart from the beginning — fix your script "
                            "on the current page.]"
                        )
                        break

        # --- One auto-solve attempt, then straight to human ---
        # Previously this allowed up to 3 solve attempts before nudging the
        # agent off the loop. That pattern trained sites to fingerprint us
        # as a bot. Under the fast-to-human policy a single failed auto
        # solve means pivot to the human: browser_ask_user surfaces the
        # live view, the user clicks through once, and bot-protection
        # cookies get persisted by cookie-jar for the next run.
        #
        # The auto-escalation in BrowserSolveCaptchaTool now handles this
        # deterministically (no LLM decision needed). This hook is the
        # backup: if the LLM somehow calls browser_solve_captcha without
        # auto_escalate, or if it ignores the auto-escalation result.
        recent_steps = self.state.step_history[-1:] if self.state.step_history else []
        for step in recent_steps:
            if step["tool"] == "browser_solve_captcha":
                self._captcha_solve_attempts += 1
                result_text = str(step.get("result", ""))
                solved = "SOLVED" in result_text or '"solved": true' in result_text
                if not solved and self._captcha_solve_attempts >= 1:
                    self._captcha_escalation_pending = True
                    sid = self.state.session_id or "<session_id>"
                    guidance_parts.append(
                        "[GUIDANCE: Auto-solve failed once — do NOT retry. "
                        "Sites fingerprint repeated solver pings as bot activity. "
                        "Hand off to the human NOW:\n"
                        f"  browser_ask_user(session_id='{sid}', "
                        "input_type='captcha', "
                        "question='Please open the live view URL and "
                        "click through the captcha — I will detect when "
                        "it clears and resume.')\n"
                        "The tool blocks while the user solves. "
                        "Do NOT call browser_solve_captcha again.]"
                    )

        # If escalation was requested but the LLM didn't call browser_ask_user
        # on its next turn, escalate the urgency.
        if self._captcha_escalation_pending:
            last_tool = (self.state.step_history[-1].get("tool") or "") if self.state.step_history else ""
            if last_tool == "browser_ask_user":
                self._captcha_escalation_pending = False
                self._captcha_escalation_turns = 0
            elif last_tool != "browser_solve_captcha":
                self._captcha_escalation_turns += 1
                if self._captcha_escalation_turns >= 2:
                    sid = self.state.session_id or "<session_id>"
                    guidance_parts.append(
                        f"[MANDATORY: You MUST call browser_ask_user(session_id='{sid}', "
                        "input_type='captcha', question='Please solve the captcha') NOW. "
                        "No other actions are allowed until you do. The captcha will NOT "
                        "resolve itself — a human must solve it.]"
                    )

        # --- Detect verification/captcha pages ---
        if self.state.session_id and not self._captcha_guidance_given:
            current_url = self.state.current_url or ""
            # Check URL patterns that indicate verification/bot-protection
            blocking_url_patterns = [
                "/login", "/signin", "/auth", "/verify",
                "/challenge", "/captcha", "/security",
            ]
            url_looks_blocking = any(
                p in current_url.lower() for p in blocking_url_patterns
            )

            # Check recent step results for blocking signals
            recent = (
                self.state.step_history[-3:]
                if self.state.step_history
                else []
            )
            text_signals = [
                "verify", "captcha", "security check", "just a moment",
                "are you a robot", "human verification", "prove you",
                "slide to verify", "complete the puzzle",
            ]
            result_looks_blocking = any(
                any(
                    sig in (step.get("result", "") or "").lower()
                    for sig in text_signals
                )
                for step in recent
            )

            if url_looks_blocking or result_looks_blocking:
                self._captcha_guidance_given = True
                sid = self.state.session_id
                guidance_parts.append(
                    "[GUIDANCE: This page appears to be a CAPTCHA or "
                    "security verification — NOT a login page. "
                    f"Call browser_detect_captcha(session_id='{sid}') "
                    "to check, then "
                    f"browser_solve_captcha(session_id='{sid}', "
                    "method='auto') to solve it. "
                    "Do NOT report LOGIN REQUIRED for bot protection "
                    "pages.]"
                )

        # --- Tier auto-escalation (t1 → t3) -----------------------------
        # Fires when a t1 tool flagged network_blocked OR vision detected a
        # captcha. Surfaces a crisp, directive guidance block telling the LLM
        # to call browser_escalate next. We do NOT call browser_escalate from
        # the hook itself — tool dispatch stays under the LLM's control so
        # the tool call appears in the transcript and the brain can react to
        # the return value. One-shot per session.
        if (
            not self._auto_escalated
            and self.state.session_id
            and self.state.backend == "t1"
        ):
            should_escalate = False
            reason = ""
            if self.state.network_blocked:
                should_escalate = True
                reason = f"network_blocked:HTTP_{self.state.last_network_status or '?'}"
            else:
                last_vision = getattr(self.state, "_last_vision_response", None)
                flags = getattr(last_vision, "flags", None)
                if flags is not None and bool(getattr(flags, "captcha_present", False)):
                    should_escalate = True
                    ct = getattr(flags, "captcha_type", None)
                    reason = f"vision_captcha:{ct or 'unspecified'}"

            if should_escalate:
                self._auto_escalated = True
                sid = self.state.session_id
                guidance_parts.append(
                    "[AUTO_ESCALATION_ADVISED] Tier 1 (Puppeteer) hit "
                    f"anti-bot protection ({reason}). "
                    f"Call browser_escalate(session_id='{sid}', reason='{reason}') "
                    "NOW. This migrates the session to Tier 3 (undetected "
                    "Chromium) preserving cookies + URL. The returned "
                    "new_session_id is what all subsequent browser_* tools "
                    "must use. Do NOT retry on the current session — Akamai-"
                    "class protections will not relent on the same IP+TLS "
                    "fingerprint."
                )

        # --- Phase 2: form-fill checklist reminder ----------------------
        # While a form_session is active, remind the brain at every
        # iteration which fields still need filling. The session itself
        # tracks state via browser_type_at + form_commit; this hook is
        # the persistent visual nudge so the brain can't forget the
        # field hidden behind an autocomplete dropdown.
        form_sess = getattr(self.state, "form_session", None)
        if form_sess is not None:
            try:
                checklist = form_sess.remaining_checklist(max_lines=10)
                if checklist:
                    needs = form_sess.needs_screenshot(
                        getattr(self.state, "_brain_turn_counter", 0)
                    )
                    pieces = [checklist]
                    if needs:
                        pieces.append(needs)
                    guidance_parts.append("\n".join(pieces))
            except Exception:
                pass

        # --- Phase 3.2: scroll-stagnation hint --------------------------
        # Below-fold target hallucination guard. Fires before any cursor
        # failure has been logged — catches the brain that re-screenshots
        # the same viewport hunting V_n indices that don't exist (because
        # the target sits below the fold) and is about to escalate to
        # browser_run_script. Steers it into browser_scroll_until.
        try:
            tel = getattr(self.state, "scroll_telemetry", None) or {}
            scroll_h = int(tel.get("scrollHeight", 0) or 0)
            vp_h = int(tel.get("viewportHeight", 0) or 0)
            has_capacity = scroll_h > vp_h + 200
            reached_bottom = bool(tel.get("reached_bottom"))
            recent = self.state.step_history[-3:] if self.state.step_history else []
            recent_tools = [s.get("tool", "") for s in recent]
            tried_scroll = any(t.startswith("browser_scroll") for t in recent_tools)
            only_screenshots = (
                len(recent_tools) >= 2
                and all(t == "browser_screenshot" for t in recent_tools)
            )
            if (
                has_capacity
                and not reached_bottom
                and not tried_scroll
                and only_screenshots
            ):
                sid = self.state.session_id or "<session_id>"
                guidance_parts.append(
                    f"[SCROLL_HINT pos={int(tel.get('scrollY', 0) or 0)}/"
                    f"{scroll_h} vp={vp_h}]\n"
                    "The page has below-fold content and no scroll has "
                    "happened in the last 3 turns — only re-screenshots. "
                    "If your target (filter, button, named control) is "
                    "plausibly off-screen, call:\n"
                    f"  browser_scroll_until(session_id='{sid}', "
                    "target_text='<label>')\n"
                    "It walks the page in fine steps, narrates labels "
                    "passed at each step, and tells you whether the "
                    "label exists on this page (look for "
                    "`reversed_no_match` — that means it doesn't). Do "
                    "NOT keep screenshotting the same viewport, and do "
                    "NOT reach for browser_run_script(mutates=true) — "
                    "the run_script gate will refuse it until you have "
                    "scrolled."
                )
        except Exception:
            pass

        # --- Phase 3.3: chevron-focus hint ------------------------------
        # Compound-row sub-tree picker. When vision has emitted both a
        # parent row bbox AND a sibling chevron/expand bbox, AND the
        # task names something specific that the parent label doesn't
        # cover (e.g. task says "California" but the row is "United
        # States"), nudge the brain at the chevron — clicking the
        # parent row would select the WHOLE group and miss the sub-
        # selection the task requires.
        if os.environ.get("WORKER_CHEVRON_FOCUS", "1") not in ("0", "false", "no"):
            try:
                last_resp = getattr(self.state, "_last_vision_response", None)
                bboxes = getattr(last_resp, "bboxes", None) if last_resp else None
                task_text = (getattr(self.state, "task_instruction", "") or "").lower()
                if bboxes and task_text:
                    # Match the brain's V_n indexing — same rank fn as
                    # as_brain_text + get_bbox. Cap at 50 like the renderer.
                    try:
                        ranked = sorted(bboxes, key=_replicate_bbox_rank)[:50]
                    except Exception:
                        ranked = list(bboxes)[:50]
                    # Build a label → V_n lookup over the ranked window.
                    label_to_v: dict[str, int] = {}
                    for idx, b in enumerate(ranked, 1):
                        lbl = (getattr(b, "label", "") or "").strip().lower()
                        if lbl and lbl not in label_to_v:
                            label_to_v[lbl] = idx
                    # Pre-extract task tokens once (length>=4, lowercase).
                    task_tokens = [
                        t for t in re.findall(r"\b[a-z]{4,}\b", task_text)
                        if t not in _CHEVRON_TASK_STOPWORDS
                    ]
                    # Avoid firing the hint twice if brain already
                    # clicked the chevron. Check last 5 click_at
                    # attempts for the candidate V_n.
                    recent_click_args = [
                        str(s.get("args", ""))
                        for s in (self.state.step_history or [])[-5:]
                        if (s.get("tool") or "").startswith("browser_click")
                    ]
                    fired = False
                    for chev_idx, b in enumerate(ranked, 1):
                        if fired:
                            break
                        lbl = (getattr(b, "label", "") or "").strip()
                        if not lbl:
                            continue
                        # Detect "Expand X" or single-chevron-character labels.
                        m = _CHEVRON_EXPAND_PATTERN.match(lbl)
                        is_chevron_glyph = (
                            len(lbl) <= 4
                            and any(c in _CHEVRON_CHARS_SET for c in lbl)
                        )
                        if not (m or is_chevron_glyph):
                            continue
                        parent_label = m.group(1).strip() if m else ""
                        if not parent_label:
                            # Glyph-only label can't tell us the parent.
                            continue
                        parent_v = label_to_v.get(parent_label.lower())
                        if parent_v is None or parent_v == chev_idx:
                            continue
                        # Task must mention something the parent doesn't.
                        parent_tokens = set(
                            re.findall(r"\b[a-z]{4,}\b", parent_label.lower())
                        )
                        interesting = [
                            t for t in task_tokens if t not in parent_tokens
                        ]
                        if not interesting:
                            continue
                        # Skip if brain already clicked V{chev_idx} recently.
                        v_marker = f"V{chev_idx}"
                        if any(v_marker in arg for arg in recent_click_args):
                            continue
                        sample = interesting[0]
                        guidance_parts.append(
                            f"[CHEVRON_FOCUS V{chev_idx}]\n"
                            f"Task mentions '{sample}'. Row {parent_label!r} "
                            f"(V{parent_v}) is a GROUP label — clicking "
                            f"V{parent_v} selects the WHOLE group and skips "
                            f"the sub-tree where '{sample}' lives. The "
                            f"sibling chevron 'Expand {parent_label}' "
                            f"(V{chev_idx}) opens that sub-tree. Click "
                            f"V{chev_idx}, NOT V{parent_v}."
                        )
                        fired = True
            except Exception:
                pass

        # --- Phase 3.4: precondition reminder ---------------------------
        # When an intent-relevant bbox has a collapsed parent
        # expand-button, surface a [PRECONDITION] block so the brain
        # explicitly sees "click V_n to expand BEFORE clicking V_m".
        # Complements the click_at gate (B5) which catches the wrong
        # click after the fact — this fires before the brain even
        # tries.
        if os.environ.get("WORKER_PRECONDITION_HINT", "1") not in ("0", "false", "no"):
            try:
                last_resp = getattr(self.state, "_last_vision_response", None)
                bboxes = getattr(last_resp, "bboxes", None) if last_resp else None
                if bboxes:
                    try:
                        ranked = sorted(bboxes, key=_replicate_bbox_rank)[:50]
                    except Exception:
                        ranked = list(bboxes)[:50]
                    parent_v_to_label: dict[int, str] = {}
                    candidates: list[tuple[int, int, str, str]] = []
                    # First pass: collect ranked-V_n for every bbox by
                    # identity, plus collect (child_v, parent_v, child_label).
                    v_by_id = {id(b): i for i, b in enumerate(ranked, 1)}
                    for child_idx, b in enumerate(ranked, 1):
                        parent_v = getattr(b, "parent_expand_v", None)
                        if not isinstance(parent_v, int) or parent_v <= 0:
                            continue
                        # Only fire on intent-relevant children — too
                        # noisy otherwise on dense filter pages.
                        if not getattr(b, "intent_relevant", False):
                            continue
                        # Resolve parent bbox to read its expansion state.
                        try:
                            parent_bbox = last_resp.get_bbox(parent_v)
                        except Exception:
                            parent_bbox = None
                        if parent_bbox is None:
                            continue
                        if getattr(parent_bbox, "aria_expanded", None) != "false":
                            continue
                        child_label = (getattr(b, "label", "") or "").strip()
                        parent_label = (getattr(parent_bbox, "label", "") or "").strip()
                        if not parent_label:
                            continue
                        # De-dup by parent_v so we don't repeat the same
                        # 'expand X first' message for every child.
                        if parent_v in parent_v_to_label:
                            continue
                        parent_v_to_label[parent_v] = parent_label
                        candidates.append((child_idx, parent_v, child_label, parent_label))
                    if candidates:
                        # Cap at 2 distinct preconditions per turn — beyond
                        # that the message gets too long.
                        msg_lines: list[str] = ["[PRECONDITION]"]
                        for child_v, parent_v, child_lbl, parent_lbl in candidates[:2]:
                            msg_lines.append(
                                f"  V{child_v} ({child_lbl!r}) is hidden "
                                f"under collapsed group {parent_lbl!r} "
                                f"(V{parent_v}, aria_expanded=false). "
                                f"Click V{parent_v} FIRST to expand, then "
                                f"re-screenshot and target V{child_v}."
                            )
                        guidance_parts.append("\n".join(msg_lines))
            except Exception:
                pass

        # --- Phase 3.1: cursor-failure ledger reminder ------------------
        # When the brain has failed at least one cursor strategy, surface
        # the ledger so it knows to try a DIFFERENT cursor strategy next
        # rather than reach for browser_run_script (which the lockout
        # gate will refuse anyway until a second strategy fails).
        try:
            recs = getattr(self.state, "cursor_failure_records", None) or []
            distinct = len(getattr(self.state, "cursor_failure_strategies", set()) or set())
            if recs and distinct < 2:
                tried = ", ".join(sorted(self.state.cursor_failure_strategies)) or "(none)"
                guidance_parts.append(
                    "[CURSOR_FAILURES_SO_FAR strategies_tried="
                    f"{tried} distinct={distinct}/2]\n"
                    "Try a DIFFERENT cursor strategy before considering "
                    "browser_run_script(mutates=true). The script lockout "
                    "will refuse it until 2 distinct cursor strategies have "
                    "failed."
                )
        except Exception:
            pass

        # --- Inject guidance into the last tool result message ---
        if guidance_parts and context.messages:
            guidance_text = "\n" + "\n".join(guidance_parts)
            # Find the last tool-result message and append guidance to it
            for i in range(len(context.messages) - 1, -1, -1):
                msg = context.messages[i]
                if msg.get("role") == "tool":
                    if isinstance(msg.get("content"), str):
                        msg["content"] += guidance_text
                    elif isinstance(msg.get("content"), list):
                        # Multimodal content (image blocks) — append as text block
                        msg["content"].append({"type": "text", "text": guidance_text})
                    break
