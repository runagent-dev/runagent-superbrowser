"""<select> + label-anchored option pickers + tracked form-fill orchestration.

`BrowserSelectTool` (DOM-index <select>), `BrowserSelectOptionTool` (label
+ value picker for any dropdown widget), `BrowserFormPlanTool` (one-shot
cascading filter execution), and the trio that drives a tracked form-fill
session: `BrowserFormBeginTool`, `BrowserFormStatusTool`,
`BrowserFormCommitTool`.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import time
from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from ..formatting import _fetch_elements
from nanobot.agent.tools.schema import (
    ArraySchema,
    BooleanSchema,
    IntegerSchema,
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)

from ..http_client import SUPERBROWSER_URL, _request_with_backoff
from ..state import BrowserSessionState
from ..vision_pipeline import _append_fresh_vision, _schedule_vision_prefetch


# ---------------------------------------------------------------------------
# Python port of the 4-tier scored matcher from src/browser/elements.ts
# (selectOptionByLabel's pickAttempt, lines 1416-1594). Used in two places:
#   1. The JS-in-Python recovery click-selector below — runs in the page so
#      it can resolve `data-sb-opt-candidate` style off-screen elements.
#   2. The Python-side option list annotator (`_score_option_text`) — runs
#      after enumeration so we can show the brain WHY a value didn't match
#      cleanly (e.g. score=0.78 contains-word vs floor=0.85 fuzzy).
#
# Tier order: exact-ci > startsWith-word > contains-word > fuzzy≥0.85.
# Shortest-text tiebreak; ambiguity reported instead of guessing.
# ---------------------------------------------------------------------------


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    m, n = len(a), len(b)
    if m == 0:
        return n
    if n == 0:
        return m
    prev = list(range(n + 1))
    for i in range(1, m + 1):
        curr = [i] + [0] * n
        for j in range(1, n + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[n]


def _is_word_char(c: str) -> bool:
    return bool(c) and (c.isalnum() or c == "_")


def _score_option_text(needle: str, text: str) -> tuple[str, float]:
    """Return ``(tier, score)`` describing how well ``text`` matches ``needle``.

    Tiers (descending priority):
      * ``exact``           — case-insensitive whole-string equality
      * ``startsWith-word`` — needle is a word-boundary prefix
      * ``contains-word``   — needle appears at a word boundary
      * ``fuzzy``           — Levenshtein similarity ≥ 0.85, needle length ≥ 4
      * ``none``            — no match

    Score is roughly comparable across tiers: closer to 1.0 = better.
    Tiers below ``exact`` reward specificity (shorter option text).
    """
    tgt = (needle or "").strip().lower()
    s = (text or "").strip().lower()
    if not tgt or not s:
        return ("none", 0.0)
    if s == tgt:
        return ("exact", 1.0)
    if len(tgt) < 3:
        return ("none", 0.0)
    if s.startswith(tgt):
        after = s[len(tgt) : len(tgt) + 1]
        if not after or not _is_word_char(after):
            spec = len(tgt) / max(len(s), len(tgt))
            return ("startsWith-word", 0.7 + 0.3 * spec)
    idx = s.find(tgt)
    if idx >= 0:
        before = s[idx - 1] if idx > 0 else ""
        after_idx = idx + len(tgt)
        after = s[after_idx : after_idx + 1]
        if (not before or not _is_word_char(before)) and (
            not after or not _is_word_char(after)
        ):
            spec = len(tgt) / max(len(s), len(tgt))
            return ("contains-word", 0.4 + 0.3 * spec)
    if len(tgt) >= 4:
        d = _levenshtein(s, tgt)
        sim = 1.0 - d / max(len(s), len(tgt))
        if sim >= 0.85:
            return ("fuzzy", sim)
    return ("none", 0.0)


# ---------------------------------------------------------------------------
# Adaptive cascade settle. Polls the page between BrowserFormPlanTool field
# picks until cascade dependencies look ready, instead of waiting a fixed
# 350ms that lands on stale options on slow React-Query cascades and burns
# time we don't need on fast pages.
# ---------------------------------------------------------------------------

_CASCADE_READY_SCRIPT = (
    "(() => {"
    " const busy = document.querySelectorAll('[aria-busy=\"true\"]').length;"
    " let open = 0;"
    " const roles = ['listbox', 'menu', 'dialog'];"
    " for (const role of roles) {"
    "  const els = document.querySelectorAll("
    "   '[role=\"' + role + '\"]:not([aria-hidden=\"true\"])');"
    "  for (const el of els) {"
    "   const cs = window.getComputedStyle(el);"
    "   if (cs.display === 'none' || cs.visibility === 'hidden') continue;"
    "   open += 1; break;"
    "  }"
    " }"
    " return {busy: busy, open: open};"
    "})()"
)


async def _wait_for_cascade_ready(
    session_id: str,
    *,
    max_wait_ms: int = 1500,
    poll_ms: int = 100,
    stable_polls: int = 2,
    fallback_sleep: float = 0.35,
) -> int:
    """Poll the page until cascade dependencies appear settled, then
    return elapsed ms. Signals:

      * No element with ``[aria-busy="true"]`` visible.
      * No visible ``[role=listbox|menu|dialog]`` (the just-picked
        popup should have closed).

    Returns when both signals are clean for ``stable_polls`` consecutive
    polls OR ``max_wait_ms`` is exhausted. Falls back to a fixed
    ``fallback_sleep`` on /evaluate transport error so a polling deadlock
    can't hang the cascade.
    """
    loop = asyncio.get_event_loop()
    started = loop.time()
    deadline = started + max_wait_ms / 1000.0
    clean_streak = 0
    while True:
        try:
            r = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
                json={"script": _CASCADE_READY_SCRIPT},
                timeout=3.0,
            )
            if r.status_code != 200:
                await asyncio.sleep(fallback_sleep)
                return int((loop.time() - started) * 1000)
            result = (r.json() or {}).get("result") or {}
            busy = int(result.get("busy") or 0)
            open_ = int(result.get("open") or 0)
        except Exception:
            await asyncio.sleep(fallback_sleep)
            return int((loop.time() - started) * 1000)

        if busy == 0 and open_ == 0:
            clean_streak += 1
            if clean_streak >= stable_polls:
                return int((loop.time() - started) * 1000)
        else:
            clean_streak = 0

        now = loop.time()
        if now >= deadline:
            return int((now - started) * 1000)
        await asyncio.sleep(poll_ms / 1000.0)


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        index=IntegerSchema(description="Element index of the select/dropdown"),
        value=StringSchema("Option value or visible text to select"),
        required=["session_id", "index", "value"],
    )
)
class BrowserSelectTool(Tool):
    name = "browser_select"
    description = "Select an option in a dropdown by value."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, index: int, value: str, **kw: Any) -> Any:
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/select",
            json={"index": index, "value": value},
            timeout=15.0,
        )
        r.raise_for_status()
        data = r.json()
        # Fetch updated elements after selection (may trigger form changes)
        if not data.get("elements"):
            elements = await _fetch_elements(session_id, self.s)
            if elements:
                data["elements"] = elements
        return self.s.build_text_only(data, f'Selected "{value}" in [{index}]')


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        label=StringSchema(
            "Human-readable label of the dropdown trigger (e.g. 'Brand', "
            "'Processor Brand', 'Year of Release'). Matched via accessible-name "
            "/ <label for=> / aria-labelledby / visible text. Optional when "
            "vision_index is provided — the bbox replaces label-based picking."
        ),
        value=StringSchema(
            "Visible text or value of the option to pick (e.g. 'Dell', 'Intel', "
            "'2017'). Matching priority: exact-ci → startsWith-with-word-boundary "
            "→ contains-with-word-boundary → fuzzy ≥0.85."
        ),
        fuzzy=BooleanSchema(
            description="Allow fuzzy match (Levenshtein ≥0.85). Default true.",
            default=True,
        ),
        timeout=IntegerSchema(
            description="Max ms to wait for listbox/options to render (default 6000).",
            nullable=True,
        ),
        extra_option_selectors=ArraySchema(
            description=(
                "Optional CSS selectors to add to the option-discovery list, "
                "for bespoke widgets that don't expose [role=option]."
            ),
            items=StringSchema(""),
            nullable=True,
        ),
        vision_index=IntegerSchema(
            description=(
                "Optional 1-based vision bbox index (V_n). When set, the "
                "dropdown trigger is resolved by bbox geometry instead of "
                "label-text matching — use this when label resolution returns "
                "ambiguous_trigger. Honors the same vision-freshness / "
                "epoch-age / blocker gates as browser_click_at."
            ),
            nullable=True,
        ),
        in_iframe=StringSchema(
            description=(
                "CSS selector of an <iframe> host. When provided, the "
                "<select> is resolved INSIDE the iframe's contentFrame "
                "instead of the top-level document. Use this when the "
                "target <select> lives inside an embedded frame (quizzes, "
                "calculators, embedded forms). v1 supports NATIVE <select> "
                "only — ARIA combobox/listbox dropdowns inside iframes "
                "should be driven via browser_click_at(vision_index=V_n) "
                "on the trigger; after the menu opens, take a "
                "browser_screenshot so vision labels each menu item and "
                "click the V_n you want."
            ),
            nullable=True,
        ),
        required=["session_id", "value"],
    )
)
class BrowserSelectOptionTool(Tool):
    """Pick a dropdown option by *label* + *value*, hiding DOM-index churn.

    Use this for ANY dropdown/listbox/combobox — native <select>, ARIA
    combobox+listbox, Headless-UI Listbox, etc. You never pass an index or
    vision V-index, so re-renders between cascade steps don't matter.

    On ambiguity (no exact/fuzzy match) the tool returns the candidate list
    instead of guessing — retry with a corrected `value`. For ≥2 dependent
    dropdowns prefer `browser_form_plan` so progress is tracked structurally.
    """

    name = "browser_select_option"
    description = (
        "Pick a dropdown option by label+value. Works on native <select> "
        "AND custom listbox/combobox widgets. Returns {ok, picked_text, "
        "verified, candidates?} — on ambiguity, retry with one of the "
        "candidates instead of clicking blindly. For dropdowns inside "
        "an <iframe>, pass in_iframe='iframe#host' — native <select>s "
        "inside frames work directly (CDP click on a native <select> "
        "does NOT open the dropdown in headless Chromium; this tool "
        "sets the value programmatically + fires `change`)."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        value: str,
        label: str = "",
        fuzzy: bool = True,
        timeout: int | None = None,
        extra_option_selectors: list[str] | None = None,
        vision_index: int | None = None,
        in_iframe: str | None = None,
        **kw: Any,
    ) -> str:
        iframe_note = f" in_iframe={in_iframe!r}" if in_iframe else ""
        if vision_index is not None:
            print(
                f"\n>> browser_select_option(V{vision_index}, "
                f"value={value!r}, label={label!r}{iframe_note})"
            )
        else:
            print(
                f"\n>> browser_select_option(label={label!r}, "
                f"value={value!r}{iframe_note})"
            )
        payload: dict[str, Any] = {
            "label": label or "",
            "value": value,
            "fuzzy": bool(fuzzy),
        }
        if timeout is not None:
            payload["timeout"] = int(timeout)
        if extra_option_selectors:
            payload["extra_option_selectors"] = list(extra_option_selectors)
        if in_iframe:
            payload["in_iframe"] = in_iframe

        # Vision-bbox path. Resolve V_n against the frozen vision epoch
        # (same path browser_click_at uses), apply the same freshness +
        # age + blocker gates, and pass `bbox` + `expected_label` to the
        # TS server. Sidesteps DOM-text ambiguity entirely.
        if vision_index is not None:
            resp = self.s.vision_for_target_resolution()
            if resp is None:
                return (
                    "[select_option_failed:no_vision] No recent vision "
                    "response to resolve vision_index against. Take a "
                    "browser_screenshot first, then retry. Or omit "
                    "vision_index and pass `label` for label-based picking."
                )
            bbox = resp.get_bbox(int(vision_index))
            if bbox is None:
                return (
                    f"[select_option_failed:bad_vision_index] V{vision_index} "
                    f"is out of range (only {len(resp.bboxes)} bboxes in "
                    f"the last vision response). Re-screenshot before retry."
                )
            freshness = getattr(resp, "screenshot_freshness", "fresh")
            if freshness != "fresh":
                return (
                    f"[select_option_failed:stale_vision freshness="
                    f"{freshness}] Vision flagged the last screenshot as "
                    f"not fresh. Call browser_screenshot to refresh and "
                    f"retry."
                )
            try:
                max_age_turns = int(
                    os.environ.get("VISION_MAX_AGE_TURNS") or "1"
                )
            except ValueError:
                max_age_turns = 1
            if max_age_turns > 0:
                age_turns = max(
                    0,
                    self.s._brain_turn_counter - self.s._vision_epoch_turn,
                )
                if age_turns > max_age_turns:
                    return (
                        f"[select_option_failed:epoch_too_old "
                        f"age_turns={age_turns} max={max_age_turns}] "
                        f"V{vision_index} resolves against a vision "
                        f"snapshot older than the safe age window. "
                        f"browser_screenshot, then retry."
                    )
            # Blocker gate.
            scene = getattr(resp, "scene", None)
            active_blocker = (
                getattr(scene, "active_blocker_layer_id", None)
                if scene is not None else None
            )
            if active_blocker:
                bbox_layer = getattr(bbox, "layer_id", None)
                if bbox_layer and bbox_layer != active_blocker:
                    return (
                        f"[select_option_failed:blocker_active "
                        f"layer={active_blocker}] A blocker layer is on "
                        f"top of content; dismiss it before targeting "
                        f"V{vision_index}."
                    )
            iw, ih = resp.image_width, resp.image_height
            if iw <= 0 or ih <= 0:
                return (
                    "[select_option_failed:no_image_dims] Vision "
                    "response has no image dimensions; cannot "
                    "denormalize bbox. Re-screenshot."
                )
            dpr_val = float(getattr(resp, "dpr", 1.0) or 1.0)
            x0, y0, x1, y1 = bbox.to_pixels(iw, ih, dpr=dpr_val)
            payload["bbox"] = {"x0": x0, "y0": y0, "x1": x1, "y1": y1}
            bbox_label = (getattr(bbox, "label", "") or "").strip()
            if bbox_label:
                payload["expected_label"] = bbox_label[:120]
                # Backfill `label` when caller omitted it — useful for
                # diagnostics.
                if not payload["label"]:
                    payload["label"] = bbox_label[:120]

        if not payload["label"] and "bbox" not in payload:
            return (
                "[select_option_failed:bad_args] Provide either `label` "
                "or `vision_index`."
            )

        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/select_option",
            json=payload,
            timeout=20.0,
        )
        try:
            r.raise_for_status()
        except Exception as e:
            self.s.record_step("browser_select_option", f"{label}={value}", f"HTTP error: {e}")
            return f"[select_option_http_error] {e}"
        data = r.json() or {}
        # Auto-refresh element_fingerprints from the response. select_option
        # often mutates the DOM (filter applied, listbox re-rendered),
        # invalidating the Python-side cache. Without this, a follow-up
        # DOM-index click sends a stale fingerprint that may collide. (B6)
        _fp_map = data.get("fingerprints") if isinstance(data, dict) else None
        if isinstance(_fp_map, dict):
            self.s.element_fingerprints = {
                int(k): v for k, v in _fp_map.items() if isinstance(v, str)
            }
        ok = bool(data.get("ok"))
        picked = data.get("picked_text") or value
        verified = bool(data.get("verified"))
        reason = data.get("reason")
        candidates = data.get("candidates") or []
        tried = data.get("tried") or []
        dom_changed = bool(data.get("dom_changed"))
        new_classes = data.get("new_classes") or []
        trigger_score = data.get("trigger_score")

        # Cursor strategy ledger — counts as a real cursor attempt for the
        # cursor-first lockout in browser_run_script.
        try:
            self.s.cursor_failure_strategies  # type: ignore[attr-defined]
        except Exception:
            pass
        else:
            if not ok and reason:
                self.s.cursor_failure_strategies.add(f"select_option:{reason}")

        if ok:
            note = f"Picked '{picked}' for '{label}'" + ("" if verified else " (verify pending)")
            # Nudge against premature page-advance after a bare single
            # pick. Skipped when a form_session is already tracking the
            # cascade (browser_form_plan / browser_form_begin) because
            # the worker hook injects a remaining-fields checklist
            # there. Without a session, the brain often clicks the
            # next Continue/Submit button after one successful pick
            # even when more filter dropdowns are visible on the page.
            if self.s.form_session is None:
                note += (
                    "\n[hint] If this page has more dropdown/filter "
                    "fields visible, fill them BEFORE clicking "
                    "Next/Continue/Submit — single picks often cause "
                    "premature page-advance. For ≥2 cascading "
                    "dropdowns, prefer browser_form_plan(fields=[...]) "
                    "over chained browser_select_option calls."
                )
            print(f"   [select_option] ok -> {picked!r} (verified={verified})")
            self.s.log_activity(f"select_option({label})", picked[:40])
            self.s.record_step("browser_select_option", f"{label}={picked}", "ok")
            return self.s.build_text_only(data, note)

        # Ambiguity / failure path — surface candidates so the LLM corrects
        # the value rather than re-screenshotting and click-looping.
        cand_preview = (
            (" candidates=" + str([c[:30] for c in candidates[:6]]))
            if candidates else ""
        )
        print(f"   [select_option] FAIL reason={reason or '?'}{cand_preview}")

        msg_parts = [f"[select_option_failed] reason={reason or 'unknown'} label={label!r} value={value!r}"]
        # Set when we need to enumerate options + surface to brain
        # (ambiguous or unrecoverable no_match after scroll). Brain gets
        # the full DOM truth and decides which value to retry with.
        _surface_options_to_brain = False
        # Set when we want to enter scroll-and-retry-with-selector flow
        # (no_match cases that might be virtualized lists).
        _try_scroll_and_retry = False
        if reason == "ambiguous_trigger":
            # Multiple candidates with similar scores. The TS picker
            # surfaces them as `candidates` (shape: tag<role>['text']
            # score=N). Brain narrows the label or passes vision_index.
            msg_parts.append(
                "Multiple candidates matched the label with similar "
                "scores — refusing to guess. Either (a) pass a more "
                "specific `label` (e.g. 'Sort by' instead of 'Sort'), "
                "or (b) take a fresh browser_screenshot and call "
                f"browser_select_option(vision_index=V_n, value={value!r}) "
                "with the V_n of the dropdown trigger you actually want."
            )
        elif reason == "ambiguous_option":
            shown = ", ".join(repr(c) for c in candidates[:5])
            # Multiple options matched. Whether they're literal
            # duplicates ('HP','HP') or near-duplicates ('HP Inc.',
            # 'HP Pavilion'), the brain — not the tool — picks. Tool
            # gives the brain DOM truth: every option in the list, so
            # the brain can pass back a more specific value (or pick
            # one positionally via a later browser_click_at).
            msg_parts.append(
                f"Multiple options match {value!r}. Top candidates: "
                f"{shown}. The full option list will appear below as "
                f"[dropdown_options] — pick the exact text you want "
                f"and re-call browser_select_option with that value."
            )
            _surface_options_to_brain = True
        elif reason == "no_option_match":
            # Could be a virtualized list where the option hasn't been
            # rendered yet. Try scrolling the popup and retrying. If
            # the scroll exhausts without finding the value, fall back
            # to enumerating + surfacing the full list to the brain.
            _try_scroll_and_retry = True
        elif reason == "popup_on_navigated_page":
            msg_parts.append(
                "The trigger opened a listbox but the page also "
                "navigated underneath it — the option click would be "
                "applied on the navigated page, not the original one. "
                "Re-screenshot and continue from the new page state, "
                "or use browser_rewind_to_checkpoint to back out."
            )
        elif reason == "bbox_not_a_dropdown":
            msg_parts.append(
                "The bbox you passed via vision_index is not a dropdown "
                "trigger (no aria-haspopup, role=combobox/listbox, "
                "<select>, datalist, or chevron-styled button). Use "
                "browser_click_at(vision_index=V_n) on this bbox "
                "instead, or pass the V_n of a real dropdown trigger."
            )
        elif reason == "option_navigated":
            msg_parts.append(
                "The option click navigated the page. The selection "
                "was NOT applied to the original page state. "
                "Re-screenshot to inspect the new page, or "
                "browser_rewind_to_checkpoint if this was unintentional."
            )
        elif reason == "trigger_disappeared":
            msg_parts.append(
                "The trigger element vanished after the option click "
                "without a navigation event. The page may have "
                "re-rendered with a different layout. Re-screenshot "
                "and re-target."
            )
        elif reason == "trigger_not_found":
            msg_parts.append(
                "The label was not found on this page. Two common causes:\n"
                "  (a) the cascading dropdown stage is over and the page "
                "transitioned to a results grid / model picker — in which "
                "case STOP using browser_form_plan / browser_select_option "
                "and instead browser_get_markdown to inspect the result list, "
                "then browser_click on the matching item.\n"
                "  (b) the label text in the page is different from what "
                "you passed — call browser_screenshot to read actual labels, "
                "then retry with the exact text. Or pass vision_index=V_n "
                "to bypass label resolution entirely."
            )
        elif reason == "trigger_navigated":
            # The trigger turned out to be a link / nav action, not a
            # dropdown opener. Best Buy trade-in 'Brand' picker is the
            # canonical case — each brand is a clickable card that
            # navigates to the next step. Tell the brain to switch
            # tools, don't keep retrying select_option.
            nav_to = ""
            for c in candidates:
                if isinstance(c, str) and c.startswith("navigated_to="):
                    nav_to = c.split("=", 1)[1]
                    break
            msg_parts.append(
                f"The trigger NAVIGATED ({label!r} click changed the "
                f"page to: {nav_to or '?'}). This isn't a dropdown — "
                f"it's a link or card-grid item. STOP retrying "
                f"select_option/form_plan for this label. The page "
                f"is now on a new step. Take a fresh screenshot, "
                f"then click the next target with `browser_click_at` "
                f"(by V_n). For "
                f"card-grid pickers (Brand → Model → ...), each step "
                f"is a navigation, not a dropdown — `browser_click_at` "
                f"on the visible card is the right tool."
            )
        elif reason == "no_popup_detected":
            msg_parts.append(
                f"Click on {label!r} did NOT open any popup/listbox/menu "
                f"(no [role=listbox|menu|dialog] or [aria-expanded=true] "
                f"appeared). The trigger is probably not a dropdown — "
                f"it might be a nav button, a card, or a label without "
                f"an interactive child. Try: (a) `browser_click_at` on "
                f"the visible target, (b) `browser_get_markdown` to "
                f"inspect the page shape, or (c) re-screenshot — the "
                f"page may have transitioned to a different stage."
            )
        if candidates and reason not in ("trigger_navigated",) and not _surface_options_to_brain:
            shown = ", ".join(repr(c) for c in candidates[:15])
            msg_parts.append(f"candidates: {shown}")
            msg_parts.append(
                "Retry browser_select_option with one of the candidates above. "
                "Do NOT fall back to raw clicking — DOM indices change after "
                "each pick; this tool re-anchors on the label."
            )
        elif (
            reason
            and reason not in ("trigger_not_found", "trigger_navigated", "no_popup_detected")
            and not _surface_options_to_brain
        ):
            msg_parts.append(
                "No options were collected. The listbox may use a non-ARIA "
                "pattern — retry with a more specific label, or pass "
                "extra_option_selectors=[...] (e.g. ['li.option', '.dropdown-item'])."
            )

        # Phase D diagnostics — append on EVERY failure path so the
        # brain can pick a different tool instead of looping. Includes
        # which open-strategies were tried and what the DOM did.
        if tried:
            msg_parts.append(f"open_strategies_tried: {', '.join(str(t) for t in tried)}")
        if reason in ("options_did_not_render", "no_popup_detected"):
            if dom_changed and new_classes:
                # Page DID change — but not into a dropdown. Show top
                # new classes so the LLM can recognize "oh that's a
                # card grid / a wizard step / a modal".
                msg_parts.append(
                    "dom_changed=true. New visible classes (top-5): "
                    + ", ".join(str(c) for c in new_classes[:5])
                )
                msg_parts.append(
                    "→ The trigger likely opened/navigated to a "
                    "non-dropdown UI. Re-screenshot and use "
                    "`browser_click_at` on the visible target instead "
                    "of `browser_select_option`."
                )
            elif not dom_changed:
                msg_parts.append(
                    "dom_changed=false — the trigger click did NOTHING. "
                    "Either the picked element wasn't actually the "
                    "dropdown trigger (label-text matched a heading or "
                    "wrapper near the real trigger), OR the trigger is "
                    "disabled. Re-screenshot, then use `browser_click_at` "
                    "on the visible dropdown chevron / control."
                )
        if trigger_score is not None and float(trigger_score) <= 0:
            # Low-confidence trigger pick — flag it. With a 4-phase
            # picker scoring positive on real dropdowns, a non-positive
            # score is itself a smell.
            msg_parts.append(
                f"trigger_score={trigger_score} (low) — the picked "
                "element may not be a real dropdown trigger. Pass a "
                "more specific label (e.g. 'Processor Brand' instead "
                "of 'Brand') or use `browser_click_at` directly."
            )
        self.s.log_activity(f"select_option({label}, FAIL)", (reason or "")[:60])
        self.s.record_step("browser_select_option", f"{label}={value}", f"FAIL:{reason or '?'}")

        # =================================================================
        # SUPER-INTELLIGENT RECOVERY. Two paths, both rooted in DOM truth:
        #
        # 1) `no_option_match` (value text not found in DOM at popup
        #    open): the popup might be virtualized — only ~20 rendered
        #    items are in DOM at a time. Scroll-and-retry up to N
        #    iterations: each iteration scrolls the popup one page,
        #    re-enumerates options, and tries to find the value. As
        #    soon as a single match is found, click it directly via
        #    /click-selector (FAST PATH — no further scroll-into-view
        #    needed; selector clicks work on off-screen DOM elements).
        #    If scroll exhausts without a match, enumerate every visible
        #    option and surface the list to the brain — brain retries
        #    with a closer value.
        #
        # 2) `ambiguous_option` (value text matched 2+ options): the
        #    tool does NOT auto-pick. It enumerates ALL options and
        #    returns them to the brain so the brain decides which
        #    specific text to re-call with (or picks visually via
        #    browser_click_at).
        # =================================================================

        async def _enumerate_popup_options() -> list[dict]:
            """Eval-based enumeration of all option-like elements
            inside any visible popup.
            Returns list of {text, in_viewport} dicts, cap 80."""
            script = (
                "(() => {"
                " const sels = ["
                "  'option',"
                "  '[role=\"option\"]:not([aria-hidden=\"true\"])',"
                "  '[role=\"menuitem\"]:not([aria-hidden=\"true\"])',"
                "  '[role=\"menuitemcheckbox\"]:not([aria-hidden=\"true\"])',"
                "  '[role=\"menuitemradio\"]:not([aria-hidden=\"true\"])'"
                " ];"
                " const seen = new Set();"
                " const items = [];"
                " for (const sel of sels) {"
                "  for (const el of document.querySelectorAll(sel)) {"
                "   if (seen.has(el)) continue; seen.add(el);"
                "   const cs = window.getComputedStyle(el);"
                "   if (cs.display === 'none' || cs.visibility === 'hidden') continue;"
                "   const r = el.getBoundingClientRect();"
                "   const t = (el.innerText || el.textContent || '').trim();"
                "   if (!t) continue;"
                "   if (t.length > 200) continue;"
                "   items.push({"
                "    text: t.slice(0, 100),"
                "    in_viewport: r.top >= 0 && r.bottom <= (window.innerHeight || 0),"
                "   });"
                "   if (items.length >= 80) break;"
                "  }"
                "  if (items.length >= 80) break;"
                " }"
                " return items;"
                "})()"
            )
            try:
                er = await _request_with_backoff(
                    "POST",
                    f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
                    json={"script": script},
                    timeout=6.0,
                )
                if er.status_code != 200:
                    return []
                body = er.json() or {}
                out = body.get("result") or []
                return out if isinstance(out, list) else []
            except Exception as exc:
                print(f"   [select_option:enumerate failed] {exc}")
                return []

        async def _click_option_by_text_via_selector(needle: str) -> dict[str, Any]:
            """Fast-path click with 4-tier scored matching, mirroring
            ``selectOptionByLabel``'s ``pickAttempt`` in
            src/browser/elements.ts:1416-1594.

            Collects ALL option-like elements (not just the first DOM-order
            hit), runs the 4 tiers (exact-ci → startsWith-word →
            contains-word → fuzzy≥0.85) with shortest-text tiebreak, tags
            the unique winner with ``data-sb-opt-recovery``, and clicks it
            via /click-selector. When ≥2 candidates tie at the same tier,
            returns ``ambiguous=True`` instead of guessing.

            Returns a dict with:
              ``ok``         — winning option was tagged AND clicked
              ``ambiguous``  — multiple options matched at the same tier
              ``tag``        — data-sb-opt-recovery value (on ok=True)
              ``picked_text``— visible text of the picked option
              ``tier``       — 'exact' | 'startsWith-word' |
                               'contains-word' | 'fuzzy' | 'none'
              ``score``      — roughly comparable across tiers
              ``candidates`` — tied texts (ambiguous) or top-N visible
                               (no_option_match)
              ``reason``     — diagnostic code
            """
            tag = f"sb-opt-recov-{secrets.token_hex(4)}"
            # 4-tier scored matcher. Same tier order, word-boundary
            # checks, shortest-text tiebreak and fuzzy floor as the
            # TS picker.
            match_script = (
                "(() => {"
                " const v = " + repr(needle.strip().lower()) + ";"
                " const tag = " + repr(tag) + ";"
                " const sels = ['option', '[role=\"option\"]', "
                "'[role=\"menuitem\"]', '[role=\"menuitemcheckbox\"]', "
                "'[role=\"menuitemradio\"]'];"
                " const seen = new Set();"
                " const items = [];"
                " for (const sel of sels) {"
                "  for (const el of document.querySelectorAll(sel)) {"
                "   if (seen.has(el)) continue; seen.add(el);"
                "   const cs = window.getComputedStyle(el);"
                "   if (cs.display === 'none' || cs.visibility === 'hidden') continue;"
                "   const t = (el.innerText || el.textContent || '').trim();"
                "   if (!t || t.length > 200) continue;"
                "   items.push({el: el, txt: t, lower: t.toLowerCase()});"
                "  }"
                " }"
                " if (items.length === 0) {"
                "  return {ok:false, ambiguous:false, tier:'none',"
                "          candidates:[], reason:'no_options_collected'};"
                " }"
                " if (!v) {"
                "  return {ok:false, ambiguous:false, tier:'none',"
                "          candidates: items.slice(0,25).map(it => it.txt),"
                "          reason:'no_target'};"
                " }"
                " const isWord = (c) => c != null && /[\\p{L}\\p{N}_]/u.test(c);"
                " const wordBoundary = (s, frag) => {"
                "  if (!frag) return false;"
                "  const i = s.indexOf(frag);"
                "  if (i < 0) return false;"
                "  return !isWord(s[i-1]) && !isWord(s[i+frag.length]);"
                " };"
                " const startsWithBoundary = (s) => {"
                "  if (!s.startsWith(v)) return false;"
                "  return !isWord(s[v.length]);"
                " };"
                " const tagWin = (it, tier, score) => {"
                "  it.el.setAttribute('data-sb-opt-recovery', tag);"
                "  return {ok:true, ambiguous:false, tag: tag,"
                "          picked_text: it.txt, tier: tier, score: score,"
                "          candidates: []};"
                " };"
                # Tier 1: exact-ci. DOM duplicates (multiple elements
                # with identical text — e.g. Best Buy mobile+desktop
                # filter variants visible at once) collapse to one and
                # are picked; mirrors the TS picker at elements.ts.
                " const exact = items.filter(it => it.lower === v);"
                " if (exact.length === 1) return tagWin(exact[0], 'exact', 1.0);"
                " if (exact.length > 1) {"
                "  const uniqExact = new Set(exact.map(it => it.txt));"
                "  if (uniqExact.size === 1) return tagWin(exact[0], 'exact', 1.0);"
                "  return {ok:false, ambiguous:true, tier:'exact',"
                "          candidates: exact.slice(0,10).map(it => it.txt),"
                "          reason:'ambiguous_at_exact'};"
                " }"
                " if (v.length < 3) {"
                "  return {ok:false, ambiguous:false, tier:'none',"
                "          candidates: items.slice(0,25).map(it => it.txt),"
                "          reason:'target_too_short_for_partial'};"
                " }"
                # Tier 2: startsWith-word. Tied-length DOM duplicates
                # collapse to one and are picked.
                " const starts = items.filter(it => startsWithBoundary(it.lower));"
                " if (starts.length >= 1) {"
                "  starts.sort((a,b) => a.txt.length - b.txt.length);"
                "  if (starts.length > 1 && starts[0].txt.length === starts[1].txt.length) {"
                "   const tied = starts.filter(h => h.txt.length === starts[0].txt.length);"
                "   const uniqTied = new Set(tied.map(it => it.txt));"
                "   if (uniqTied.size !== 1) {"
                "    return {ok:false, ambiguous:true, tier:'startsWith-word',"
                "            candidates: starts.slice(0,10).map(it => it.txt),"
                "            reason:'ambiguous_at_startsWith-word'};"
                "   }"
                "  }"
                "  const sc = 0.7 + 0.3 * (v.length / Math.max(starts[0].txt.length, v.length));"
                "  return tagWin(starts[0], 'startsWith-word', sc);"
                " }"
                # Tier 3: contains-word. Same DOM-duplicate handling.
                " const contains = items.filter(it => wordBoundary(it.lower, v));"
                " if (contains.length >= 1) {"
                "  contains.sort((a,b) => a.txt.length - b.txt.length);"
                "  if (contains.length > 1 && contains[0].txt.length === contains[1].txt.length) {"
                "   const tied = contains.filter(h => h.txt.length === contains[0].txt.length);"
                "   const uniqTied = new Set(tied.map(it => it.txt));"
                "   if (uniqTied.size !== 1) {"
                "    return {ok:false, ambiguous:true, tier:'contains-word',"
                "            candidates: contains.slice(0,10).map(it => it.txt),"
                "            reason:'ambiguous_at_contains-word'};"
                "   }"
                "  }"
                "  const sc = 0.4 + 0.3 * (v.length / Math.max(contains[0].txt.length, v.length));"
                "  return tagWin(contains[0], 'contains-word', sc);"
                " }"
                # Tier 4: fuzzy (Levenshtein ≥ 0.85, target length ≥ 4)
                " if (v.length >= 4) {"
                "  const lev = (a, b) => {"
                "   if (a === b) return 0;"
                "   const m = a.length, n = b.length;"
                "   if (m === 0) return n; if (n === 0) return m;"
                "   const dp = Array.from({length: m+1}, () => new Array(n+1).fill(0));"
                "   for (let i = 0; i <= m; i++) dp[i][0] = i;"
                "   for (let j = 0; j <= n; j++) dp[0][j] = j;"
                "   for (let i = 1; i <= m; i++) {"
                "    for (let j = 1; j <= n; j++) {"
                "     const c = a[i-1] === b[j-1] ? 0 : 1;"
                "     dp[i][j] = Math.min(dp[i-1][j]+1, dp[i][j-1]+1, dp[i-1][j-1]+c);"
                "    }"
                "   }"
                "   return dp[m][n];"
                "  };"
                "  let best = null, runnerUp = null;"
                "  for (const it of items) {"
                "   if (!it.lower) continue;"
                "   const d = lev(it.lower, v);"
                "   const sim = 1 - d / Math.max(it.lower.length, v.length);"
                "   if (sim >= 0.85) {"
                "    if (!best || sim > best.score) {"
                "     runnerUp = best;"
                "     best = {it: it, score: sim};"
                "    } else if (!runnerUp || sim > runnerUp.score) {"
                "     runnerUp = {it: it, score: sim};"
                "    }"
                "   }"
                "  }"
                "  if (best) {"
                "   if (runnerUp && (best.score - runnerUp.score) < 0.05) {"
                "    return {ok:false, ambiguous:true, tier:'fuzzy',"
                "            candidates: [best.it.txt, runnerUp.it.txt],"
                "            reason:'ambiguous_at_fuzzy'};"
                "   }"
                "   return tagWin(best.it, 'fuzzy', best.score);"
                "  }"
                " }"
                " return {ok:false, ambiguous:false, tier:'none',"
                "         candidates: items.slice(0,25).map(it => it.txt),"
                "         reason:'no_option_match'};"
                "})()"
            )

            fail: dict[str, Any] = {
                "ok": False, "ambiguous": False, "tag": "", "picked_text": "",
                "tier": "none", "score": 0.0, "candidates": [],
                "reason": "eval_failed",
            }
            try:
                tr = await _request_with_backoff(
                    "POST",
                    f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
                    json={"script": match_script},
                    timeout=5.0,
                )
                if tr.status_code != 200:
                    return fail
                result = (tr.json() or {}).get("result")
                if not isinstance(result, dict):
                    return fail
                out: dict[str, Any] = {
                    "ok": bool(result.get("ok")),
                    "ambiguous": bool(result.get("ambiguous")),
                    "tag": str(result.get("tag") or ""),
                    "picked_text": str(result.get("picked_text") or ""),
                    "tier": str(result.get("tier") or "none"),
                    "score": float(result.get("score") or 0.0),
                    "candidates": [
                        str(c) for c in (result.get("candidates") or []) if c
                    ],
                    "reason": str(result.get("reason") or ""),
                }
                if not out["ok"]:
                    return out
                # Unique winner — click the tagged element.
                cr = await _request_with_backoff(
                    "POST",
                    f"{SUPERBROWSER_URL}/session/{session_id}/click-selector",
                    json={"selector": f"[data-sb-opt-recovery=\"{out['tag']}\"]"},
                    timeout=10.0,
                )
                if cr.status_code != 200:
                    out["ok"] = False
                    out["reason"] = "click_http_error"
                    return out
                body = cr.json() or {}
                if not bool(body.get("success")):
                    out["ok"] = False
                    out["reason"] = "click_failed"
                return out
            except Exception as exc:
                print(f"   [select_option:selector_click failed] {exc}")
                fail["reason"] = f"exception:{type(exc).__name__}"
                return fail

        def _format_options_block(
            options: list[dict],
            header: str,
            *,
            needle: str = "",
        ) -> str:
            """Render an enumerated option list as a clean text block,
            with viewport hints so the brain can correlate with what it
            sees on screen. When ``needle`` is provided, annotate each
            row with its match tier + score so the brain can see WHY
            its requested value didn't pick cleanly (e.g. score=0.78
            contains-word, below the 0.85 fuzzy floor)."""
            if not options:
                return f"[{header} count=0] (no options enumerated — popup may be closed or non-standard)"
            shown = options[:80]
            truncated = len(options) > 80
            rows = []
            for o in shown:
                vis = "✓" if o.get("in_viewport") else " "
                txt = (o.get("text") or "")
                row = f"  {vis} {txt!r}"
                if needle:
                    tier, score = _score_option_text(needle, txt)
                    if tier != "none":
                        row += f"  ({tier} {score:.2f})"
                rows.append(row)
            tail = f"\n  ... (showing 80 of {len(options)}+)" if truncated else ""
            header_label = (
                f"{header} count={len(shown)}{('+' if truncated else '')}"
            )
            if needle:
                header_label += f" — scored against {needle!r}"
            return f"[{header_label}]\n" + "\n".join(rows) + tail

        if _try_scroll_and_retry:
            # no_option_match path: scroll-and-retry with selector
            # fast-path. Up to 5 iterations. The fast-path matcher
            # may surface ambiguity mid-scroll — in that case stop
            # scrolling (the option IS in DOM, we just need a more
            # specific value) and hand candidates back to the brain.
            MAX_ITERS = 5
            picked = False
            ambiguous_recov: dict[str, Any] | None = None

            def _picked_msg(recov: dict[str, Any], suffix: str) -> str:
                return (
                    f"Picked {recov.get('picked_text') or value!r} for "
                    f"{label!r} via scroll-recovery {suffix} "
                    f"(tier={recov.get('tier', '?')}, "
                    f"score={float(recov.get('score') or 0.0):.2f})."
                )

            try:
                for iteration in range(MAX_ITERS):
                    recov = await _click_option_by_text_via_selector(value)
                    if recov["ok"]:
                        msg_parts = [_picked_msg(recov, f"iter={iteration + 1}")]
                        # The popup closed (option clicked) — clear the
                        # scroll guard so unrelated DOM-index clicks
                        # afterwards aren't blocked. (TS picker relies
                        # on this for the next browser_click_at.)
                        try:
                            self.s.popup_scroll_pending = False
                            self.s.popup_scroll_at = 0.0
                        except Exception:
                            pass
                        picked = True
                        break

                    if recov["ambiguous"]:
                        ambiguous_recov = recov
                        print(
                            f"   [select_option:scroll_retry iter={iteration + 1}]"
                            f" ambiguous at tier={recov['tier']} —"
                            f" {len(recov['candidates'])} candidates;"
                            f" stopping scroll, surfacing to brain"
                        )
                        break

                    # No match — scroll the popup and try again.
                    try:
                        sw_r = await _request_with_backoff(
                            "POST",
                            f"{SUPERBROWSER_URL}/session/{session_id}/scroll-within",
                            json={"direction": "down", "amount": "page"},
                            timeout=10.0,
                        )
                        sw_outcome = ((sw_r.json() or {}).get("outcome") or {}) if sw_r.status_code == 200 else {}
                        sw_px = sw_outcome.get("scrolledPx", 0)
                        sw_reason = sw_outcome.get("reason", "")
                        print(
                            f"   [select_option:scroll_retry iter={iteration + 1}]"
                            f" scrolledPx={sw_px} reason={sw_reason}"
                        )
                        if sw_px > 0:
                            try:
                                self.s.flag_popup_scroll(reason="select_option_scroll_retry")
                            except Exception:
                                pass
                        # Stop conditions: no scroll progress.
                        if sw_reason in ("no_container", "page_end") or sw_px == 0:
                            # One more match attempt in case the value
                            # is now in DOM (last page of a virtualized
                            # list). Honor ambiguity here too.
                            final = await _click_option_by_text_via_selector(value)
                            if final["ok"]:
                                msg_parts = [_picked_msg(
                                    final,
                                    "(final attempt after scroll exhausted)",
                                )]
                                try:
                                    self.s.popup_scroll_pending = False
                                    self.s.popup_scroll_at = 0.0
                                except Exception:
                                    pass
                                picked = True
                            elif final["ambiguous"]:
                                ambiguous_recov = final
                            break
                    except Exception as sw_exc:
                        print(f"   [select_option:scroll iter={iteration + 1} failed] {sw_exc}")
                        break
            except Exception as recov_exc:
                print(f"   [select_option:scroll_recovery error] {recov_exc}")

            if picked:
                self.s.log_activity(f"select_option({label})", f"{value} (scroll-recovered)")
                self.s.record_step(
                    "browser_select_option",
                    f"{label}={value}",
                    "ok (scroll-recovered)",
                )
                return "\n".join(msg_parts)

            # Either ambiguity was surfaced mid-scroll OR scroll
            # exhausted without finding the option. Either way, hand
            # the brain the full option list — but lead with the
            # ambiguity-specific message when applicable so the brain
            # knows which tier the conflict was at.
            if ambiguous_recov is not None:
                cand_repr = ", ".join(
                    repr(c) for c in ambiguous_recov["candidates"][:10]
                )
                msg_parts.append(
                    f"[scroll_recovery_ambiguous tier="
                    f"{ambiguous_recov['tier']}] {value!r} matched "
                    f"multiple options at this tier: {cand_repr}. "
                    f"Re-call browser_select_option with one of these "
                    f"exact strings."
                )
            _surface_options_to_brain = True

        if _surface_options_to_brain:
            try:
                opts = await _enumerate_popup_options()
                if opts:
                    msg_parts.append(
                        _format_options_block(
                            opts, "dropdown_options", needle=value,
                        )
                    )
                    msg_parts.append(
                        f"Re-call browser_select_option(label={label!r}, "
                        f"value=<exact text from above>) to pick. ✓ = currently "
                        f"in viewport;  = scrolled-off (still clickable via "
                        f"this tool — selector fast-path handles it). The "
                        f"(tier score) annotation shows how each option "
                        f"matched against {value!r} — pick the one closest "
                        f"to your intent."
                    )
                else:
                    msg_parts.append(
                        "[dropdown_options count=0] Could not enumerate "
                        "options — the popup may have closed or uses a "
                        "non-standard pattern. Call browser_screenshot "
                        "and inspect manually."
                    )
            except Exception as exc:
                print(f"   [select_option:enumerate_for_brain failed] {exc}")

        return "\n".join(msg_parts)


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        intent=StringSchema(
            "Short description of what this filter form is for "
            "(e.g. 'Best Buy laptop trade-in valuation')."
        ),
        fields=ArraySchema(
            description=(
                "Ordered list of dropdowns to fill. Each entry: "
                "{label, value, kind?}. Order is the cascade order — later "
                "fields are filled only after earlier ones succeed. Use "
                "the *visible label text* (e.g. 'Brand', 'Processor Brand') "
                "and the *visible option text* (e.g. 'Dell', 'Intel')."
            ),
            items=ObjectSchema(
                label=StringSchema("Visible label text of the dropdown"),
                value=StringSchema("Visible option text to pick"),
                kind=StringSchema(
                    "Optional: 'select' | 'cascade_select' (default).",
                    nullable=True,
                ),
                required=["label", "value"],
            ),
        ),
        per_step_timeout=IntegerSchema(
            description="Per-field listbox-render timeout (ms, default 4000).",
            nullable=True,
        ),
        stop_on_failure=BooleanSchema(
            description=(
                "If true (default), stop and return on first failed field "
                "with the candidate list. If false, continue past failures "
                "to fill what's possible."
            ),
            default=True,
        ),
        scout=BooleanSchema(
            description=(
                "If true, perform a PRE-FLIGHT SCOUT instead of filling the "
                "form: open each dropdown in order, harvest its visible "
                "option strings, close it, and return a menu schema to the "
                "brain. No values are committed. Use this when you don't "
                "know the exact option text for one or more fields and "
                "want to avoid the 'commit blind → fail → retry' loop. "
                "Stops at the first field whose dropdown isn't available "
                "yet (depends on a prior pick). After scouting, re-call "
                "with scout=false and exact `value` strings from the "
                "schema. `value` may be empty for fields you're scouting. "
                "Default false."
            ),
            default=False,
        ),
        required=["session_id", "intent", "fields"],
    )
)
class BrowserFormPlanTool(Tool):
    """Plan + execute a cascading filter form in one tool call.

    The LLM declares the *whole* form once — Brand=Dell, Processor=Intel,
    RAM=8GB, Year=2017, etc. — and the runtime fills each field by
    label-anchored selection (browser_select_option), settling between
    steps so the next dropdown's options can populate. Removes the
    "stale V-index, regress, retry" loop on multi-step filter forms.

    Returns a structured progress string. On per-field failure (no match,
    listbox didn't render) the tool surfaces the candidate list so the
    LLM can retry with a corrected value.
    """

    name = "browser_form_plan"
    description = (
        "Fill a cascading filter form (≥2 dependent dropdowns) in one "
        "call. Pass an ordered list of {label, value} pairs. The runtime "
        "label-anchors each pick (no DOM index, no V-index) and settles "
        "between steps. Strongly preferred over manual click-loops on "
        "trade-in / search-filter / quote forms."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        intent: str,
        fields: list[dict[str, Any]],
        per_step_timeout: int | None = None,
        stop_on_failure: bool = True,
        scout: bool = False,
        **kw: Any,
    ) -> str:
        if not isinstance(fields, list) or not fields:
            return "[form_plan_failed] `fields` must be a non-empty list."
        # Defensive: coerce to plain dicts; reject malformed entries early.
        # In scout mode, only `label` is required — `value` may be empty
        # since the brain is asking us to discover option strings first.
        clean: list[dict[str, Any]] = []
        for i, f in enumerate(fields):
            if not isinstance(f, dict):
                return f"[form_plan_failed] fields[{i}] is not a dict."
            label = (f.get("label") or "").strip()
            value = (f.get("value") or "").strip()
            if not label:
                return (
                    f"[form_plan_failed] fields[{i}] missing label: {f!r}"
                )
            if not value and not scout:
                return (
                    f"[form_plan_failed] fields[{i}] missing value (only "
                    f"allowed when scout=True): {f!r}"
                )
            clean.append({"label": label, "value": value, "kind": (f.get("kind") or "cascade_select")})

        if scout:
            return await self._scout_fields(
                session_id, intent, clean,
                per_step_timeout=per_step_timeout,
            )

        try:
            from superbrowser_bridge.form_session import FormFillSession, FieldStatus
        except ImportError as exc:
            return f"[form_plan_failed:import] {exc}"

        sess = FormFillSession.begin_cascade(
            intent=intent, fields=clean,
            started_at_turn=self.s._brain_turn_counter,
        )
        self.s.form_session = sess

        print(f"\n>> browser_form_plan({len(clean)} fields)")
        progress: list[str] = [
            f"[form_plan] intent={intent!r} planning {len(clean)} fields"
        ]
        failures: list[str] = []

        # Best-effort: close any open dropdown / modal before we start.
        # If the previous tool call left a listbox/menu open, the next
        # trigger lookup will land inside the overlay rather than on the
        # field's own combobox button.
        try:
            await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/keys",
                json={"keys": "Escape"},
                timeout=5.0,
            )
            await asyncio.sleep(0.2)
        except Exception:
            pass

        for entry in clean:
            label = entry["label"]
            value = entry["value"]
            payload = {"label": label, "value": value, "fuzzy": True}
            if per_step_timeout is not None:
                payload["timeout"] = int(per_step_timeout)
            print(f"   [form_plan] -> {label}={value!r}")
            r = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/select_option",
                json=payload,
                timeout=20.0,
            )
            try:
                r.raise_for_status()
            except Exception as e:
                failures.append(f"{label}={value} → HTTP error: {e}")
                if stop_on_failure:
                    break
                continue
            data = r.json() or {}
            ok = bool(data.get("ok"))
            picked = data.get("picked_text") or value
            reason = data.get("reason")
            candidates = data.get("candidates") or []
            auto_retry_note = ""

            # One-shot auto-retry on ambiguous_option. Score each
            # returned candidate against the brain's value; if a clear
            # winner exists (top score >= 0.40 AND beats runner-up by
            # >= 0.05), retry once with that candidate. Strictly
            # gated on `ambiguous_option` — NEVER on `ambiguous_trigger`
            # (label-level ambiguity is a footgun for auto-retry).
            if (
                not ok
                and reason == "ambiguous_option"
                and isinstance(candidates, list)
                and candidates
            ):
                scored = [
                    (_score_option_text(value, c), c)
                    for c in candidates
                    if isinstance(c, str) and c
                ]
                scored.sort(key=lambda x: x[0][1], reverse=True)
                if scored:
                    (best_tier, best_score), best_value = scored[0]
                    runner_up = scored[1][0][1] if len(scored) > 1 else 0.0
                    clear_winner = (
                        best_tier != "none"
                        and best_score >= 0.40
                        and (best_score - runner_up) >= 0.05
                    )
                    if clear_winner:
                        print(
                            f"   [form_plan]   auto_retry: {label} "
                            f"{value!r} → {best_value!r} "
                            f"(tier={best_tier}, score={best_score:.2f})"
                        )
                        retry_payload = dict(payload)
                        retry_payload["value"] = best_value
                        try:
                            r2 = await _request_with_backoff(
                                "POST",
                                f"{SUPERBROWSER_URL}/session/{session_id}/select_option",
                                json=retry_payload,
                                timeout=20.0,
                            )
                            r2.raise_for_status()
                            data = r2.json() or {}
                            ok = bool(data.get("ok"))
                            picked = data.get("picked_text") or best_value
                            reason = data.get("reason")
                            candidates = data.get("candidates") or []
                            if ok:
                                auto_retry_note = (
                                    f" (auto-retried from {value!r})"
                                )
                                value = best_value
                            else:
                                print(
                                    f"   [form_plan]   auto_retry FAILED "
                                    f"reason={reason or '?'}"
                                )
                        except Exception as exc:
                            print(
                                f"   [form_plan]   auto_retry error: {exc}"
                            )

            if ok:
                sess.mark_picked(label, picked)
                progress.append(f"  [+] {label} = {picked!r}{auto_retry_note}")
                print(f"   [form_plan]   ok -> picked {picked!r}{auto_retry_note}")
                # Adaptive settle so the dependent dropdown's options
                # can populate before the next iteration. Polls until
                # aria-busy clears AND no listbox/menu/dialog is open
                # for two consecutive polls, up to 1500ms. Falls back
                # to a fixed 0.35s on /evaluate error.
                elapsed_ms = await _wait_for_cascade_ready(session_id)
                print(f"   [form_plan]   cascade_settled in {elapsed_ms}ms")
                # And close any lingering listbox before the next trigger lookup.
                try:
                    await _request_with_backoff(
                        "POST",
                        f"{SUPERBROWSER_URL}/session/{session_id}/keys",
                        json={"keys": "Escape"},
                        timeout=5.0,
                    )
                    await asyncio.sleep(0.15)
                except Exception:
                    pass
            else:
                cand_str = ""
                if candidates:
                    cand_str = (
                        " candidates=" + ", ".join(repr(c) for c in candidates[:10])
                    )
                msg = f"{label}={value} → {reason or 'no_match'}{cand_str}"
                failures.append(msg)
                progress.append(f"  [!] {msg}")
                print(f"   [form_plan]   FAIL -> {reason or '?'} {('cands=' + str(candidates[:6])) if candidates else ''}")
                if stop_on_failure:
                    break

        # Final progress summary
        progress.append("")
        progress.append(sess.cascade_progress())
        if failures and stop_on_failure:
            progress.append(
                "Stopped on first failure. Retry browser_form_plan with "
                "corrected values for the remaining fields, OR retry just "
                "the failed field with browser_select_option using one of "
                "the listed candidates."
            )
        elif failures:
            progress.append(
                f"Continued past {len(failures)} failure(s). Use "
                "browser_select_option to retry each."
            )

        self.s.log_activity(
            f"form_plan({intent[:30]})",
            f"verified {sum(1 for fs in sess.fields.values() if fs.status == FieldStatus.VERIFIED)}/{len(sess.fields)}",
        )
        return "\n".join(progress)

    async def _scout_fields(
        self,
        session_id: str,
        intent: str,
        clean: list[dict[str, Any]],
        *,
        per_step_timeout: int | None,
    ) -> str:
        """Open each dropdown in order, harvest its visible option list,
        close it, settle, then move on. Return the menu schema to the
        brain without committing any pick.

        Stops at the first field whose trigger isn't available — that's
        usually a sign the field depends on a prior pick, and we can't
        scout deeper without committing. The brain re-calls with
        ``scout=False`` and the picked values to run the real cascade,
        then can re-scout downstream fields after each pick lands.

        Implementation note: an empty ``value`` to /select_option already
        triggers the picker's ``no_target`` branch, which returns the
        first ~25 enumerated options as ``candidates``. No new server
        contract needed.
        """
        print(f"\n>> browser_form_plan(scout, {len(clean)} fields)")
        schemas: list[dict[str, Any]] = []
        for entry in clean:
            label = entry["label"]
            payload = {"label": label, "value": "", "fuzzy": False}
            if per_step_timeout is not None:
                payload["timeout"] = int(per_step_timeout)
            print(f"   [form_plan:scout] -> {label}")
            try:
                r = await _request_with_backoff(
                    "POST",
                    f"{SUPERBROWSER_URL}/session/{session_id}/select_option",
                    json=payload,
                    timeout=20.0,
                )
                r.raise_for_status()
            except Exception as exc:
                schemas.append({
                    "label": label, "options": [], "status": "http_error",
                    "note": str(exc)[:160],
                })
                break
            data = r.json() or {}
            candidates = [
                str(c) for c in (data.get("candidates") or [])
                if isinstance(c, str) and c
            ]
            reason = data.get("reason") or ""
            unavailable_reasons = (
                "trigger_not_found", "trigger_disappeared",
                "trigger_navigated", "no_popup_detected",
                "popup_on_navigated_page",
            )
            if reason in unavailable_reasons:
                schemas.append({
                    "label": label, "options": candidates,
                    "status": "not_available", "reason": reason,
                })
                print(
                    f"   [form_plan:scout]   {label}: not_available "
                    f"({reason}); stopping scout"
                )
                break
            schemas.append({
                "label": label, "options": candidates,
                "status": "ok" if candidates else "empty",
                "reason": reason,
            })
            # Close any open listbox; settle briefly.
            try:
                await _request_with_backoff(
                    "POST",
                    f"{SUPERBROWSER_URL}/session/{session_id}/keys",
                    json={"keys": "Escape"},
                    timeout=5.0,
                )
            except Exception:
                pass
            await _wait_for_cascade_ready(
                session_id, max_wait_ms=600, fallback_sleep=0.2,
            )

        # Render schema back to brain.
        scouted = sum(1 for s in schemas if s["status"] == "ok")
        out: list[str] = [
            f"[form_scout intent={intent!r} scouted_ok={scouted}/{len(clean)}]"
        ]
        for s in schemas:
            label = s["label"]
            status = s["status"]
            if status == "ok":
                opts = s["options"]
                preview = ", ".join(repr(o) for o in opts[:15])
                more = (
                    f", … +{len(opts) - 15} more"
                    if len(opts) > 15 else ""
                )
                out.append(f"  {label}: [{preview}{more}]")
            elif status == "empty":
                out.append(
                    f"  {label}: (popup opened but no options enumerated)"
                )
            elif status == "not_available":
                out.append(
                    f"  {label}: <not available yet — reason="
                    f"{s.get('reason') or '?'}; usually depends on a "
                    f"prior pick>"
                )
            else:
                out.append(
                    f"  {label}: <error: {s.get('note') or '?'}>"
                )
        # Append fields we never reached (scout stopped early).
        scouted_labels = {s["label"] for s in schemas}
        for entry in clean:
            if entry["label"] not in scouted_labels:
                out.append(
                    f"  {entry['label']}: <not scouted — stopped after "
                    f"earlier field>"
                )
        out.append("")
        out.append(
            "Re-call browser_form_plan(scout=false, fields=[...]) with "
            "`value` set to an exact option string from above for each "
            "field. Fields shown as <not available yet> need a prior "
            "pick to commit first — you can scout them again in a "
            "later call after the cascade has reached them."
        )
        self.s.log_activity(
            f"form_plan:scout({intent[:30]})",
            f"scouted {scouted}/{len(clean)}",
        )
        return "\n".join(out)


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        intent=StringSchema(
            "What this form does — e.g. 'apartment search filters', "
            "'flight booking', 'signup'. Used by the worker hook to "
            "phrase the per-turn checklist."
        ),
        fields=ArraySchema(
            description=(
                "Ordered list of fields to fill. Each entry is an object "
                "with `label` (human-readable name to match against vision "
                "bboxes), `value` (target text to type), and optional "
                "`autocomplete` (true if this field opens a suggestions "
                "overlay that must be picked from)."
            ),
            items=ObjectSchema(
                label=StringSchema("Field name shown to the user"),
                value=StringSchema("Value to type"),
                autocomplete=BooleanSchema(
                    description="Whether this field opens an autocomplete dropdown",
                    nullable=True,
                ),
                required=["label", "value"],
            ),
        ),
        submit_label=StringSchema(
            "Optional label of the submit button (e.g. 'Search'). "
            "If provided, browser_form_commit will look for it in vision.",
            nullable=True,
        ),
        required=["session_id", "intent", "fields"],
    )
)
class BrowserFormBeginTool(Tool):
    """Phase 2.1: open a form-fill session.

    Tracks pending/filled/verified state for each declared field. While
    a session is active the worker hook injects a remaining-fields
    checklist into every tool result, and browser_form_commit refuses
    to dispatch the submit click until every field's typed value is
    visible on the page.

    Use this on dense filter/booking/signup forms where the brain
    routinely loses track of fields below an autocomplete dropdown.
    """

    name = "browser_form_begin"
    description = (
        "Open a tracked form-fill session. After calling, fill each "
        "field with browser_type_at — the session tracks progress and "
        "warns when a field is missed. Conclude with browser_form_commit "
        "to verify all values stuck before submitting."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        intent: str,
        fields: list[dict[str, Any]],
        submit_label: str | None = None,
        **kw: Any,
    ) -> str:
        if os.environ.get("FORM_SESSION_ENABLED", "1") in ("0", "false", "no"):
            return (
                "[form_begin_disabled] FORM_SESSION_ENABLED=0 — fall "
                "back to ad-hoc filling. Track remaining fields yourself."
            )
        if not isinstance(fields, list) or not fields:
            return "[form_begin_failed] `fields` must be a non-empty list."
        try:
            from superbrowser_bridge.form_session import FormFillSession
        except ImportError as exc:
            return f"[form_begin_failed:import] {exc}"
        sess = FormFillSession.begin(
            intent=intent,
            fields=fields,
            started_at_turn=self.s._brain_turn_counter,
            submit_label=submit_label,
        )
        self.s.form_session = sess
        labels = ", ".join(fs.label for fs in sess.fields.values())
        return (
            f"[form_begin] intent={intent!r} fields=[{labels}]\n"
            f"Now: call browser_screenshot once to anchor every field's "
            f"bbox, then for each field call browser_type_at(vision_index="
            f"V_n, text=...). After typing into a field that opens "
            f"autocomplete, click the matching suggestion (or press "
            f"Escape) BEFORE moving on. When every field is filled, "
            f"call browser_form_commit to verify."
        )


@tool_parameters(
    tool_parameters_schema(session_id=StringSchema("Session ID"), required=["session_id"])
)
class BrowserFormStatusTool(Tool):
    """Phase 2.1: report the current form-fill checklist."""

    name = "browser_form_status"
    description = (
        "Report status of the active form-fill session: which fields are "
        "still pending, which are filled, which need autocomplete picks. "
        "Cheap / no screenshot. Returns [no_form_session] if none active."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, **kw: Any) -> str:
        sess = self.s.form_session
        if sess is None:
            return (
                "[no_form_session] No form-fill session active. Call "
                "browser_form_begin to start tracking a multi-field form."
            )
        return sess.remaining_checklist(max_lines=20)


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        force=BooleanSchema(
            description=(
                "Skip the verify-all-fields check and close the session anyway. "
                "Use only when you intentionally want to submit a partial form."
            ),
            nullable=True,
        ),
        required=["session_id"],
    )
)
class BrowserFormCommitTool(Tool):
    """Phase 2.1: verify and close a form-fill session.

    Forces a fresh screenshot, then for each tracked field checks that
    the typed value appears in the page's relevant_text. Returns a
    structured pass/fail report — the brain decides whether to refill
    mismatched fields or submit.
    """

    name = "browser_form_commit"
    description = (
        "Verify every tracked field's typed value appears on screen, "
        "then close the form-fill session. Returns the per-field "
        "verdict so the brain can refill any mismatches before "
        "clicking submit."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        force: bool = False,
        **kw: Any,
    ) -> str:
        sess = self.s.form_session
        if sess is None:
            return (
                "[form_commit_failed:no_session] No form session is "
                "active. Call browser_form_begin first."
            )
        # Refresh vision so we verify against the latest screenshot.
        resp = self.s._last_vision_response
        text_hay = ""
        if resp is not None:
            text_hay = (getattr(resp, "relevant_text", "") or "").lower()
        for fs in sess.fields.values():
            if fs.status == FieldStatus.SKIPPED:
                continue
            target_lower = (fs.target_value or "").strip().lower()
            if not target_lower:
                continue
            if target_lower in text_hay:
                sess.mark_verified(fs.label, fs.target_value)
            else:
                # Don't overwrite VERIFIED set during typing flow.
                if fs.status not in (FieldStatus.VERIFIED,):
                    fs.status = FieldStatus.MISMATCH
        summary = sess.commit_summary()
        if not force and not sess.is_complete():
            return (
                f"[form_commit_incomplete] {summary}\n"
                f"{sess.remaining_checklist(max_lines=10)}\n"
                f"Refill the mismatched / pending fields, then call "
                f"browser_form_commit again. Use force=true ONLY if you "
                f"intentionally want to submit a partial form."
            )
        # Success — clear the session so the next form starts clean.
        result = (
            f"[form_commit_ok] {summary}\n"
            f"You may now click the submit button "
            + (f"('{sess.submit_label}') " if sess.submit_label else "")
            + "via browser_click_at(vision_index=V_n)."
        )
        self.s.form_session = None
        return result


# Re-export FieldStatus into module scope so the commit tool can refer to
# it without importing locally on every call.
try:
    from superbrowser_bridge.form_session import FieldStatus  # noqa: F401
except ImportError:
    class FieldStatus:  # type: ignore[no-redef]
        VERIFIED = "verified"
        SKIPPED = "skipped"
        MISMATCH = "mismatch"
