"""Surgical undo for the most recent reversible click.

Distinct from `browser_rewind_to_checkpoint`. Rewind reloads the
page from `best_checkpoint_url` and wipes all in-page state — fine
for navigation misclicks, catastrophic for the "8 right checkbox
checks then 1 wrong one" pattern. This tool consumes the undo ring
populated by the click tools (state.py:_undo_ring) and reverses
just the most recent reversible click(s):
  - toggle: re-click the same target to flip back.
  - nav:    one history.back() step (T1; T3 falls back to JS).
  - irreversible (form submit / delete / place-order / etc.):
            refused with a structured error.
"""

from __future__ import annotations

from typing import Any

import httpx
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)

from ..http_client import SUPERBROWSER_URL, _request_with_backoff
from ..state import BrowserSessionState
from ..vision_pipeline import _append_fresh_vision, _schedule_vision_prefetch


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        steps=IntegerSchema(
            description=(
                "How many recent reversible clicks to peel back. Default "
                "1. Maximum 4. Stops at the first irreversible entry."
            ),
            nullable=True,
        ),
        required=["session_id"],
    )
)
class BrowserUndoLastClickTool(Tool):
    name = "browser_undo_last_click"
    description = (
        "Surgically reverse the most recent NON-NAVIGATIONAL click that "
        "produced an unintended state change. Unlike "
        "browser_rewind_to_checkpoint, this does NOT reload the page — "
        "it preserves all other in-page state (other checkboxes, scroll, "
        "form fields, applied filters).\n\n"
        "Three behaviours, picked from the click's recorded class:\n"
        "  • TOGGLE (filter chip / checkbox / radio / aria-pressed flip) "
        "→ re-clicks the same target to flip back. The is_active flip "
        "is verified post-click.\n"
        "  • NAV (the click navigated the page) → falls through to one "
        "history.back() step. Use browser_rewind_to_checkpoint when "
        "you need to jump further back to a known-good URL.\n"
        "  • IRREVERSIBLE (form submit, delete, place/buy/pay, send) "
        "→ refused with a structured error.\n\n"
        "Optional `steps` argument (default 1, max 4) peels off N "
        "consecutive reversible clicks LIFO. Stops at the first "
        "irreversible entry."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        steps: int | None = None,
        **kw: Any,
    ) -> str:
        try:
            n = int(steps) if steps is not None else 1
        except (TypeError, ValueError):
            n = 1
        n = max(1, min(int(self.s.UNDO_RING_MAX), n))

        # Block until any in-flight vision prefetch lands so we operate
        # on the post-click vision (which decides toggle flip detection).
        sync_block = await self.s.ensure_vision_synced(reason="browser_undo_last_click")
        if sync_block:
            return sync_block
        self.s._brain_turn_counter += 1

        candidates = self.s.pop_undo_candidates(n)
        if not candidates:
            return (
                "[undo_blocked:no_history] No reversible clicks in the "
                "ring. browser_undo_last_click only reverses the most "
                "recent click(s); nothing has been clicked yet, or the "
                "ring was already drained. Use "
                "browser_rewind_to_checkpoint to jump back further."
            )
        # Top-of-ring is candidates[0]. If it's irreversible, refuse the
        # whole call — partial undo on top of an irreversible doesn't
        # make sense (you'd be reaching past a destructive action).
        first = candidates[0]
        if first.get("kind") == "irreversible":
            return (
                f"[undo_blocked:irreversible label="
                f"{first.get('label','')!r}] The most recent click looks "
                f"destructive (matched a submit/buy/delete/send pattern) "
                f"and will not be reversed automatically. If this was a "
                f"false positive, call browser_navigate or "
                f"browser_rewind_to_checkpoint manually. To recover from "
                f"a real destructive action, contact the site directly "
                f"(this tool cannot undo what the server has committed)."
            )

        results: list[str] = []
        successful: list[dict] = []
        partial = False
        partial_reason = ""

        for entry in candidates:
            kind = entry.get("kind")
            if kind == "irreversible":
                # We've walked into an irreversible deeper in the ring.
                # Stop here, don't undo it.
                results.append(
                    f"[undo_stopped:irreversible label="
                    f"{entry.get('label','')!r}] Halted before reversing "
                    f"a destructive click."
                )
                break

            if kind == "nav":
                ok, msg = await self._undo_nav(session_id, entry)
                results.append(msg)
                if ok:
                    self.s.mark_undone(entry)
                    successful.append(entry)
                else:
                    partial = True
                    partial_reason = msg
                    break
                continue

            # toggle (or unknown — treat as toggle: re-click same target)
            ok, msg = await self._undo_toggle(session_id, entry)
            results.append(msg)
            if ok:
                self.s.mark_undone(entry)
                successful.append(entry)
            else:
                partial = True
                partial_reason = msg
                break

        header_lines: list[str] = []
        if successful and not partial:
            summary = "; ".join(
                self._fmt_undone_entry(e) for e in successful
            )
            header_lines.append(f"[UNDONE: {summary}]")
        elif successful and partial:
            summary = "; ".join(
                self._fmt_undone_entry(e) for e in successful
            )
            header_lines.append(
                f"[undo_partial steps_undone={len(successful)} of "
                f"{len(candidates)} reason={partial_reason!r}] {summary}"
            )
        else:
            header_lines.append(
                f"[undo_failed steps_attempted={len(candidates)} reason="
                f"{partial_reason!r}]"
            )

        # Record this in step history so worker hook / activity log have
        # a single line to render around.
        last_msg = "; ".join(results)[:300]
        self.s.record_step(
            "browser_undo_last_click",
            f"steps={n}",
            last_msg,
        )
        self.s.log_activity(
            f"undo({len(successful)}/{len(candidates)})",
            last_msg[:80],
        )

        # Fresh vision so the brain sees the reverted state.
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        body = "\n".join(header_lines + results)
        return await _append_fresh_vision(
            _vision_task,
            body,
            state=self.s,
        )

    @staticmethod
    def _fmt_undone_entry(entry: dict) -> str:
        label = (entry.get("label") or "?")[:40]
        v = entry.get("vision_index")
        prefix = f"V{v} " if isinstance(v, int) and v > 0 else ""
        kind = entry.get("kind", "?")
        if kind == "toggle":
            pre = entry.get("pre_active")
            arrow = (
                "off" if pre else "on"
            ) if pre is not None else "reverted"
            return f"{prefix}{label!r}→{arrow}"
        if kind == "nav":
            return f"back from {entry.get('post_url','?')[:60]}"
        return f"{prefix}{label!r}"

    async def _undo_toggle(
        self, session_id: str, entry: dict,
    ) -> tuple[bool, str]:
        """Re-click the recorded target to flip its active state back."""
        box_2d = entry.get("box_2d")
        label = entry.get("label", "")
        if not box_2d or len(box_2d) != 4:
            return False, (
                f"[undo_skipped:no_bbox label={label!r}] no bbox geometry "
                f"recorded for this click — likely a DOM-index or "
                f"selector click that we can't synthesize a re-click for."
            )

        # Renormalize the bbox to pixel coords using the cached vision
        # response's image dims (same path BrowserClickAtTool uses). If
        # vision is gone, fall back to the recorded box_2d as raw pixels
        # — better than nothing, and the TS-side snap will still try.
        resp = self.s.vision_for_target_resolution()
        try:
            iw = float(getattr(resp, "image_width", 0) or 0)
            ih = float(getattr(resp, "image_height", 0) or 0)
            dpr = float(getattr(resp, "dpr", 1.0) or 1.0)
        except Exception:
            iw = ih = 0.0
            dpr = 1.0

        ymin, xmin, ymax, xmax = (
            float(box_2d[0]), float(box_2d[1]),
            float(box_2d[2]), float(box_2d[3]),
        )
        if iw > 0 and ih > 0:
            x0 = (xmin / 1000.0) * iw / max(dpr, 1.0)
            y0 = (ymin / 1000.0) * ih / max(dpr, 1.0)
            x1 = (xmax / 1000.0) * iw / max(dpr, 1.0)
            y1 = (ymax / 1000.0) * ih / max(dpr, 1.0)
        else:
            x0, y0, x1, y1 = xmin, ymin, xmax, ymax

        payload: dict[str, Any] = {
            "bbox": {"x0": x0, "y0": y0, "x1": x1, "y1": y1},
        }
        if label:
            payload["expected_label"] = label[:120]
            payload["label"] = label[:120]

        try:
            r = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/click",
                json=payload,
                timeout=30.0,
            )
        except (httpx.ReadTimeout, httpx.ConnectTimeout):
            return False, "[undo_failed:transport_timeout]"
        except Exception as exc:
            return False, f"[undo_failed:transport] {exc}"

        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            return False, f"[undo_failed:http_{r.status_code}] {err}"

        data = r.json()
        # Auto-refresh fingerprints from the undo click. (B6)
        _fp_map = data.get("fingerprints") if isinstance(data, dict) else None
        if isinstance(_fp_map, dict):
            self.s.element_fingerprints = {
                int(k): v for k, v in _fp_map.items() if isinstance(v, str)
            }
        if isinstance(data, dict) and data.get("error") == "element_mismatch":
            return False, (
                f"[undo_failed:element_mismatch] target "
                f"({label!r}) no longer at recorded coords; the page "
                f"shifted between the click and the undo. Re-screenshot "
                f"and target the moved element manually."
            )

        # Verify the click landed (mutation_delta > 0). Without a fresh
        # vision pass we can't yet confirm `is_active` flipped — that
        # check happens implicitly when the brain reads the next vision
        # response (which will carry just_toggled='off' on the bbox).
        effect = data.get("effect") or {}
        try:
            mutation_delta = int(effect.get("mutation_delta") or 0)
        except (TypeError, ValueError):
            mutation_delta = 0
        if mutation_delta == 0 and not effect.get("url_changed"):
            return False, (
                f"[undo_partial:no_effect] Re-click on {label!r} produced "
                f"no DOM change. The target may already be in the desired "
                f"state, or the page is blocking the click."
            )

        # Update current_url if the post-click response shifted
        actual_url = data.get("url")
        if actual_url:
            self.s.record_url(actual_url)
        return True, f"[undo_ok:toggle label={label!r}]"

    async def _undo_nav(
        self, session_id: str, entry: dict,
    ) -> tuple[bool, str]:
        """One history.back() step. Verifies URL drifted back toward
        the entry's pre_url; refuses to go back further if the back step
        landed somewhere unexpected."""
        try:
            r = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/back",
                json={},
                timeout=20.0,
            )
        except Exception as exc:
            return False, f"[undo_failed:transport] {exc}"
        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            return False, f"[undo_failed:http_{r.status_code}] {err}"

        data = r.json()
        # Auto-refresh fingerprints after history.back(). (B6)
        _fp_map = data.get("fingerprints") if isinstance(data, dict) else None
        if isinstance(_fp_map, dict):
            self.s.element_fingerprints = {
                int(k): v for k, v in _fp_map.items() if isinstance(v, str)
            }
        new_url = data.get("url") or ""
        if new_url:
            self.s.record_url(new_url)

        pre_url = entry.get("pre_url") or ""
        if pre_url and new_url:
            if self.s._normalize_url(new_url) != self.s._normalize_url(pre_url):
                return False, (
                    f"[undo_failed:url_drift expected={pre_url[:60]} "
                    f"actual={new_url[:60]}] history.back() landed on a "
                    f"different page than the one we navigated FROM. The "
                    f"page may use replaceState; falling through. Use "
                    f"browser_rewind_to_checkpoint or browser_navigate "
                    f"to recover."
                )
        return True, f"[undo_ok:nav back_to={new_url[:60]}]"
