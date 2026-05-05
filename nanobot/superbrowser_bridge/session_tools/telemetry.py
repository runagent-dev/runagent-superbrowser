"""Telemetry helpers — screenshot budget computation and post-scroll
geometry recording on the session state."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .constants import _CAPTCHA_KEYWORDS, _HARD_DOMAINS

if TYPE_CHECKING:
    from .state import BrowserSessionState  # noqa: F401

def _compute_screenshot_budget(
    task_instruction: str = "",
    target_url: str = "",
    is_research: bool = False,
) -> int:
    """Task-complexity-aware screenshot budget.

    Base=6. +4 for research tasks, +10 for captcha-suspect tasks, +8 for
    known-hard domains. Capped at 30 to prevent runaway cost.
    """
    budget = 6
    lower_task = (task_instruction or "").lower()
    lower_url = (target_url or "").lower()
    if is_research:
        budget += 4
    if any(kw in lower_task for kw in _CAPTCHA_KEYWORDS):
        budget += 10
    if any(dom in lower_url for dom in _HARD_DOMAINS):
        budget += 8
    return min(budget, 30)
def _update_scroll_telemetry(
    state: "BrowserSessionState",
    scroll_info: Any,
    direction: str | None,
    extra: dict | None = None,
) -> None:
    """Record post-scroll geometry on the session state.

    Read by `_format_state` (and the [SCROLL_STATE …] caption line in
    `build_text_only`) so vision can reason about whether more scrolling
    is plausible. Tolerant of missing scrollInfo — telemetry is best-
    effort and must not break the tool path.
    """
    try:
        if not isinstance(scroll_info, dict):
            scroll_info = {}
        scroll_y = int(scroll_info.get("scrollY") or 0)
        scroll_h = int(scroll_info.get("scrollHeight") or 0)
        vp_h = int(scroll_info.get("viewportHeight") or 0)
        # 12px of slack at the bottom catches off-by-one rounding without
        # falsely flagging "reached_bottom" mid-page.
        reached_bottom = scroll_h > 0 and (scroll_y + vp_h) >= (scroll_h - 12)
        reached_top = scroll_y <= 4
        prev = getattr(state, "scroll_telemetry", None) or {}
        history = list(prev.get("direction_history") or [])
        if direction:
            history.append(direction)
            history = history[-6:]
        tel = {
            "scrollY": scroll_y,
            "scrollHeight": scroll_h,
            "viewportHeight": vp_h,
            "direction_history": history,
            "reached_bottom": reached_bottom,
            "reached_top": reached_top,
        }
        if extra:
            tel.update(extra)
        state.scroll_telemetry = tel
    except Exception:
        # Telemetry is best-effort — never let it block the scroll tool.
        pass
