"""Step-history mining + budget computation + scroll telemetry.

Read by the resumption-handoff path (for `recent_failures`), by
`BrowserOpenTool` (for budget allocation), and by the scroll tools
(for `[SCROLL_STATE …]` caption lines).
"""

from __future__ import annotations

from typing import Any

from .effects import _CAPTCHA_KEYWORDS, _HARD_DOMAINS


def _extract_recent_failures(step_history: list[dict], limit: int = 5) -> list[dict]:
    """Pull the most recent tool steps that look like failures.

    With Priority 1 in place, click/type results include phrases like
    '(element_covered):' or '(stale_selector):' when the structured reason
    is set. We match on those plus generic error markers.
    """
    out: list[dict] = []
    markers = ("FAILED", "failed (", "error:", "Script error", "ERROR:", "NOT solved")
    for step in reversed(step_history):
        result = str(step.get("result") or "")
        if any(m in result for m in markers):
            out.append({
                "tool": step.get("tool", ""),
                "args": str(step.get("args", ""))[:160],
                "result_excerpt": result[:220],
                "url": step.get("url", ""),
                "time": step.get("time", ""),
            })
        if len(out) >= limit:
            break
    return list(reversed(out))


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
