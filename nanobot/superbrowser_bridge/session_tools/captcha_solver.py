"""Per-step vision-driven captcha solver.

`_solve_captcha_iterative` is the single core loop shared by the
in-tool BrowserSolveCaptchaTool and the antibot/captcha/solve_vision
fallback path. Lives in its own module because it is large (~400 lines)
and otherwise weighs down the state and tools modules.
"""

from __future__ import annotations

import asyncio
import sys as _sys
import time
from typing import Any

from .constants import SUPERBROWSER_URL, _SUBMIT_KEYWORDS
from .http_client import _request_with_backoff as _real_request_with_backoff
from .state import BrowserSessionState
from .vision_sync import _read_image_dims


async def _request_with_backoff(*args: Any, **kwargs: Any) -> Any:
    """Wrapper that defers to ``session_tools._request_with_backoff``.

    The package-level symbol is what tests patch (``patch.object(st,
    "_request_with_backoff", ...)``). Looking it up at call time lets
    those patches reach the iterative solver even though the solver
    now lives in its own submodule. Falls back to the http_client
    function when the package isn't fully initialized yet.
    """
    pkg = _sys.modules.get("superbrowser_bridge.session_tools")
    fn = getattr(pkg, "_request_with_backoff", None) if pkg is not None else None
    if fn is None or fn is _request_with_backoff:
        fn = _real_request_with_backoff
    return await fn(*args, **kwargs)

def _first_actionable(resp: Any) -> Any:
    """Pick the next bbox worth acting on from a VisionResponse.

    Preference: unselected captcha_tile → slider_handle → verify/submit
    button → captcha_widget fallback. Returns None if the response has
    nothing usable. Purely local — no side effects.
    """
    bboxes = getattr(resp, "bboxes", None) or []
    tiles, handles, submits, widgets = [], [], [], []
    for b in bboxes:
        role = (getattr(b, "role", "") or "").lower()
        label = (getattr(b, "label", "") or "").lower()
        if role == "captcha_tile":
            tiles.append(b)
        elif role == "slider_handle":
            handles.append(b)
        elif role == "captcha_widget":
            widgets.append(b)
        elif role == "button" and any(kw in label for kw in _SUBMIT_KEYWORDS):
            submits.append(b)
    # Tiles first if any remain; submit only takes priority when tiles are
    # gone (i.e. grid challenge completed).
    if tiles:
        return tiles[0]
    if handles:
        return handles[0]
    if submits:
        return submits[0]
    if widgets:
        return widgets[0]
    return None
async def _solve_captcha_iterative(
    session_id: str,
    captcha_info: Any,
    vision_agent: Any,
    *,
    task_instruction: str = "",
    solve_round: int = 0,
    max_steps: int = 12,
) -> dict[str, Any]:
    """Per-step vision-driven captcha solver.

    Each iteration: screenshot → vision (returns one next action) → click
    or drag → poll structural hash until page changes (cap 600ms). Exits
    when vision reports captcha_present=false or when a safety guard
    (dead-action streak, max_steps, HTTP failure) fires.

    Returns a structured dict the orchestrator's captcha-learnings
    consumer understands — see orchestrator_tools._update_captcha_learnings.
    Never raises into the caller; failures collapse into solved=False +
    an `error` field.
    """
    actions: list[str] = []
    last_click_xy: tuple[int, int] | None = None
    # Trail of up to 6 recent click points, overlaid on the next
    # screenshot so the model can see where we've already interacted.
    cursor_trail: list[tuple[int, int]] = []
    same_action_streak = 0
    steps_taken = 0
    model_name: str | None = None
    provider_name: str | None = None

    # Surface any text prompt the native detector already captured.
    prompt_hint = ""
    if captcha_info is not None:
        for n in list(getattr(captcha_info, "notes", None) or []):
            if isinstance(n, str) and n.startswith("text_signal:"):
                prompt_hint = n.split(":", 1)[1]
                break

    for step in range(max_steps):
        steps_taken = step + 1
        # 1. Screenshot + page state.
        try:
            sr = await _request_with_backoff(
                "GET",
                f"{SUPERBROWSER_URL}/session/{session_id}/state",
                params={"vision": "true"},
                timeout=15.0,
            )
            sr.raise_for_status()
            state = sr.json()
        except Exception as exc:
            actions.append(f"step {step}: state fetch failed: {exc}")
            return {
                "solved": False, "method": "vision_iterative",
                "subMethod": "python_vision_agent",
                "steps": steps_taken, "actions": actions,
                "provider": provider_name, "model": model_name,
                "error": f"state_fetch:{exc}",
            }

        b64 = state.get("screenshot")
        if not b64:
            actions.append(f"step {step}: no screenshot in state payload")
            return {
                "solved": False, "method": "vision_iterative",
                "subMethod": "python_vision_agent",
                "steps": steps_taken, "actions": actions,
                "provider": provider_name, "model": model_name,
                "error": "no_screenshot",
            }
        current_url = state.get("url", "")
        elements_str = (
            state.get("clickableElementsToString") or state.get("elements") or ""
        )
        try:
            page_hash = BrowserSessionState.hash_page_content(elements_str)
        except Exception:
            page_hash = ""
        img_w, img_h = _read_image_dims(b64)

        # 2. Ask vision for the single next action.
        last_action_hint = (
            f"Previous click: ({last_click_xy[0]},{last_click_xy[1]})"
            if last_click_xy else "Previous click: none"
        )
        try:
            resp = await vision_agent.analyze(
                screenshot_b64=b64,
                # "captcha step" routes to the solve_captcha_step intent
                # bucket in prompts.intent_bucket — step-mode suppresses
                # SoM overlay and skips result caching.
                intent="solve captcha step — pick the single next action",
                session_id=session_id,
                url=current_url,
                dom_hash=f"cap-r{solve_round}-s{step}-{page_hash}",
                image_width=img_w,
                image_height=img_h,
                task_instruction=(
                    (task_instruction or "")
                    + ("\nCaptcha prompt: " + prompt_hint if prompt_hint else "")
                    + f"\n{last_action_hint}"
                    + "\nReturn next_action committing to ONE step. If every "
                    "matching tile appears selected/dismissed, pick "
                    "action_type=submit targeting the verify button. If the "
                    "captcha appears gone, pick action_type=done. Do NOT "
                    "target within 40 pixels of the previous click unless a "
                    "visibly new tile has rendered there."
                ),
                cursor_trail=cursor_trail if cursor_trail else None,
            )
        except Exception as exc:
            actions.append(f"step {step}: vision call failed: {exc}")
            return {
                "solved": False, "method": "vision_iterative",
                "subMethod": "python_vision_agent",
                "steps": steps_taken, "actions": actions,
                "provider": provider_name, "model": model_name,
                "error": f"vision_call:{exc}",
            }

        model_name = model_name or resp.model
        provider_name = provider_name or resp.provider

        # 3. Done if the captcha is gone (only trust this after step 0 —
        # the very first screenshot should see the captcha).
        if step > 0 and not resp.flags.captcha_present:
            actions.append(f"step {step}: captcha_present=false, exiting loop")
            break

        # 4. Prefer the structured next_action from step-mode. Fall back
        # to the bbox-preference picker for older vision responses that
        # don't fill next_action (e.g. providers that ignore the field,
        # or non-step intents calling the shim).
        na = getattr(resp, "next_action", None)
        forced_drag = False  # na says drag_slider — override role dispatch
        forced_type = False  # na says type_text — use target_input_bbox
        type_value = ""
        last_expect_change = "static"
        if na is not None:
            at = (getattr(na, "action_type", "") or "").lower()
            if at == "done":
                actions.append(f"step {step}: next_action=done, exiting loop")
                break
            if at == "stuck":
                actions.append(
                    f"step {step}: next_action=stuck "
                    f"(reason: {getattr(na, 'reasoning', '')[:120]})"
                )
                return {
                    "solved": False, "method": "vision_iterative",
                    "subMethod": "python_vision_agent",
                    "steps": steps_taken, "actions": actions,
                    "provider": provider_name, "model": model_name,
                    "error": "vision_stuck",
                }
            forced_drag = at == "drag_slider"
            forced_type = at == "type_text"
            last_expect_change = getattr(na, "expect_change", "static") or "static"
            if forced_type:
                # type_text targets the input field, not the image.
                target = (
                    getattr(na, "target_input_bbox", None)
                    or getattr(na, "target_bbox", None)
                )
                type_value = (getattr(na, "type_value", "") or "").strip()
                if not type_value:
                    actions.append(
                        f"step {step}: type_text without type_value — treating as stuck"
                    )
                    return {
                        "solved": False, "method": "vision_iterative",
                        "subMethod": "python_vision_agent",
                        "steps": steps_taken, "actions": actions,
                        "provider": provider_name, "model": model_name,
                        "error": "type_text_missing_value",
                    }
            else:
                target = getattr(na, "target_bbox", None)
        else:
            target = None

        if target is None:
            target = _first_actionable(resp)
        if target is None:
            if step == 0 and resp.flags.captcha_widget_bbox is not None:
                target = resp.flags.captcha_widget_bbox
            else:
                actions.append(f"step {step}: no actionable bbox returned")
                return {
                    "solved": False, "method": "vision_iterative",
                    "subMethod": "python_vision_agent",
                    "steps": steps_taken, "actions": actions,
                    "provider": provider_name, "model": model_name,
                    "error": "no_actionable_bbox",
                }

        try:
            cx, cy = target.center_pixels(img_w, img_h)
            x0, y0, x1, y1 = target.to_pixels(img_w, img_h)
        except Exception as exc:
            actions.append(f"step {step}: malformed bbox: {exc}")
            return {
                "solved": False, "method": "vision_iterative",
                "subMethod": "python_vision_agent",
                "steps": steps_taken, "actions": actions,
                "provider": provider_name, "model": model_name,
                "error": "malformed_bbox",
            }

        # 5. Dead-action guard. Two successive near-duplicates within 40px
        # and no site change → bail to the caller (who decides handoff).
        if last_click_xy and abs(cx - last_click_xy[0]) < 40 and abs(cy - last_click_xy[1]) < 40:
            same_action_streak += 1
            if same_action_streak >= 2:
                actions.append(
                    f"step {step}: third same-target attempt within 40px — bailing"
                )
                return {
                    "solved": False, "method": "vision_iterative",
                    "subMethod": "python_vision_agent",
                    "steps": steps_taken, "actions": actions,
                    "provider": provider_name, "model": model_name,
                    "error": "dead_action_streak",
                }
        else:
            same_action_streak = 0

        # 6. Dispatch. Explicit `drag_slider` / `type_text` from next_action
        # win; otherwise fall back to bbox role.
        role = (getattr(target, "role", "") or "").lower()
        if forced_type:
            try:
                tr = await _request_with_backoff(
                    "POST",
                    f"{SUPERBROWSER_URL}/session/{session_id}/type-at",
                    json={
                        "x": cx, "y": cy,
                        "text": type_value,
                        "clear": True,
                    },
                    timeout=30.0,
                )
                if tr.status_code >= 400:
                    actions.append(
                        f"step {step}: type-at HTTP {tr.status_code}, ending loop"
                    )
                    break
                actions.append(
                    f"step {step}: type_text {type_value!r} @({cx},{cy})"
                )
            except Exception as exc:
                actions.append(f"step {step}: type-at dispatch exception: {exc}")
                break
        elif forced_drag or role == "slider_handle":
            if captcha_info is not None and getattr(captcha_info, "widget_bbox", None):
                wbb = captcha_info.widget_bbox
                ex = max(float(wbb[2]) - 12, cx + 50)
            elif resp.flags.captcha_widget_bbox is not None:
                _x0, _y0, _x1, _y1 = resp.flags.captcha_widget_bbox.to_pixels(
                    img_w, img_h,
                )
                ex = max(_x1 - 12, cx + 50)
            else:
                ex = cx + 250
            try:
                dr = await _request_with_backoff(
                    "POST",
                    f"{SUPERBROWSER_URL}/session/{session_id}/drag",
                    json={
                        "startX": cx, "startY": cy,
                        "endX": ex, "endY": cy,
                        "steps": 30,
                    },
                    timeout=30.0,
                )
                if dr.status_code >= 400:
                    actions.append(
                        f"step {step}: drag HTTP {dr.status_code}, ending loop"
                    )
                    break
                actions.append(
                    f"step {step}: drag handle {getattr(target, 'label', '')!r} "
                    f"({cx},{cy})→({int(ex)},{cy})"
                )
            except Exception as exc:
                actions.append(f"step {step}: drag dispatch exception: {exc}")
                break
        else:
            try:
                cr = await _request_with_backoff(
                    "POST",
                    f"{SUPERBROWSER_URL}/session/{session_id}/click",
                    json={
                        "x": cx, "y": cy,
                        "bbox": {"x0": x0, "y0": y0, "x1": x1, "y1": y1},
                    },
                    timeout=30.0,
                )
                if cr.status_code == 409:
                    actions.append(
                        f"step {step}: click@({cx},{cy}) refused "
                        "(low-reward band) — re-analyzing"
                    )
                    # Do NOT advance last_click_xy/streak; re-ask vision.
                    continue
                if cr.status_code >= 400:
                    actions.append(
                        f"step {step}: click HTTP {cr.status_code}, ending loop"
                    )
                    break
                actions.append(
                    f"step {step}: click {role or 'bbox'} "
                    f"{getattr(target, 'label', '')!r} @({cx},{cy})"
                )
            except Exception as exc:
                actions.append(f"step {step}: click dispatch exception: {exc}")
                break

        last_click_xy = (cx, cy)
        cursor_trail.append((cx, cy))
        if len(cursor_trail) > 6:
            cursor_trail.pop(0)

        # 7. Poll the structural hash until it changes, capped adaptively
        # based on vision's expect_change hint. Hash-poll is faster than
        # a blind sleep on static grids and safer on slow re-rendering
        # ones. page_nav gets a much longer ceiling because whole-page
        # transitions take ~1.5s.
        poll_cap = {
            "page_nav": 2.5,
            "widget_replace": 1.0,
            "new_tile": 0.6,
            "static": 0.3,
        }.get(last_expect_change, 0.6)
        change_deadline = time.monotonic() + poll_cap
        while time.monotonic() < change_deadline:
            await asyncio.sleep(0.08)
            try:
                pr = await _request_with_backoff(
                    "GET",
                    f"{SUPERBROWSER_URL}/session/{session_id}/state",
                    params={"vision": "false"},
                    timeout=5.0,
                )
                if pr.status_code != 200:
                    continue
                ps = pr.json()
                _elems = (
                    ps.get("clickableElementsToString")
                    or ps.get("elements") or ""
                )
                if not _elems:
                    continue
                new_hash = BrowserSessionState.hash_page_content(_elems)
                if new_hash and new_hash != page_hash:
                    break
            except Exception:
                pass

    # 8. Verify once.
    solved = False
    try:
        vr = await _request_with_backoff(
            "GET",
            f"{SUPERBROWSER_URL}/session/{session_id}/state",
            params={"vision": "true"},
            timeout=15.0,
        )
        if vr.status_code == 200:
            verify_state = vr.json()
            verify_b64 = verify_state.get("screenshot")
            if verify_b64:
                vresp = await vision_agent.analyze(
                    screenshot_b64=verify_b64,
                    intent="verify captcha cleared",
                    session_id=session_id,
                    url=verify_state.get("url", ""),
                    dom_hash=f"cap-verify-r{solve_round}",
                )
                solved = not vresp.flags.captcha_present
    except Exception:
        solved = False

    return {
        "solved": solved,
        "method": "vision_iterative",
        "subMethod": "python_vision_agent",
        "steps": steps_taken,
        "actions": actions,
        "provider": provider_name,
        "model": model_name,
    }
