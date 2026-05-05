"""Effect-classification helpers.

`_classify_effect` reads the TS bridge's `effect` snapshot to decide
whether a mutation tool actually moved the page (URL change, DOM
delta, focus change). `_maybe_no_effect_prefix` wraps tool captions
with a `[no_effect:...]` header so the brain and the worker hook can
distinguish a stalled tool from a real success.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .constants import _CURSOR_TOOL_NAMES

if TYPE_CHECKING:
    from .state import BrowserSessionState  # noqa: F401

def _diff_text(a: str, b: str) -> str:
    """Human-readable diff summary for a → b text change."""
    if a == b:
        return "no change"
    p = 0
    while p < len(a) and p < len(b) and a[p] == b[p]:
        p += 1
    suf = 0
    while (suf < len(a) - p and suf < len(b) - p
           and a[len(a) - 1 - suf] == b[len(b) - 1 - suf]):
        suf += 1
    old_mid = a[p:len(a) - suf]
    new_mid = b[p:len(b) - suf]
    if not old_mid and new_mid:
        return f"inserted {new_mid!r} at position {p}"
    if old_mid and not new_mid:
        return f"removed {old_mid!r} at position {p}"
    return f"replaced {old_mid!r} with {new_mid!r} at position {p}"

def _maybe_no_effect_prefix(
    data: Any, tool_name: str, base_caption: str,
    *, session_state: "BrowserSessionState | None" = None,
) -> str:
    """Wrap a mutation-tool caption with a `[no_effect:...]` header when
    the TS bridge reports zero url/DOM/focus delta. The base caption is
    preserved so vision prefetch, cached bboxes and elements text still
    reach the brain — the prefix is what the brain AND the worker hook
    read as a hard failure signal.

    Also records the failure against the per-domain tactic registry via
    `routing.record_tactic_failure` and (on effect) decays any prior
    penalty via `routing.decay_tactic_success`. The penalty data is what
    the next worker's delegation prompt reads to pre-select a better
    tactic on sites that systematically reject a given tool.
    """
    had_effect, reason = _classify_effect(data, tool_name)
    # Tactic-penalty bookkeeping — resolve domain from session state.
    domain = ""
    if session_state is not None:
        try:
            from urllib.parse import urlparse
            url = session_state.current_url or ""
            if url:
                host = (urlparse(url).hostname or "").lower()
                domain = host[4:] if host.startswith("www.") else host
        except Exception:
            domain = ""
    try:
        from superbrowser_bridge.routing import (
            record_tactic_failure, decay_tactic_success,
        )
        if domain:
            if had_effect:
                decay_tactic_success(domain, tool_name)
            else:
                record_tactic_failure(domain, tool_name)
    except Exception:
        pass

    if had_effect:
        # Positive reinforcement — when a cursor tool successfully
        # moves the page state, tag it so the brain's next turn sees
        # "this tactic worked" and stays on the cursor track instead
        # of pivoting to scripts. Also resets the script-usage
        # counter so a cursor-success cleanly breaks any recent
        # script streak.
        if tool_name in _CURSOR_TOOL_NAMES:
            if session_state is not None:
                try:
                    session_state.consecutive_script_calls = 0
                except Exception:
                    pass
            return f"[cursor_success:{tool_name}] {base_caption}"
        return base_caption
    hint = (
        f"[no_effect:{tool_name}] {reason}. The tool dispatched but the "
        f"page didn't respond — no DOM mutation, no URL change, no focus "
        f"change. Do NOT retry the same tool with the same target; try "
        f"ONE OF (in this preference order): "
        f"(a) **browser_screenshot** to refresh the V_n bbox list, then "
        f"**browser_click_at(vision_index=V_n)** with a different V_n "
        f"that better matches your intended target; "
        f"(b) browser_rewind_to_checkpoint if the page appears frozen. "
        f"Do NOT synthesize clicks via browser_run_script — JS clicks are "
        f"isTrusted=false and bot-detected; the sandbox will reject them."
    )
    return f"{hint}\n{base_caption}"

def _maybe_script_usage_warning(state: "BrowserSessionState") -> str:
    """Return a `[script_warning] ...` string when the brain is
    over-using `browser_run_script` even though cursor alternatives
    are visible, else empty string.

    Trigger: 3+ consecutive script calls, AND the last vision pass
    emitted at least one clickable bbox. The warning lists the top 3
    labels so the brain has concrete semantic targets to reach for.
    """
    try:
        count = int(state.consecutive_script_calls or 0)
    except Exception:
        count = 0
    if count < 3:
        return ""
    resp = getattr(state, "_last_vision_response", None)
    bboxes = getattr(resp, "bboxes", None) if resp is not None else None
    if not bboxes:
        return ""
    labels: list[str] = []
    for b in bboxes[:20]:
        if not getattr(b, "clickable", False):
            continue
        lbl = (getattr(b, "label", "") or "").strip()
        if lbl and lbl not in labels:
            labels.append(lbl)
        if len(labels) >= 3:
            break
    if not labels:
        return ""
    rendered = ", ".join(f"'{lbl}'" for lbl in labels)
    return (
        f"[script_warning] {count} consecutive run_script calls. "
        f"Vision shows clickable bboxes ({rendered}, ...) — these can "
        f"be clicked atomically via `browser_semantic_click(target='<label>')` "
        f"without the WAF-block risk scripts carry. Reserve scripts "
        f"for actions no cursor tool can express."
    )

def _classify_effect(
    data: Any, tool_name: str,
) -> tuple[bool, str]:
    """Inspect a mutation tool's HTTP response for the TS `effect` field.

    Returns `(had_effect, no_effect_reason)`:
      * `had_effect=True, ""` when the TS bridge reports any of
        url_changed / mutation_delta > 0 / focused_changed.
      * `had_effect=False, <human_reason>` when all three are zero —
        the caller prefixes `[no_effect:<tool>] …` onto its return so
        the brain and the worker hook can distinguish "the tool fired
        but nothing happened" from a real success.
      * `had_effect=True, ""` when the `effect` field is missing —
        preserves legacy behavior against an older TS bridge that
        hasn't shipped the effect snapshot yet.

    Used by click / type / keys / drag / type_at / click_at / drag_slider
    at the moment they've got the HTTP response back but haven't built
    the brain-facing caption yet.
    """
    if not isinstance(data, dict):
        return True, ""
    effect = data.get("effect")
    if not isinstance(effect, dict):
        return True, ""  # TS side too old OR path didn't capture effect
    url_changed = bool(effect.get("url_changed"))
    try:
        mutation_delta = int(effect.get("mutation_delta") or 0)
    except (TypeError, ValueError):
        mutation_delta = 0
    focused_changed = bool(effect.get("focused_changed"))
    if url_changed or mutation_delta > 0 or focused_changed:
        return True, ""
    return False, (
        f"{tool_name}: url unchanged, DOM unchanged "
        f"(mutation_delta=0), focus unchanged"
    )

