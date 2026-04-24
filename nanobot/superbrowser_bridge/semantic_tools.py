"""Semantic-target mutation tools.

These tools decouple the brain's intent from vision-index mechanics.
Instead of "click V1" (which depends on vision bbox ordering), the
brain says "click the SFMOMA suggestion" and the tool:

  1. Takes a fresh screenshot + vision analysis in the same turn
     (atomic — no drift window between plan and dispatch).
  2. Scores each bbox against the description with a small
     label/role matcher in pure Python.
  3. Dispatches the action at the matched bbox's coordinates.
  4. Returns the usual post-action caption with `effect` classified
     so `[no_effect:...]` bubbles up like any other mutation tool.

This is the user's "get proper bbox + image before each action"
pattern executed as a single atomic call — the V-index drift bug
doesn't apply because there is no V-index.

Registered in `register_session_tools` right after the index/coord
click/type tools so the brain can reach for it when those hit penalty
thresholds on a domain.
"""

from __future__ import annotations

import re
import time
from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    BooleanSchema,
    StringSchema,
    tool_parameters_schema,
)

from superbrowser_bridge.session_tools import (
    SUPERBROWSER_URL,
    BrowserSessionState,
    _append_fresh_vision,
    _feedback_gate,
    _maybe_no_effect_prefix,
    _request_with_backoff,
    _require_fresh_vision,
    _schedule_vision_prefetch,
)


# ---------------------------------------------------------------- scoring


_INPUT_HINT_TOKENS = {
    "input", "field", "textbox", "search", "search box", "email",
    "password", "name", "address", "query", "destination", "where",
    "textarea", "combobox",
}


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"\b\w+\b", (s or "").lower()) if t}


def _score_bbox(bbox: Any, description: str, *, want_input: bool = False) -> float:
    """Return a match score in [0, 1]. Higher is better.

    Heuristic ladder (first matching rule wins):
      - exact normalized label match → 1.0
      - description substring of label OR label substring of description → 0.9
      - >= 80% of description tokens appear in label tokens → 0.8
      - >= 50% token overlap → 0.6
      - any overlap → 0.4
      - no overlap → 0.0
    Apply +0.1 role/role_in_scene boosts (button/target for clicks,
    input/role_in_scene="target" for typing).
    """
    label = _normalize(getattr(bbox, "label", "") or "")
    desc_n = _normalize(description)
    if not label or not desc_n:
        return 0.0

    base = 0.0
    if label == desc_n:
        base = 1.0
    elif desc_n in label or label in desc_n:
        base = 0.9
    else:
        l_tok, d_tok = _tokens(label), _tokens(desc_n)
        if d_tok and l_tok:
            overlap = len(d_tok & l_tok) / max(1, len(d_tok))
            if overlap >= 0.8:
                base = 0.8
            elif overlap >= 0.5:
                base = 0.6
            elif overlap > 0:
                base = 0.4

    # No lexical match → no amount of role boosting makes this a legit
    # target. A search bar with label "Search" shouldn't match a
    # description of "Checkout" just because it's clickable.
    if base <= 0.0:
        return 0.0

    # Role boosts — break ties when multiple bboxes share a base score.
    # Capped at 1.0 total. Deliberately small (0.05–0.15) so they can
    # shift ordering but not manufacture a match.
    role = (getattr(bbox, "role", "") or "").lower()
    role_in_scene = (getattr(bbox, "role_in_scene", "") or "").lower()
    boost = 0.0
    if want_input:
        if role in ("input", "textbox", "combobox", "search"):
            boost += 0.15
        if any(tok in label for tok in _INPUT_HINT_TOKENS):
            boost += 0.05
    else:
        if role in ("button", "link", "menuitem") or getattr(bbox, "clickable", False):
            boost += 0.1
        if role_in_scene == "target":
            boost += 0.1
        elif role_in_scene == "blocker":
            boost += 0.05

    # Score can exceed base by up to ~0.2 but never exceeds 1.0.
    return min(1.0, base + boost)


def _labels_summary(bboxes: list[Any], limit: int = 8) -> str:
    """Render a short "labels we saw" hint for error messages."""
    if not bboxes:
        return "(vision returned no bboxes)"
    parts: list[str] = []
    for b in bboxes[:limit]:
        label = (getattr(b, "label", "") or "").strip()[:40]
        role = (getattr(b, "role", "") or "").strip()[:12]
        if label:
            parts.append(f"'{label}' ({role})" if role else f"'{label}'")
    more = f" +{len(bboxes) - limit} more" if len(bboxes) > limit else ""
    return ", ".join(parts) + more


# ---------------------------------------------------------------- vision


async def _fresh_vision_now(state: BrowserSessionState, session_id: str) -> Any:
    """Force a fresh screenshot + vision analysis in THIS turn.

    Unlike `_schedule_vision_prefetch`, this is synchronous from the
    caller's perspective — the tool waits for the result because it
    needs the bboxes to dispatch the action. Cache is busted inside
    the prefetch so the response reflects the page right now.

    Uses `_require_fresh_vision(force_refresh=True)` — passes the
    refresh opt-in as an explicit kwarg rather than mutating the
    VISION_GATE_ALWAYS_REFRESH env var, so two concurrent sessions
    can't race on the flag.
    """
    ok, _msg = await _require_fresh_vision(
        state, session_id,
        reason="semantic_target",
        force_refresh=True,
    )
    if not ok:
        return None
    resp = state._last_vision_response
    # Freeze as the current epoch — any subsequent _append_fresh_vision
    # will re-freeze post-action, which is the desired behavior.
    try:
        state.freeze_vision_epoch()
    except Exception:
        pass
    return resp


# ---------------------------------------------------------------- tools


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        # NB: the kwarg MUST NOT be named `description` — that collides
        # with `tool_parameters_schema`'s own reserved `description: str`
        # parameter, causing the StringSchema to land in the root-level
        # description field instead of in `properties`, and `json.dumps`
        # then blows up when the tool list gets serialized for the LLM.
        # Use `target` — short, clear, unique to semantic tools.
        target=StringSchema(
            "A short natural-language description of the element to click "
            "— match the visible label as closely as you can. Examples: "
            "'Accept cookies', 'San Francisco Museum of Modern Art', "
            "'Continue Anyway', 'Search button'."
        ),
        required=["session_id", "target"],
    )
)
class BrowserSemanticClickTool(Tool):
    """Click a target described semantically (by label/role) rather
    than by vision_index. Atomic: fresh vision analysis + dispatch in
    one turn, no drift window between plan and dispatch.

    Prefer this over `browser_click_at(vision_index=...)` when:
      - The tactic registry has flagged click_at as unreliable on the
        current domain (e.g., React autocomplete).
      - You know what the target says/does but the element tree is
        noisy enough that V-indices shift between turns.
    """

    name = "browser_semantic_click"
    description = (
        "Click the first element whose label matches the given "
        "description. Takes a fresh screenshot, analyzes with vision, "
        "picks the best-matching clickable bbox, dispatches the click, "
        "and returns the usual post-action caption. Atomic — no "
        "vision_index needed."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self, session_id: str, target: str, **kw: Any,
    ) -> str:
        print(f"\n>> browser_semantic_click({target!r})")
        gate = await _feedback_gate("browser_semantic_click")
        if gate:
            return gate
        resp = await _fresh_vision_now(self.s, session_id)
        if resp is None or not getattr(resp, "bboxes", None):
            return (
                f"[semantic_click_failed:no_vision] Could not get a fresh "
                f"vision pass for this action. Call browser_screenshot to "
                f"force one, or try browser_click_selector with a CSS "
                f"selector."
            )
        bboxes = list(resp.bboxes)
        # Rank all bboxes; keep only clickable + non-zero score.
        scored: list[tuple[float, Any]] = []
        for b in bboxes:
            s = _score_bbox(b, target, want_input=False)
            if s > 0 and getattr(b, "clickable", True):
                scored.append((s, b))
        scored.sort(key=lambda x: -x[0])
        if not scored or scored[0][0] < 0.5:
            return (
                f"[semantic_click_failed:no_match] Vision listed "
                f"{_labels_summary(bboxes)} but none match "
                f"{target!r} with confidence >= 0.5. Try a more "
                f"specific description, or inspect the bbox list via "
                f"browser_screenshot and pick a vision_index directly."
            )
        # Ambiguity check: if the top-2 scores are tied AND above 0.8,
        # surface the choice rather than guessing.
        if len(scored) >= 2 and scored[0][0] >= 0.8 and abs(scored[0][0] - scored[1][0]) < 0.05:
            top = scored[:3]
            rendered = ", ".join(
                f"'{getattr(b, 'label', '')!s}' (score={s:.2f})"
                for s, b in top
            )
            return (
                f"[semantic_click_ambiguous] Multiple bboxes match "
                f"{target!r}: {rendered}. Make the description more "
                f"specific, or use browser_click_at with a vision_index."
            )
        match_bbox = scored[0][1]
        iw = int(getattr(resp, "image_width", 0) or 0)
        ih = int(getattr(resp, "image_height", 0) or 0)
        dpr_val = float(getattr(resp, "dpr", 1.0) or 1.0)
        if iw <= 0 or ih <= 0:
            return (
                "[semantic_click_failed:no_image_dims] Fresh vision pass "
                "had no source dimensions; cannot denormalize coordinates."
            )
        x0, y0, x1, y1 = match_bbox.to_pixels(iw, ih, dpr=dpr_val)
        payload: dict[str, Any] = {
            "bbox": {"x0": x0, "y0": y0, "x1": x1, "y1": y1},
        }
        _t0 = time.monotonic()
        try:
            r = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/click",
                json=payload,
                timeout=30.0,
            )
        except Exception as exc:
            return f"[semantic_click_failed:network] {exc}"
        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            return f"[semantic_click_failed:http_{r.status_code}] {err}"

        data = r.json()
        match_label = getattr(match_bbox, "label", "") or "?"
        self.s.log_activity(
            f"semantic_click({target[:40]!r})",
            f"→ {match_label[:40]!r}",
        )
        self.s.record_step(
            "browser_semantic_click",
            target[:60],
            f"matched '{match_label[:60]}' score={scored[0][0]:.2f}",
            latency_ms=int((time.monotonic() - _t0) * 1000),
        )
        self.s.advance_observation_token("semantic_click")
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        caption = f"Clicked {match_label[:60]!r} (semantic match for {target!r})"
        return await _append_fresh_vision(
            _vision_task,
            _maybe_no_effect_prefix(
                data, "browser_semantic_click",
                self.s.build_text_only(data, caption),
                session_state=self.s,
            ),
            state=self.s,
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        # See BrowserSemanticClickTool for why this kwarg is `target`
        # and not `description` — it would collide with the outer
        # `tool_parameters_schema(description=...)` reserved kwarg.
        target=StringSchema(
            "A short description of the input field — match its label, "
            "placeholder, or ARIA name as closely as you can. Examples: "
            "'Email address', 'Where are you going?', 'Search destination', "
            "'First name'."
        ),
        text=StringSchema("Text to type into the matched field."),
        clear=BooleanSchema(
            description="Clear the field before typing (default: true). Uses React-safe clear.",
            default=True,
        ),
        required=["session_id", "target", "text"],
    )
)
class BrowserSemanticTypeTool(Tool):
    """Type into an input identified by a semantic description.

    Same atomic pattern as browser_semantic_click but targets input-
    class elements. Uses the TS server's /type endpoint via the nearest
    interactive element inside the matched bbox.
    """

    name = "browser_semantic_type"
    description = (
        "Type text into the input whose label/placeholder matches the "
        "description. Atomic: fresh vision + dispatch in one turn."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self, session_id: str, target: str, text: str,
        clear: bool = True, **kw: Any,
    ) -> str:
        print(f"\n>> browser_semantic_type({target!r}, text={text[:30]!r})")
        if text is None:
            text = ""
        gate = await _feedback_gate("browser_semantic_type")
        if gate:
            return gate
        resp = await _fresh_vision_now(self.s, session_id)
        if resp is None or not getattr(resp, "bboxes", None):
            return (
                f"[semantic_type_failed:no_vision] Could not get fresh "
                f"vision. Use browser_screenshot to force one, or call "
                f"browser_type_at with a vision_index."
            )
        bboxes = list(resp.bboxes)
        scored: list[tuple[float, Any]] = []
        for b in bboxes:
            s = _score_bbox(b, target, want_input=True)
            if s > 0:
                scored.append((s, b))
        scored.sort(key=lambda x: -x[0])
        if not scored or scored[0][0] < 0.5:
            return (
                f"[semantic_type_failed:no_match] Vision listed "
                f"{_labels_summary(bboxes)} but none match input "
                f"description {target!r} with confidence >= 0.5."
            )
        match_bbox = scored[0][1]
        iw = int(getattr(resp, "image_width", 0) or 0)
        ih = int(getattr(resp, "image_height", 0) or 0)
        dpr_val = float(getattr(resp, "dpr", 1.0) or 1.0)
        if iw <= 0 or ih <= 0:
            return "[semantic_type_failed:no_image_dims]"
        x0, y0, x1, y1 = match_bbox.to_pixels(iw, ih, dpr=dpr_val)
        target_x = (x0 + x1) / 2
        target_y = (y0 + y1) / 2
        # Use the existing /evaluate atomic fix-text path for semantic
        # typing so React-controlled inputs get the native-setter
        # treatment automatically. Falls back to /type-at-coords if
        # the atomic fix isn't available.
        from .session_tools import _ATOMIC_FIX_TEXT_JS
        import json as _json
        atomic_js = _ATOMIC_FIX_TEXT_JS.replace(
            "__TARGET_X__", str(float(target_x))
        ).replace(
            "__TARGET_Y__", str(float(target_y))
        ).replace(
            "__TARGET_TEXT__", _json.dumps(text)
        )
        try:
            ev = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
                json={"script": atomic_js},
                timeout=30.0,
            )
            ev.raise_for_status()
        except Exception as exc:
            return f"[semantic_type_failed:network] {exc}"
        payload_body = ev.json()
        result = (
            payload_body.get("result") if isinstance(payload_body, dict) else None
        ) or {}
        if not isinstance(result, dict) or not result.get("ok"):
            reason = (result or {}).get("reason", "unknown") if isinstance(result, dict) else "bad_shape"
            return f"[semantic_type_failed:{reason}]"
        before = str(result.get("before", "") or "")
        after = str(result.get("after", "") or "")
        changed = bool(result.get("changed"))
        match_label = getattr(match_bbox, "label", "") or "?"
        if not changed:
            caption = (
                f"Field {match_label!r} already contained {text!r}. "
                f"No typing needed."
            )
        else:
            caption = (
                f'Typed {text!r} into {match_label!r} (was {before!r}).'
                if before
                else f'Typed {text!r} into {match_label!r}.'
            )
        synthetic_data: dict[str, Any] = {
            "success": True,
            "before": before,
            "after": after,
            "changed": changed,
            # Mirror the effect shape other type tools emit.
            "effect": {
                "url_changed": False,
                "mutation_delta": 1 if changed else 0,
                "focused_changed": True if changed else False,
            },
        }
        self.s.record_step(
            "browser_semantic_type",
            f"{target[:40]}, text={text[:30]!r}",
            "typed" if changed else "skip_match",
        )
        if changed:
            self.s.advance_observation_token("semantic_type")
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        return await _append_fresh_vision(
            _vision_task,
            _maybe_no_effect_prefix(
                synthetic_data, "browser_semantic_type",
                self.s.build_text_only(synthetic_data, caption),
                session_state=self.s,
            ),
            state=self.s,
        )
