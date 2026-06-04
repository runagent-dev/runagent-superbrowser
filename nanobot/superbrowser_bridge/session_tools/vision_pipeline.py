"""Vision prefetch + brain-text emission pipeline.

Owns the asynchronous vision_agent.analyze() loop that fires after every
mutating tool. Mutation tools call `_schedule_vision_prefetch` on success;
the next screenshot/click finds the bboxes already cached.

Functions in this module read+write `BrowserSessionState` fields
(`_last_vision_response`, `_pending_vision_task`, etc.) but never import
the state class — `state` is taken as a parameter and only string-typed
in annotations (`from __future__ import annotations`).
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import time
from typing import Any

import httpx

from .http_client import SUPERBROWSER_URL, _request_with_backoff


# Compound-row split helpers (v2-C). The vision prompt instructs Gemini
# to emit chevron/expand triggers as separate bboxes, but in practice
# Gemini occasionally merges them. The DOM-side selectorEntries tell us
# WHERE chevrons live; we use them as a safety net to inject the
# missing sub-bbox after vision returns.
_COMPOUND_CHEVRON_CHARS = "▼▶◀▲►◄⌃⌄⋮"
_COMPOUND_TASK_STOPWORDS = frozenset({
    "this", "that", "with", "from", "into", "open", "click", "find",
    "select", "filter", "show", "tell", "what", "which", "where",
    "when", "page", "site", "link", "button", "search", "result",
    "results", "item", "items", "list", "menu", "option", "options",
})


def _is_chevron_entry(attrs: dict[str, Any], text: str) -> bool:
    """Decide if a DOM selectorEntry looks like an expand/collapse trigger."""
    if not isinstance(attrs, dict):
        return False
    if "aria-expanded" in attrs:
        return True
    if attrs.get("aria-haspopup"):
        return True
    t = (text or "").strip()
    if len(t) == 1 and t in _COMPOUND_CHEVRON_CHARS:
        return True
    al = (attrs.get("aria-label") or "").lower()
    if al and re.search(r"expand|toggle|collapse|more", al):
        return True
    return False


def _entry_pixel_rect(entry: dict) -> tuple[float, float, float, float] | None:
    """Return (x0, y0, x1, y1) in CSS-pixel space, or None when missing."""
    bounds = entry.get("bounds") or {}
    if not bounds:
        return None
    try:
        x0 = float(bounds.get("x") or 0.0)
        y0 = float(bounds.get("y") or 0.0)
        w = float(bounds.get("width") or 0.0)
        h = float(bounds.get("height") or 0.0)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return (x0, y0, x0 + w, y0 + h)


def _rect_iou(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    """Standard IoU on two rectangles."""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0 = max(ax0, bx0); iy0 = max(ay0, by0)
    ix1 = min(ax1, bx1); iy1 = min(ay1, by1)
    iw = max(0.0, ix1 - ix0); ih = max(0.0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0.0:
        return 0.0
    a_area = max(1.0, (ax1 - ax0) * (ay1 - ay0))
    b_area = max(1.0, (bx1 - bx0) * (by1 - by0))
    return inter / (a_area + b_area - inter)


def _resolve_aria_idref(
    target_id: str,
    selector_entries: list[dict],
) -> dict | None:
    """Find the selectorEntry whose attributes['id'] == target_id."""
    if not target_id:
        return None
    for entry in selector_entries:
        attrs = entry.get("attributes") or {}
        if (attrs.get("id") or "") == target_id:
            return entry
    return None


def _bbox_index_containing_entry(
    bboxes_rects: list[tuple[int, int, int, int, float]],
    entry_rect: tuple[float, float, float, float],
) -> int:
    """Return the index of a bbox whose rect contains entry_rect's centre.

    Picks the SMALLEST containing bbox (most specific). -1 when no
    containing bbox exists.
    """
    cx = (entry_rect[0] + entry_rect[2]) / 2.0
    cy = (entry_rect[1] + entry_rect[3]) / 2.0
    best = -1
    best_area = float("inf")
    for idx, (bx0, by0, bx1, by1, area) in enumerate(bboxes_rects):
        if bx0 <= cx <= bx1 and by0 <= cy <= by1 and area < best_area:
            best = idx
            best_area = area
    return best


def _enrich_bboxes_with_dom_metadata(
    resp: Any,
    selector_entries: list[dict] | None,
    image_w: int,
    image_h: int,
    dpr: float,
    task_instruction: str | None,
) -> int:
    """Populate DOM-derived fields on each bbox from selectorEntries.

    Mutates ``resp.bboxes`` in place. Returns count of bboxes touched.
    Run AFTER `_apply_compound_row_split` so chevron sub-bboxes
    injected by that helper are included in the enrichment pass.

    Fields populated when an underlying selectorEntry can be matched:
      * ``dom_index``        — selectorEntry's index for fallback clicks.
      * ``aria_expanded``    — current 'true'/'false'/'mixed' state.
      * ``is_disabled``      — disabled or aria-disabled.
      * ``is_active``        — aria-checked/pressed/selected/current.
      * ``group_label``      — aria-labelledby resolved text, when
                               not already in the bbox label.
      * ``aria_controls_v``  — V_n of the bbox this entry's
                               aria-controls points to.
      * ``parent_expand_v``  — V_n of an expand control whose
                               aria-controls points back at this bbox.

    Match policy: best-IoU between bbox pixel rect and selectorEntry
    pixel rect, with a label-similarity tie-break. IoU floor 0.10 to
    avoid spurious matches on dense pages.
    """
    if os.environ.get("BBOX_DOM_ENRICHMENT", "1") in ("0", "false", "no"):
        return 0
    if not resp or not getattr(resp, "bboxes", None):
        return 0
    if not selector_entries:
        return 0
    if image_w <= 0 or image_h <= 0:
        return 0

    dpr_eff = dpr if dpr and dpr > 0 else 1.0

    # Build the bbox pixel-rect cache once.
    bboxes = resp.bboxes
    bbox_pixel_rects: list[tuple[int, int, int, int, float] | None] = []
    for b in bboxes:
        try:
            x0, y0, x1, y1 = b.to_pixels(image_w, image_h, dpr=dpr_eff)
        except Exception:
            bbox_pixel_rects.append(None)
            continue
        area = max(1.0, (x1 - x0) * (y1 - y0))
        bbox_pixel_rects.append((x0, y0, x1, y1, area))

    # Build entry pixel-rect cache + entry-by-id lookup.
    entry_rects: list[tuple[float, float, float, float] | None] = []
    for entry in selector_entries:
        entry_rects.append(_entry_pixel_rect(entry))

    # Match each bbox to its best selectorEntry (best IoU).
    bbox_to_entry: dict[int, int] = {}
    used_entries: set[int] = set()
    for bi, brect in enumerate(bbox_pixel_rects):
        if brect is None:
            continue
        bb = (brect[0], brect[1], brect[2], brect[3])
        bbox_label = (getattr(bboxes[bi], "label", "") or "").strip().lower()
        best_ei = -1
        best_score = 0.0
        for ei, erect in enumerate(entry_rects):
            if erect is None or ei in used_entries:
                continue
            iou = _rect_iou(bb, erect)
            if iou < 0.10:
                continue
            # Light label tie-breaker: when labels overlap, bias up.
            entry_text = (selector_entries[ei].get("text") or "").strip().lower()
            entry_aria = ((selector_entries[ei].get("attributes") or {}).get("aria-label") or "").strip().lower()
            label_bonus = 0.0
            if bbox_label:
                if bbox_label == entry_text or bbox_label == entry_aria:
                    label_bonus = 0.15
                elif bbox_label in entry_text or entry_text in bbox_label:
                    label_bonus = 0.08
            score = iou + label_bonus
            if score > best_score:
                best_score = score
                best_ei = ei
        if best_ei >= 0:
            bbox_to_entry[bi] = best_ei
            used_entries.add(best_ei)

    # First pass — populate per-bbox attributes from the matched entry.
    touched = 0
    for bi, ei in bbox_to_entry.items():
        bbox = bboxes[bi]
        entry = selector_entries[ei]
        attrs = entry.get("attributes") or {}
        try:
            bbox.dom_index = entry.get("index")
            ae = attrs.get("aria-expanded")
            if ae in ("true", "false", "mixed"):
                bbox.aria_expanded = ae
            disabled_attr = attrs.get("disabled")
            adis = (attrs.get("aria-disabled") or "").lower() == "true"
            bbox.is_disabled = bool(
                (disabled_attr is not None and disabled_attr != "false")
                or adis
            )
            active_signals = []
            for k in ("aria-checked", "aria-pressed", "aria-selected"):
                v = (attrs.get(k) or "").lower()
                if v == "true":
                    active_signals.append(k)
            ac = (attrs.get("aria-current") or "").lower()
            if ac and ac != "false":
                active_signals.append("aria-current")
            bbox.is_active = bool(active_signals)
            # Group label via aria-labelledby — resolve to the
            # referenced element's text. Only attach when it adds
            # info (not already in the bbox label itself).
            labelled_by = attrs.get("aria-labelledby") or ""
            if labelled_by:
                ref = _resolve_aria_idref(labelled_by.split()[0], selector_entries)
                if ref:
                    grp = (ref.get("text") or "").strip()
                    if grp and grp.lower() not in (bbox.label or "").lower():
                        bbox.group_label = grp[:80]
            touched += 1
        except Exception:
            continue

    # Second pass — wire aria_controls_v and parent_expand_v.
    # aria-controls points an expand button at the element it controls.
    # Convert that to V_n by:
    #   1. find the controlled selectorEntry (by id)
    #   2. find the bbox that bbox-contains the controlled entry
    # The reverse direction (parent_expand_v on the controlled bbox)
    # falls out of the same pair: the expander's bbox V_n is the parent.
    # V_n is 1-based and uses the rendering rank, NOT the raw bbox order.
    try:
        ranked = sorted(bboxes, key=_replicate_rank_for_enrichment)
        # Map bbox identity -> V_n.
        identity_to_v: dict[int, int] = {
            id(b): i for i, b in enumerate(ranked, 1)
        }
    except Exception:
        identity_to_v = {id(b): i for i, b in enumerate(bboxes, 1)}

    for bi, ei in bbox_to_entry.items():
        bbox = bboxes[bi]
        attrs = selector_entries[ei].get("attributes") or {}
        controls_id = (attrs.get("aria-controls") or "").strip()
        if not controls_id:
            continue
        # First ID in a space-separated list is the controlled element.
        target_id = controls_id.split()[0]
        target_entry = _resolve_aria_idref(target_id, selector_entries)
        if target_entry is None:
            continue
        target_rect = _entry_pixel_rect(target_entry)
        if target_rect is None:
            continue
        # Find the bbox whose rect contains the target's centre.
        valid_rects = [
            r for r in bbox_pixel_rects if r is not None
        ]
        # Build a parallel index map (skip Nones).
        idx_map = [
            i for i, r in enumerate(bbox_pixel_rects) if r is not None
        ]
        target_local_idx = _bbox_index_containing_entry(
            valid_rects, target_rect,
        )
        if target_local_idx < 0:
            continue
        controlled_bi = idx_map[target_local_idx]
        if controlled_bi == bi:
            # The expand control and the controlled element matched
            # the same bbox — no-op (vision merged them; the compound
            # split helper handles that case separately).
            continue
        # Wire both directions.
        try:
            bbox.aria_controls_v = identity_to_v.get(id(bboxes[controlled_bi]))
            bboxes[controlled_bi].parent_expand_v = identity_to_v.get(id(bbox))
        except Exception:
            pass

    return touched


def _apply_just_toggled_marker(
    resp: Any,
    state: Any,
) -> int:
    """Stamp `just_toggled` on the bbox the brain just clicked, when
    the click flipped its is_active state.

    Reads `state.last_click_target_label` / `last_click_target_box_2d`
    / `last_click_target_active_state` (set by `register_click_attempt`
    just BEFORE the dispatch) and finds the same bbox in the new
    response by label match + box_2d proximity. When the new bbox's
    `is_active` differs from the recorded one, mark `just_toggled='on'`
    or `'off'`.

    The brain reads this in `as_brain_text` as e.g.
    `[V5] checkbox 'Samsung' (...) active=true just_toggled=on`,
    enabling the worker_hook's filter-toggle recovery hint and
    teaching the brain that re-clicking the same V_n undoes the
    accidental filter.

    Returns the count of bboxes touched (0 or 1).
    Safe no-op when:
      * `BBOX_JUST_TOGGLED_DETECT` env flag is disabled,
      * `state` has no recorded last-click target,
      * the new bbox's is_active didn't flip (legitimate fresh click),
      * no bbox in the new response matches the prior target.
    """
    if os.environ.get("BBOX_JUST_TOGGLED_DETECT", "1") in ("0", "false", "no"):
        return 0
    if not resp or not getattr(resp, "bboxes", None):
        return 0
    prior_label = (getattr(state, "last_click_target_label", "") or "").strip()
    prior_box = getattr(state, "last_click_target_box_2d", None)
    prior_active = getattr(state, "last_click_target_active_state", None)
    # We need at least the prior label OR the prior box to match by.
    # Prior active_state must be known for a flip to be meaningful.
    if prior_active is None:
        return 0
    if not prior_label and not prior_box:
        return 0

    prior_label_lc = prior_label.lower()
    # Find the closest matching bbox in the new response.
    # Strategy: label-exact wins (when label present); fallback to
    # the bbox whose box_2d centre is closest to the prior centre.
    best_idx = -1
    best_score = -1.0
    for idx, b in enumerate(resp.bboxes):
        b_label = (getattr(b, "label", "") or "").strip().lower()
        # Hard label match — most reliable.
        label_score = 0.0
        if prior_label_lc and b_label:
            if b_label == prior_label_lc:
                label_score = 1.0
            elif prior_label_lc in b_label or b_label in prior_label_lc:
                label_score = 0.5
        # Box-centre proximity (normalized space, max distance ~1414).
        box_score = 0.0
        if prior_box and len(prior_box) == 4:
            try:
                py0, px0, py1, px1 = prior_box
                pcx = (px0 + px1) / 2.0
                pcy = (py0 + py1) / 2.0
                ny0, nx0, ny1, nx1 = b.box_2d
                ncx = (nx0 + nx1) / 2.0
                ncy = (ny0 + ny1) / 2.0
                dist = ((ncx - pcx) ** 2 + (ncy - pcy) ** 2) ** 0.5
                # Closer = higher score; cap at 100 norm units (~10% viewport).
                if dist < 100:
                    box_score = max(0.0, 1.0 - dist / 100.0) * 0.5
            except (TypeError, ValueError):
                pass
        score = label_score + box_score
        if score > best_score:
            best_score = score
            best_idx = idx

    # Require a non-trivial match: either hard label hit or close box.
    if best_idx < 0 or best_score < 0.5:
        return 0
    target = resp.bboxes[best_idx]
    new_active = bool(getattr(target, "is_active", False))
    if bool(prior_active) == new_active:
        # No flip — either click missed or click on a non-toggle
        # element. Don't stamp.
        return 0
    target.just_toggled = "on" if new_active else "off"
    return 1


def _detect_misclick_flip(resp: Any, state: Any) -> int:
    """Surface a misclick advisory when the brain aimed at one bbox but
    a *different* bbox flipped active state instead.

    Complements `_apply_just_toggled_marker`: the marker stamps the
    expected-target bbox when its `is_active` flipped (good toggle).
    This detector handles the inverse — expected target did NOT flip,
    but some OTHER bbox did. That's the dropdown / checkbox misclick
    fingerprint we need to surface to the brain (and to flag the
    pending undo entry with `misclick_evidence` so a later
    `browser_undo_last_click` can target the actually-flipped bbox).

    Reads `state._prev_active_map` (populated by this same fn on each
    prior call) to know what each bbox's is_active was BEFORE this
    response. Cheap O(N+M) over the bbox lists.

    Returns the count of advisories appended (0 or 1; >1 flips collapse
    to a single `[MISCLICK_AMBIGUOUS]` advisory).

    Safe no-op when:
      * `BBOX_MISCLICK_DETECT` env flag is disabled,
      * no prior active map is available (first vision pass),
      * no click target was registered (state.last_click_target_label
        is empty — typically a non-click action),
      * prior_active is None (DOM/index/selector click without bbox),
      * the expected target's is_active flipped (not a misclick).
    """
    if os.environ.get("BBOX_MISCLICK_DETECT", "1") in ("0", "false", "no"):
        return 0
    if not resp or not getattr(resp, "bboxes", None):
        # Refresh prev_active_map even on empty response so a future
        # call doesn't read stale data — but use empty map.
        try:
            state._prev_active_map = {}
        except Exception:
            pass
        return 0

    prev_map: dict[tuple[str, int, int], bool] = (
        getattr(state, "_prev_active_map", None) or {}
    )
    prior_label = (getattr(state, "last_click_target_label", "") or "").strip()
    prior_box = getattr(state, "last_click_target_box_2d", None)
    prior_active = getattr(state, "last_click_target_active_state", None)

    def _key(label: str, box_2d: Any) -> tuple[str, int, int]:
        # Bucket box centroid into 50-norm-unit cells (~5% viewport).
        try:
            ny0, nx0, ny1, nx1 = box_2d
            cx = int(((nx0 + nx1) / 2.0) // 50)
            cy = int(((ny0 + ny1) / 2.0) // 50)
        except Exception:
            cx = cy = -1
        return ((label or "").strip().lower(), cx, cy)

    # Build NEW active map and compute flips against prev_map.
    new_map: dict[tuple[str, int, int], bool] = {}
    flipped: list[tuple[int, Any, bool, bool]] = []  # (idx, bbox, prev, new)
    for idx, b in enumerate(resp.bboxes):
        active = bool(getattr(b, "is_active", False))
        k = _key(getattr(b, "label", "") or "", getattr(b, "box_2d", None))
        new_map[k] = active
        prev = prev_map.get(k) if prev_map else None
        if prev is not None and prev != active:
            flipped.append((idx, b, prev, active))

    # Stash for the next call.
    try:
        state._prev_active_map = new_map
    except Exception:
        pass

    # No prior map (first vision pass) → nothing to compare.
    if not prev_map:
        return 0
    # No click target registered → not a click outcome — nothing to flag.
    if not prior_label and not prior_box:
        return 0
    if prior_active is None:
        # The click was DOM-index / selector / raw-coords with no
        # is_active recorded. Without a baseline we can't say which
        # flip is "intended" vs "unintended". Skip — the brain reads
        # just_toggled as the primary signal.
        return 0

    # Identify the expected target in the new response (same proximity
    # match _apply_just_toggled_marker uses).
    expected_idx = -1
    expected_score = -1.0
    prior_label_lc = prior_label.lower()
    for idx, b in enumerate(resp.bboxes):
        b_label = (getattr(b, "label", "") or "").strip().lower()
        label_score = 0.0
        if prior_label_lc and b_label:
            if b_label == prior_label_lc:
                label_score = 1.0
            elif prior_label_lc in b_label or b_label in prior_label_lc:
                label_score = 0.5
        box_score = 0.0
        if prior_box and len(prior_box) == 4:
            try:
                py0, px0, py1, px1 = prior_box
                pcx = (px0 + px1) / 2.0
                pcy = (py0 + py1) / 2.0
                ny0, nx0, ny1, nx1 = b.box_2d
                ncx = (nx0 + nx1) / 2.0
                ncy = (ny0 + ny1) / 2.0
                dist = ((ncx - pcx) ** 2 + (ncy - pcy) ** 2) ** 0.5
                if dist < 100:
                    box_score = max(0.0, 1.0 - dist / 100.0) * 0.5
            except (TypeError, ValueError):
                pass
        score = label_score + box_score
        if score > expected_score:
            expected_score = score
            expected_idx = idx

    expected_flipped = False
    if expected_idx >= 0:
        exp_b = resp.bboxes[expected_idx]
        exp_active = bool(getattr(exp_b, "is_active", False))
        if bool(prior_active) != exp_active:
            expected_flipped = True

    # Filter flips to OTHER bboxes (i.e., not the expected target).
    other_flips = [
        f for f in flipped
        if f[0] != expected_idx
    ]

    if expected_flipped:
        # Intended toggle. Even if other bboxes flipped (rare on dense
        # pages with concurrent state changes), the expected target
        # responded — leave the marker logic to handle it.
        return 0

    if not other_flips:
        # Expected didn't flip and nothing else did either. Click was
        # a no-op or hit a non-toggle target. Not our concern here.
        return 0

    advisories = getattr(state, "_misclick_advisory", None)
    if advisories is None:
        return 0

    # Compute 1-based V_n indices using the same bbox ordering the
    # brain sees. The bboxes list IS already in display order on the
    # vision response (bbox V1 == resp.bboxes[0], etc.).
    def _v_for(idx: int) -> int:
        return idx + 1

    expected_v = _v_for(expected_idx) if expected_idx >= 0 else None
    expected_label = (
        getattr(resp.bboxes[expected_idx], "label", "")
        if expected_idx >= 0 else prior_label
    ) or "?"

    if len(other_flips) == 1:
        flip_idx, flip_b, _, _ = other_flips[0]
        flip_v = _v_for(flip_idx)
        flip_label = (getattr(flip_b, "label", "") or "?")[:80]
        advisories.append(
            f"[MISCLICK_DETECTED] Aimed at "
            f"{('V' + str(expected_v)) if expected_v else 'target'} "
            f"({expected_label!r}) but V{flip_v} "
            f"({flip_label!r}) flipped active. Undo with "
            f"browser_undo_last_click."
        )
        # Mark the pending undo entry (if any) with misclick evidence
        # so the recovery tool can target the actually-flipped bbox.
        pending = getattr(state, "_pending_undo_entry", None)
        if isinstance(pending, dict):
            pending["misclick_flag"] = True
            pending["misclick_evidence"] = {
                "expected_v": expected_v,
                "flipped_v": flip_v,
                "flipped_label": flip_label,
                "flipped_box_2d": list(
                    getattr(flip_b, "box_2d", []) or []
                ) or None,
            }
        # If finalize already ran, also tag the top of the ring.
        ring = getattr(state, "_undo_ring", None)
        if isinstance(ring, list) and ring:
            top = ring[-1]
            if isinstance(top, dict) and not top.get("misclick_flag"):
                top["misclick_flag"] = True
                top["misclick_evidence"] = {
                    "expected_v": expected_v,
                    "flipped_v": flip_v,
                    "flipped_label": flip_label,
                    "flipped_box_2d": list(
                        getattr(flip_b, "box_2d", []) or []
                    ) or None,
                }
        return 1
    else:
        # Multiple flips. Don't pre-mark any single one — let the brain
        # inspect.
        names = ", ".join(
            f"V{_v_for(f[0])} ({(getattr(f[1], 'label', '') or '?')[:30]!r})"
            for f in other_flips[:3]
        )
        advisories.append(
            f"[MISCLICK_AMBIGUOUS] Aimed at "
            f"{('V' + str(expected_v)) if expected_v else 'target'} "
            f"({expected_label!r}); multiple bboxes flipped active "
            f"unexpectedly: {names}. Inspect the new state before "
            f"calling browser_undo_last_click."
        )
        return 1


def _replicate_rank_for_enrichment(b: Any) -> tuple[int, int, int, float]:
    """Mirror VisionResponse._rank used by as_brain_text/get_bbox.

    Kept inline so we don't import a private helper out of schemas.
    """
    role_in_scene = getattr(b, "role_in_scene", "") or ""
    if role_in_scene == "blocker":
        role_rank = 0
    elif role_in_scene == "target":
        role_rank = 1
    else:
        role_rank = 2
    try:
        confidence = float(getattr(b, "confidence", 0.5) or 0.5)
    except (TypeError, ValueError):
        confidence = 0.5
    return (
        role_rank,
        0 if getattr(b, "intent_relevant", False) else 1,
        0 if getattr(b, "clickable", False) else 1,
        -confidence,
    )


def _apply_compound_row_split(
    resp: Any,
    selector_entries: list[dict] | None,
    image_w: int,
    image_h: int,
    dpr: float,
    task_instruction: str | None,
) -> int:
    """Inject chevron sub-bboxes when vision merged a compound row.

    Mutates ``resp.bboxes`` in place. Returns the count of bboxes added.
    Safe no-op when:
      * the env flag ``BBOX_COMPOUND_ROW_SPLIT`` is disabled,
      * the response carries no bboxes or no usable image dims,
      * ``selector_entries`` is empty/missing,
      * no chevron-shaped DOM entry is enclosed by a row-shaped vision
        bbox that doesn't already have a sibling targeting just the
        chevron.
    """
    if os.environ.get("BBOX_COMPOUND_ROW_SPLIT", "1") in ("0", "false", "no"):
        return 0
    if not resp or not getattr(resp, "bboxes", None):
        return 0
    if not selector_entries:
        return 0
    if image_w <= 0 or image_h <= 0:
        return 0
    try:
        from vision_agent.schemas import BBox  # type: ignore[import-not-found]
    except ImportError:
        return 0

    dpr_eff = dpr if dpr and dpr > 0 else 1.0

    # Pre-compute pixel rects for each existing bbox so we don't pay
    # to_pixels() per chevron candidate.
    bbox_rects: list[tuple[int, int, int, int, float]] = []
    for b in resp.bboxes:
        try:
            x0, y0, x1, y1 = b.to_pixels(image_w, image_h, dpr=dpr_eff)
        except Exception:
            continue
        area = max(1, (x1 - x0) * (y1 - y0))
        bbox_rects.append((x0, y0, x1, y1, area))

    task_lc = (task_instruction or "").lower()
    task_tokens: list[str] = []
    if task_lc:
        task_tokens = [
            t for t in re.findall(r"\b[a-z]{4,}\b", task_lc)
            if t not in _COMPOUND_TASK_STOPWORDS
        ]

    added = 0
    for entry in selector_entries:
        attrs = entry.get("attributes") or {}
        text = entry.get("text") or ""
        bounds = entry.get("bounds") or {}
        if not bounds:
            continue
        try:
            cx0 = float(bounds.get("x") or 0.0)
            cy0 = float(bounds.get("y") or 0.0)
            cw = float(bounds.get("width") or 0.0)
            ch = float(bounds.get("height") or 0.0)
        except (TypeError, ValueError):
            continue
        if cw <= 0 or ch <= 0:
            continue
        if not _is_chevron_entry(attrs, text):
            continue
        cx1 = cx0 + cw
        cy1 = cy0 + ch
        chev_area = max(1.0, cw * ch)
        ccx = (cx0 + cx1) / 2.0
        ccy = (cy0 + cy1) / 2.0

        # Find a row-shaped parent bbox that contains this chevron.
        parent_idx = -1
        for idx, (bx0, by0, bx1, by1, area) in enumerate(bbox_rects):
            if not (bx0 <= ccx <= bx1 and by0 <= ccy <= by1):
                continue
            # Row shape: wide-ish, not too tall, and a few× larger than
            # the chevron itself (so we don't try to "split" a bbox
            # that's already targeting just the chevron).
            if (bx1 - bx0) < 60 or (by1 - by0) < 24 or (by1 - by0) > 120:
                continue
            if area < chev_area * 3:
                continue
            parent_idx = idx
            break
        if parent_idx < 0:
            continue

        # Skip when another existing bbox already covers ONLY the
        # chevron (vision did its job for this row already).
        already_split = False
        for idx, (bx0, by0, bx1, by1, area) in enumerate(bbox_rects):
            if idx == parent_idx:
                continue
            if (bx0 <= cx0 + 2 and by0 <= cy0 + 2
                    and bx1 + 2 >= cx1 and by1 + 2 >= cy1
                    and area < chev_area * 4):
                already_split = True
                break
        if already_split:
            continue

        parent_bbox = resp.bboxes[parent_idx]
        parent_label = (getattr(parent_bbox, "label", "") or "").strip()
        chev_label_attr = (attrs.get("aria-label") or "").strip()
        if chev_label_attr:
            new_label = chev_label_attr
        elif parent_label:
            new_label = f"Expand {parent_label}"
        else:
            new_label = "Expand row"

        # Convert CSS-px bounds back to box_2d normalized [0, 1000].
        # to_pixels does: scale = image_dim / dpr; px = norm/1000 * scale.
        # Inverse: norm = px * dpr / image_dim * 1000.
        def _norm_y(py: float) -> int:
            return max(0, min(1000, int(round(py * dpr_eff / image_h * 1000))))

        def _norm_x(px: float) -> int:
            return max(0, min(1000, int(round(px * dpr_eff / image_w * 1000))))

        ymin = _norm_y(cy0)
        ymax = _norm_y(cy1)
        xmin = _norm_x(cx0)
        xmax = _norm_x(cx1)
        if ymax <= ymin:
            ymax = min(1000, ymin + 1)
        if xmax <= xmin:
            xmax = min(1000, xmin + 1)

        # Mark as intent-relevant when the task names something the
        # parent label doesn't cover — strong signal the chevron's
        # sub-tree is what the user actually wants.
        intent_rel = False
        if task_tokens:
            parent_tokens = set(re.findall(r"\b[a-z]{4,}\b", parent_label.lower()))
            if any(t not in parent_tokens for t in task_tokens):
                intent_rel = True

        try:
            new_bbox = BBox(
                label=new_label[:120],
                box_2d=[ymin, xmin, ymax, xmax],
                clickable=True,
                role="button",
                confidence=0.7,
                intent_relevant=intent_rel,
                role_in_scene=getattr(parent_bbox, "role_in_scene", "unknown"),
                layer_id=getattr(parent_bbox, "layer_id", None),
            )
        except Exception:
            continue
        resp.bboxes.append(new_bbox)
        # Also extend the local rect cache so subsequent iterations of
        # the chevron loop see the just-added bbox and don't double-split.
        bbox_rects.append((int(cx0), int(cy0), int(cx1), int(cy1), chev_area))
        added += 1

    return added


def _read_image_dims(b64: str) -> tuple[int, int]:
    """Decode (width, height) from a base64-encoded screenshot.

    Used to denormalize Gemini's box_2d coords (in [0, 1000] space)
    against the actual screenshot dimensions before any click is
    dispatched. Returns (0, 0) if PIL isn't available or the bytes
    don't decode — the vision agent then falls back to showing
    normalized coords in brain text.
    """
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(base64.b64decode(b64)))
        return int(img.width), int(img.height)
    except Exception:
        return 0, 0


async def _push_vision_bboxes(
    session_id: str,
    resp: Any,
    *,
    url: str | None = None,
    latency_ms: int | None = None,
) -> None:
    """POST denormalized bboxes to the SuperBrowser server so live
    viewers can flash them on the screencast overlay.

    Fire-and-forget; never raises into the caller. Brain-text indices
    (`V_n`) come from the same ranking `as_brain_text()` uses so the
    overlay's labels match what the brain sees.

    Extra payload fields (all optional):
      - `url`          — the URL the screenshot was captured on. The UI
                          uses this to drop bboxes whose URL no longer
                          matches the current screencast frame.
      - `freshness`    — `fresh|uncertain|stale` from the vision model.
                          The UI dims the overlay when != "fresh".
      - `latencyMs`    — vision-agent round-trip. Surfaces in the UI as
                          debug info.
    """
    if not session_id:
        return
    iw, ih = getattr(resp, "image_width", 0), getattr(resp, "image_height", 0)
    if iw <= 0 or ih <= 0:
        return
    # Mirror the rank order of as_brain_text() so the overlay's V_n
    # labels line up with what the brain sees in tool output.
    ordered = sorted(
        getattr(resp, "bboxes", []),
        key=lambda b: (
            0 if getattr(b, "intent_relevant", False) else 1,
            0 if getattr(b, "clickable", False) else 1,
            -getattr(b, "confidence", 0.0),
        ),
    )
    payload_bboxes: list[dict[str, Any]] = []
    for i, b in enumerate(ordered, start=1):
        try:
            x0, y0, x1, y1 = b.to_pixels(iw, ih)
        except Exception:
            continue
        payload_bboxes.append({
            "x0": x0, "y0": y0, "x1": x1, "y1": y1,
            "label": getattr(b, "label", "")[:40],
            "role": getattr(b, "role", "other"),
            "clickable": bool(getattr(b, "clickable", False)),
            "intent_relevant": bool(getattr(b, "intent_relevant", False)),
            "index": i,
        })
    freshness = getattr(resp, "screenshot_freshness", "fresh") or "fresh"
    payload: dict[str, Any] = {
        "bboxes": payload_bboxes,
        "imageWidth": iw,
        "imageHeight": ih,
        "url": url or "",
        "freshness": freshness,
    }
    if latency_ms is not None:
        payload["latencyMs"] = int(latency_ms)
    # For T3 sessions, fan out to the local Python event bus so the T3
    # viewer at :3101 can paint the same overlays T1 shows. The TS
    # server POST still runs (404s cleanly for t3-* session IDs) so
    # the non-T3 path stays byte-identical.
    if session_id.startswith("t3-"):
        try:
            from superbrowser_bridge.antibot import t3_event_bus as _bus
            _bus.default().emit_vision_bboxes(
                session_id, payload_bboxes, iw, ih,
                url=url or "",
                freshness=freshness,
                latency_ms=latency_ms,
            )
        except Exception:
            pass
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/vision-bboxes",
                json=payload,
            )
    except Exception:
        # Best-effort — overlay is debug visualization, not load-bearing.
        pass


async def _push_vision_pending(session_id: str) -> None:
    """Tell live viewers a vision pass is in flight. The UI renders a
    transient "vision updating…" indicator; without it the overlay
    silently lags the action by one Gemini round-trip.

    Fire-and-forget. Never raises into the caller.
    """
    if not session_id:
        return
    payload = {"dispatchedAt": int(time.time() * 1000)}
    if session_id.startswith("t3-"):
        try:
            from superbrowser_bridge.antibot import t3_event_bus as _bus
            _bus.default().emit_vision_pending(session_id)
        except Exception:
            pass
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/vision-pending",
                json=payload,
            )
    except Exception:
        pass


def _schedule_vision_prefetch(
    state: "BrowserSessionState", session_id: str,
) -> "asyncio.Task[Any] | None":
    """Fire a background vision_agent.analyze() so the next
    `browser_screenshot` call finds cached bboxes instead of waiting 3-8s
    for Gemini.

    Returns the spawned task so callers can optionally wait for it with
    a budget via `_await_vision_prefetch`. Errors are swallowed inside
    `_run()`; the caller receives `None` when vision is disabled, the
    session is missing, or task creation failed.

    Called from the success path of mutating tools (click, type, scroll,
    navigate). Uses the same cache key as the sync path so the real
    screenshot call hits cache.
    """
    # Ablation toggle (default on). VISION_ASYNC_PREFETCH=0 disables the
    # background prefetch entirely (forcing the synchronous vision path) for the
    # "- asynchronous vision prefetch" row of the Table 1 ablations. Callers
    # already treat None as "no prefetch scheduled", so no caller change needed.
    if os.environ.get("VISION_ASYNC_PREFETCH", "1") in ("0", "false", "no"):
        return None
    try:
        from vision_agent import (  # type: ignore[import-not-found]
            dom_hash_of,
            get_vision_agent,
            vision_agent_enabled,
        )
        try:
            from vision_agent import (  # type: ignore[import-not-found]
                dom_text_hash_of,
            )
        except ImportError:
            dom_text_hash_of = None  # type: ignore[assignment]
    except ImportError:
        return None
    if not vision_agent_enabled() or get_vision_agent is None:
        return None
    if not session_id:
        return None

    # Announce pending vision so the UI can show a "vision updating…"
    # indicator. Fire-and-forget; failure must not block the prefetch.
    try:
        asyncio.create_task(_push_vision_pending(session_id))
    except Exception:
        pass

    async def _run() -> "Any":
        try:
            # v5 — when the most recent action was a navigation, ask
            # the TS server to wait for VISUAL stability (fonts /
            # images / layout-shift idle) before capturing the
            # screenshot. Avoids the cold-page bbox-above-text race.
            # State flag is one-shot: consume it here so subsequent
            # mid-session prefetches don't re-pay the cost.
            params: dict[str, str] = {"vision": "true", "bounds": "true"}
            if getattr(state, "_needs_visual_settle", False):
                params["settle"] = "true"
                try:
                    state._needs_visual_settle = False
                except Exception:
                    pass
            # Bump prefetch timeout when settle is active — the TS
            # waitForVisualStable can spend up to VISUAL_STABLE_MAX_MS
            # (default 1500ms) before /state returns.
            timeout_s = 18.0 if params.get("settle") else 15.0
            r = await _request_with_backoff(
                "GET",
                f"{SUPERBROWSER_URL}/session/{session_id}/state",
                params=params,
                timeout=timeout_s,
            )
            if r.status_code != 200:
                return None
            data = r.json()
            b64 = data.get("screenshot")
            if not b64:
                return None
            agent = get_vision_agent()
            img_w, img_h = _read_image_dims(b64)
            elements = data.get("elements", "")
            # Phase I: include iframe content signature in the cache key
            # so iframe-internal mutations (quiz question advancing,
            # calculator value updating) bust the vision cache.
            # Empty signature falls through to legacy behaviour for
            # non-iframe pages.
            iframe_sig = data.get("iframeSignature", "") or ""
            dh = (
                dom_hash_of(elements, iframe_sig)
                if dom_hash_of else ""
            )
            # Phase 1.2: viewport-aware secondary cache-key signal so
            # the same page at different scroll positions doesn't reuse
            # bboxes captured for the previous viewport.
            dth = ""
            if dom_text_hash_of is not None:
                try:
                    dth = dom_text_hash_of(
                        elements,
                        scroll_info=data.get("scrollInfo"),
                    )
                except Exception:
                    dth = ""
            dispatched = time.monotonic()
            # DPR: when the viewport runs at deviceScaleFactor > 1 the
            # screenshot is physical-pixel sized. Pass it through so
            # click dispatch can divide to land in CSS pixel space.
            try:
                dpr_val = float(data.get("devicePixelRatio") or 1.0)
            except (TypeError, ValueError):
                dpr_val = 1.0
            resp = await agent.analyze(
                screenshot_b64=b64,
                intent=state._last_intent or "observe page",
                session_id=session_id,
                url=data.get("url", "") or state.current_url,
                dom_hash=dh,
                dom_text_hash=dth,
                previous_summary=state._last_vision_summary or None,
                image_width=img_w,
                image_height=img_h,
                task_instruction=state.task_instruction or None,
            )
            resp.with_image_dims(img_w, img_h, dpr=dpr_val)
            # v2-C: post-vision DOM enrichment. When vision merged a
            # compound row (parent control + chevron in one bbox), the
            # selectorEntries from the same /state response give us the
            # chevron's exact bounds — inject a sub-bbox so the brain
            # has a V_n it can target directly. No-op when vision did
            # the split itself.
            sel_entries_pref = data.get("selectorEntries") or []
            try:
                _apply_compound_row_split(
                    resp,
                    sel_entries_pref,
                    img_w,
                    img_h,
                    dpr_val,
                    state.task_instruction,
                )
            except Exception as exc:
                print(f"  [compound_row_split prefetch failed: {exc}]")
            # Then attach DOM-derived metadata (parent_expand_v,
            # aria_expanded, dom_index, group_label, ...). Runs AFTER
            # the compound split so injected chevron sub-bboxes also
            # get enriched.
            try:
                _enrich_bboxes_with_dom_metadata(
                    resp,
                    sel_entries_pref,
                    img_w,
                    img_h,
                    dpr_val,
                    state.task_instruction,
                )
            except Exception as exc:
                print(f"  [dom_enrichment prefetch failed: {exc}]")
            # v4 C6 — stamp `just_toggled` on the bbox the brain just
            # clicked, when its is_active flipped relative to what was
            # recorded at click dispatch. Brain then sees e.g.
            # `active=true just_toggled=on` and knows re-clicking the
            # same V_n will UN-toggle.
            try:
                _apply_just_toggled_marker(resp, state)
            except Exception as exc:
                print(f"  [just_toggled prefetch failed: {exc}]")
            try:
                _detect_misclick_flip(resp, state)
            except Exception as exc:
                print(f"  [misclick_detect prefetch failed: {exc}]")
            state._last_vision_response = resp
            state._last_vision_summary = resp.summary
            state._last_vision_ts = time.time()
            state._last_vision_url = (data.get("url", "") or state.current_url or "")
            state._last_dom_hash = dh or state._last_dom_hash
            state.vision_calls += 1
            # Push the fresh bboxes to live viewers immediately —
            # without this, overlay only updates on the next
            # screenshot tool call, so the user sees bboxes lag by
            # one full action cycle. Fire-and-forget, non-fatal.
            try:
                latency_ms = int((time.monotonic() - dispatched) * 1000)
                await _push_vision_bboxes(
                    session_id, resp,
                    url=state._last_vision_url,
                    latency_ms=latency_ms,
                )
            except Exception:
                pass
            return resp
        except Exception as exc:
            print(f"  [vision prefetch failed: {exc}]")
            return None

    try:
        new_task = asyncio.create_task(_run())
    except Exception:
        return None
    # Phase 1.1: store the task on state so the NEXT mutating tool call
    # can wait for it via ensure_vision_synced(). Cancel any prior
    # in-flight prefetch — only one is meaningful at a time, and a
    # never-awaited older task is just wasted Gemini latency. Best
    # effort; if cancellation is too late, the older task will write
    # into _last_vision_response then the newer task overwrites, so
    # correctness is preserved.
    prev = state._pending_vision_task
    if prev is not None and not prev.done():
        try:
            prev.cancel()
        except Exception:
            pass
    state._pending_vision_task = new_task
    return new_task


async def _append_fresh_vision(
    task: "asyncio.Task[Any] | None",
    result: str,
    *,
    budget_ms: int | None = None,
    expected_label: str | None = None,
    pre_url: str | None = None,
    pre_dom_hash: str | None = None,
    state: "BrowserSessionState | None" = None,
) -> str:
    """Wait for the prefetched vision pass (up to the budget) and
    append a one-line brain-facing hint to `result` when it arrives.

    The hint lets the planner reason on the post-action screen state
    in the SAME tool response rather than waiting for the next
    screenshot call. If the vision pass didn't finish in time, the
    task keeps running in the background (shielded) and the overlay
    will update on the next push.

    Phase 3.3: when `expected_label` + `pre_url` + `pre_dom_hash` are
    supplied (the click_at tool fills these in), compare the post-click
    vision pass against them. If the label is STILL visible AND the
    URL/DOM didn't change, the click missed — surface
    `[click_missed:label_still_visible]` so the brain stops assuming
    success after a no-op click on a `pointer-events:none` overlay or
    a covered element. This converts a class of silent failures into
    explicit signals.
    """
    resp = await _await_vision_prefetch(task, budget_ms=budget_ms)
    if resp is None:
        return result
    summary = (getattr(resp, "summary", "") or "").strip()
    note_parts: list[str] = []
    if summary:
        note_parts.append(summary[:240])
        freshness = getattr(resp, "screenshot_freshness", "fresh") or "fresh"
        if freshness != "fresh":
            note_parts[-1] = f"{note_parts[-1]} [freshness={freshness}]"
    # Phase 3.3 click-hit verification.
    if expected_label and state is not None:
        try:
            label_lower = expected_label.strip().lower()
            relevant = (getattr(resp, "relevant_text", "") or "").lower()
            current_url = (state.current_url or "")
            same_url = (
                pre_url is not None
                and pre_url == current_url
            )
            same_dom = (
                pre_dom_hash is not None
                and pre_dom_hash == (state._last_dom_hash or "")
            )
            if (
                label_lower
                and label_lower in relevant
                and same_url
                and same_dom
            ):
                # Record the cursor failure so the script-lockout gate
                # counts this as a tried-and-failed cursor strategy.
                try:
                    state.record_cursor_failure(
                        strategy="click_at",
                        target=expected_label[:80],
                        reason="label_still_visible (no URL/DOM delta)",
                    )
                except Exception:
                    pass
                miss_note = (
                    f"[click_missed:label_still_visible expected="
                    f"{expected_label[:40]!r}] The clicked target is "
                    f"still visible on the page and neither the URL "
                    f"nor DOM hash changed — the click likely landed "
                    f"on a covered or pointer-events:none surface. Re-"
                    f"observe vision (pick a fresh V_n) before trying "
                    f"again with a different strategy."
                )
                note_parts.append(miss_note)
        except Exception:
            pass
    if not note_parts:
        return result
    sep = "" if result.endswith("\n") else "\n"
    return f"{result}{sep}[vision] {' | '.join(note_parts)}"


async def _await_vision_required(
    task: "asyncio.Task[Any] | None",
    timeout_ms: int | None = None,
) -> "Any":
    """Phase 1.1 hard sync. Block until `task` resolves or `timeout_ms`
    elapses. Default timeout is VISION_HARD_SYNC_TIMEOUT_MS (8000ms).

    Unlike `_await_vision_prefetch`, this is intended to be called from
    the START of a mutating tool to guarantee fresh state — not from the
    END to opportunistically attach a hint. On timeout the task is left
    running (shielded), but the caller is responsible for surfacing
    that timeout to the brain so it can retry rather than dispatch on
    cached vision.
    """
    if task is None:
        return None
    if task.done():
        try:
            return task.result()
        except Exception:
            return None
    if timeout_ms is None:
        try:
            timeout_ms = int(
                os.environ.get("VISION_HARD_SYNC_TIMEOUT_MS") or "8000"
            )
        except ValueError:
            timeout_ms = 8000
    if timeout_ms <= 0:
        return None
    try:
        return await asyncio.wait_for(
            asyncio.shield(task), timeout=timeout_ms / 1000.0,
        )
    except asyncio.TimeoutError:
        return None
    except Exception:
        return None


async def _await_vision_prefetch(
    task: "asyncio.Task[Any] | None",
    budget_ms: int | None = None,
) -> "Any":
    """Wait up to `budget_ms` for a prefetch task to complete.

    Returns the VisionResponse when the task finishes in time, otherwise
    None. On timeout the task is left running (shielded) so the
    background cache write + UI push still happen. Budget defaults to
    VISION_AWAIT_BUDGET_MS env var (fallback 2000 ms); 0 disables the
    wait and returns immediately.
    """
    if task is None:
        return None
    if budget_ms is None:
        try:
            budget_ms = int(
                os.environ.get("VISION_AWAIT_BUDGET_MS") or "2000"
            )
        except ValueError:
            budget_ms = 2000
    if budget_ms <= 0:
        return None
    try:
        return await asyncio.wait_for(
            asyncio.shield(task), timeout=budget_ms / 1000.0,
        )
    except asyncio.TimeoutError:
        return None
    except Exception:
        return None
