"""Perception fusion — merge vision bboxes with DOM-indexed clickables.

Vision and DOM each see different things:
  - Vision returns rich labelled bboxes (what Gemini *reads*), but caps the
    list around 10-25 and biases toward the page's primary region. Sidebar,
    toolbar, header-nav and action-cluster elements routinely get culled.
  - DOM-side `selectorEntries` (from src/browser/dom-scripts.ts) has EVERY
    indexed clickable with exact bounds + xpath, but no natural-language
    label understanding beyond `text` + attributes.

The brain delegates to browser tools through bbox indices, so "vision missed
it → brain can't reach it" is a real failure mode on tools-section-heavy
pages. This module produces a unified FusedPerception:

  - Normalize both streams to 0-1000 rects (vision's native `box_2d` space).
  - IoU-match vision bboxes ↔ DOM elements that likely describe the same
    target. Merge into `FusedElement(source="fused")`.
  - Keep orphan vision bboxes as `source="vision"`.
  - Recover orphan DOM clickables whose text matches the active subgoal
    precondition — they re-enter as `source="dom"`, meaning the validator
    can dispatch via xpath even though vision never saw them.

The fused view is re-built per `observation_token` and cached on the
session state so back-to-back tool dispatches within the same turn reuse
one fusion pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Optional


@dataclass
class Rect:
    """Normalized 0-1000 rect with the same orientation as vision box_2d."""

    ymin: int = 0
    xmin: int = 0
    ymax: int = 0
    xmax: int = 0

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.ymin, self.xmin, self.ymax, self.xmax)

    def is_empty(self) -> bool:
        return self.ymax <= self.ymin or self.xmax <= self.xmin

    def iou(self, other: "Rect") -> float:
        if self.is_empty() or other.is_empty():
            return 0.0
        y0 = max(self.ymin, other.ymin)
        x0 = max(self.xmin, other.xmin)
        y1 = min(self.ymax, other.ymax)
        x1 = min(self.xmax, other.xmax)
        if y1 <= y0 or x1 <= x0:
            return 0.0
        inter = (y1 - y0) * (x1 - x0)
        area_a = (self.ymax - self.ymin) * (self.xmax - self.xmin)
        area_b = (other.ymax - other.ymin) * (other.xmax - other.xmin)
        union = area_a + area_b - inter
        return inter / union if union > 0 else 0.0


@dataclass
class FusedElement:
    """Unified view of one clickable element.

    `source` describes which stream contributed:
      - "fused":  both vision and DOM — best case, richest data
      - "vision": vision only (DOM missed or not indexed in viewport)
      - "dom":    DOM only (vision culled this element — "tools section" case)
    """

    label: str
    source: str  # "vision" | "dom" | "fused"
    rect_norm: Rect
    score: float = 0.0  # fusion confidence (IoU × label overlap), or 1.0 for pure-source
    # Raw refs — duck-typed to avoid binding either schema at module scope.
    vision_bbox: Any = None  # nanobot.vision_agent.schemas.BBox | None
    dom_element: Any = None  # dict from SelectorEntry | None
    # Scene layer id from vision (L0_modal, L1_content, etc.); None when
    # the element came from DOM alone.
    scene_layer: Optional[str] = None
    # Pre-computed role — vision's canonical role, else coerced from DOM
    # tag/attributes. Used by validator role-matching.
    role: str = "other"

    @property
    def xpath(self) -> Optional[str]:
        if isinstance(self.dom_element, dict):
            return self.dom_element.get("xpath")
        return None

    @property
    def dom_index(self) -> Optional[int]:
        if isinstance(self.dom_element, dict):
            idx = self.dom_element.get("index")
            if idx is not None:
                try:
                    return int(idx)
                except (TypeError, ValueError):
                    return None
        return None

    def click_point_px(
        self,
        image_width: int,
        image_height: int,
        *,
        dpr: float = 1.0,
    ) -> tuple[int, int]:
        """Centre of the element in CSS pixels — ready for CDP dispatch."""
        if self.vision_bbox is not None and hasattr(self.vision_bbox, "center_pixels"):
            return self.vision_bbox.center_pixels(image_width, image_height, dpr=dpr)
        # Fall back to DOM-space: bounds are already CSS pixels (x, y, w, h).
        dom = self.dom_element
        if isinstance(dom, dict):
            bounds = dom.get("bounds") or {}
            x = float(bounds.get("x") or 0)
            y = float(bounds.get("y") or 0)
            w = float(bounds.get("width") or 0)
            h = float(bounds.get("height") or 0)
            return (int(x + w / 2), int(y + h / 2))
        # Last resort: derive from normalized rect assuming image dims are
        # the viewport. Caller should have supplied image dims; this keeps
        # the method total.
        if image_width > 0 and image_height > 0:
            cx = (self.rect_norm.xmin + self.rect_norm.xmax) / 2_000.0 * (
                image_width / max(dpr, 1e-6)
            )
            cy = (self.rect_norm.ymin + self.rect_norm.ymax) / 2_000.0 * (
                image_height / max(dpr, 1e-6)
            )
            return (int(cx), int(cy))
        return (0, 0)


# ---------------------------------------------------------------- helpers


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


_TOKEN_RE = re.compile(r"\b\w+\b")


def _tokens(s: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall((s or "").lower()) if t}


def _label_overlap(a: str, b: str) -> float:
    """0-1 token-overlap score between two labels (Jaccard-ish).

    Kept symmetric and cheap — same formula used by semantic_tools._score_bbox
    without the role-based boosts (those belong in the validator).
    """
    a_n, b_n = _normalize(a), _normalize(b)
    if not a_n or not b_n:
        return 0.0
    if a_n == b_n:
        return 1.0
    if a_n in b_n or b_n in a_n:
        return 0.85
    a_tok, b_tok = _tokens(a_n), _tokens(b_n)
    if not a_tok or not b_tok:
        return 0.0
    inter = a_tok & b_tok
    if not inter:
        return 0.0
    return len(inter) / max(len(a_tok), len(b_tok))


def _vision_bbox_rect(bbox: Any) -> Rect:
    raw = getattr(bbox, "box_2d", None)
    if not raw or not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return Rect()
    try:
        ymin, xmin, ymax, xmax = (int(v) for v in raw)
    except (TypeError, ValueError):
        return Rect()
    return Rect(ymin=ymin, xmin=xmin, ymax=ymax, xmax=xmax)


def _dom_entry_rect(entry: dict) -> Rect:
    """Normalize a DOM selectorEntry bounds to 0-1000 space.

    Bounds are emitted in viewport pixels with `vw`/`vh` alongside
    (see src/browser/dom-scripts.ts). If `vw`/`vh` are missing (older
    payloads), we can't normalize and return an empty rect — fusion
    will still surface the element via pure-DOM path, just without
    geometric match to vision.
    """
    bounds = entry.get("bounds") or {}
    if not isinstance(bounds, dict):
        return Rect()
    try:
        x = float(bounds.get("x") or 0)
        y = float(bounds.get("y") or 0)
        w = float(bounds.get("width") or 0)
        h = float(bounds.get("height") or 0)
        vw = float(bounds.get("vw") or 0)
        vh = float(bounds.get("vh") or 0)
    except (TypeError, ValueError):
        return Rect()
    if w <= 0 or h <= 0 or vw <= 0 or vh <= 0:
        return Rect()
    ymin = int(round(y / vh * 1000))
    xmin = int(round(x / vw * 1000))
    ymax = int(round((y + h) / vh * 1000))
    xmax = int(round((x + w) / vw * 1000))
    # Clamp to valid 0-1000 range even if the element is partially off-viewport.
    ymin = max(0, min(1000, ymin))
    xmin = max(0, min(1000, xmin))
    ymax = max(0, min(1000, ymax))
    xmax = max(0, min(1000, xmax))
    if ymax <= ymin:
        ymax = min(1000, ymin + 1)
    if xmax <= xmin:
        xmax = min(1000, xmin + 1)
    return Rect(ymin=ymin, xmin=xmin, ymax=ymax, xmax=xmax)


def _dom_entry_label(entry: dict) -> str:
    """Best-effort readable label from a DOM entry.

    Priority: visible text > aria-label > placeholder > title > tagName.
    """
    text = (entry.get("text") or "").strip()
    if text:
        return text[:120]
    attrs = entry.get("attributes") or {}
    if not isinstance(attrs, dict):
        attrs = {}
    for key in ("aria-label", "placeholder", "title", "alt", "name", "value"):
        v = attrs.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()[:120]
    tag = entry.get("tagName") or entry.get("tag") or "element"
    return str(tag)


def _dom_entry_role(entry: dict) -> str:
    """Coerce a DOM entry to a vision-canonical role keyword."""
    attrs = entry.get("attributes") or {}
    if not isinstance(attrs, dict):
        attrs = {}
    role = (entry.get("role") or attrs.get("role") or "").strip().lower()
    if role:
        return role
    tag = (entry.get("tagName") or entry.get("tag") or "").strip().lower()
    if tag in ("a",):
        return "link"
    if tag in ("button",):
        return "button"
    if tag in ("input", "textarea"):
        return "input"
    if tag in ("select",):
        return "combobox"
    return "other"


# ---------------------------------------------------------------- fusion


_IOU_THRESHOLD = 0.4
_LABEL_OVERLAP_THRESHOLD = 0.3


@dataclass
class FusedPerception:
    """Read-only snapshot of the fused view for one observation_token."""

    elements: list[FusedElement] = field(default_factory=list)
    observation_token: int = 0
    # Diagnostics — useful for validator telemetry and tests.
    vision_count: int = 0
    dom_count: int = 0
    fused_count: int = 0
    vision_only_count: int = 0
    dom_only_count: int = 0

    # ------------------------------------------------------------ builders

    @classmethod
    def build(
        cls,
        *,
        vision_bboxes: Iterable[Any] = (),
        dom_entries: Iterable[dict] = (),
        observation_token: int = 0,
        intent_labels: Iterable[str] = (),
    ) -> "FusedPerception":
        """Merge the two streams. ``intent_labels`` are the active
        subgoal's precondition labels + current intent — DOM clickables
        whose text matches one of them are promoted to ``source="dom"``
        even if vision didn't cover the area. That's the "tools section
        recovery" path.
        """
        vision_list = [b for b in vision_bboxes if b is not None]
        dom_list = [e for e in dom_entries if isinstance(e, dict)]
        intent_tokens: set[str] = set()
        for lbl in intent_labels:
            intent_tokens.update(_tokens(lbl))

        fused_elements: list[FusedElement] = []
        dom_claimed: set[int] = set()

        # Pass 1 — for each vision bbox, find the best-IoU DOM match.
        for vb in vision_list:
            v_rect = _vision_bbox_rect(vb)
            v_label = (getattr(vb, "label", "") or "").strip()
            best_j: Optional[int] = None
            best_iou: float = 0.0
            best_label_ov: float = 0.0
            for j, de in enumerate(dom_list):
                if j in dom_claimed:
                    continue
                d_rect = _dom_entry_rect(de)
                if d_rect.is_empty() or v_rect.is_empty():
                    continue
                iou = v_rect.iou(d_rect)
                if iou < _IOU_THRESHOLD:
                    continue
                ov = _label_overlap(v_label, _dom_entry_label(de))
                # Require BOTH a spatial AND a label signal — empty-label
                # DOM elements (icon-only buttons) still match via high
                # IoU since the label overlap check degrades gracefully.
                if ov < _LABEL_OVERLAP_THRESHOLD and iou < 0.7:
                    continue
                score = 0.6 * iou + 0.4 * ov
                if score > 0.6 * best_iou + 0.4 * best_label_ov:
                    best_j = j
                    best_iou = iou
                    best_label_ov = ov
            if best_j is not None:
                dom_claimed.add(best_j)
                fused_elements.append(FusedElement(
                    label=v_label or _dom_entry_label(dom_list[best_j]),
                    source="fused",
                    rect_norm=v_rect,
                    score=0.6 * best_iou + 0.4 * best_label_ov,
                    vision_bbox=vb,
                    dom_element=dom_list[best_j],
                    scene_layer=getattr(vb, "layer_id", None),
                    role=(getattr(vb, "role", "") or _dom_entry_role(dom_list[best_j])),
                ))
            else:
                fused_elements.append(FusedElement(
                    label=v_label,
                    source="vision",
                    rect_norm=v_rect,
                    score=1.0,
                    vision_bbox=vb,
                    dom_element=None,
                    scene_layer=getattr(vb, "layer_id", None),
                    role=(getattr(vb, "role", "") or "other"),
                ))

        # Pass 2 — DOM orphans. Surface two classes:
        #   (a) Any DOM clickable whose text overlaps intent_tokens —
        #       the "tools section recovery" path. These let the
        #       validator match a precondition label even when vision
        #       culled the bbox.
        #   (b) Everything else becomes available for explicit xpath
        #       lookups via resolve_by_xpath — but we DON'T add them
        #       to `elements` to avoid drowning the validator in noise
        #       when intent is absent. Keeping pass 2 narrow preserves
        #       the contract that iterating yields things the brain
        #       can plausibly reach.
        dom_only = 0
        for j, de in enumerate(dom_list):
            if j in dom_claimed:
                continue
            d_label = _dom_entry_label(de)
            d_tokens = _tokens(d_label)
            # Recovery criterion: any token overlap with intent labels,
            # OR an attributes hint (aria-label) that isn't covered by
            # any vision bbox.
            if intent_tokens and not (d_tokens & intent_tokens):
                continue
            d_rect = _dom_entry_rect(de)
            fused_elements.append(FusedElement(
                label=d_label,
                source="dom",
                rect_norm=d_rect,
                score=0.5,  # lower than vision/fused — "best guess"
                vision_bbox=None,
                dom_element=de,
                scene_layer=None,
                role=_dom_entry_role(de),
            ))
            dom_only += 1

        fused_cnt = sum(1 for e in fused_elements if e.source == "fused")
        return cls(
            elements=fused_elements,
            observation_token=int(observation_token or 0),
            vision_count=len(vision_list),
            dom_count=len(dom_list),
            fused_count=fused_cnt,
            vision_only_count=sum(1 for e in fused_elements if e.source == "vision"),
            dom_only_count=dom_only,
        )

    # ------------------------------------------------------------ queries

    def iter(self) -> Iterator[FusedElement]:
        return iter(self.elements)

    def resolve_by_bbox_index(self, index_1based: int) -> Optional[FusedElement]:
        """Lookup by the [V_n] index vision attached.

        We preserve the original vision ranking by walking the input
        order — the validator consumes the same ranking contract as
        `VisionResponse.get_bbox(n)`, so callers using `vision_index`
        continue to hit the same element.
        """
        if index_1based < 1:
            return None
        seen = 0
        for elem in self.elements:
            if elem.vision_bbox is None:
                continue
            seen += 1
            if seen == index_1based:
                return elem
        return None

    def resolve_by_label(self, label: str) -> Optional[FusedElement]:
        """Best-match by label — highest overlap wins."""
        label_n = _normalize(label)
        if not label_n:
            return None
        best: Optional[FusedElement] = None
        best_score = 0.0
        for elem in self.elements:
            score = _label_overlap(label, elem.label)
            if score > best_score:
                best_score = score
                best = elem
        # Threshold mirrors semantic_tools — anything below 0.5 is too
        # loose to act on without user clarification.
        return best if best_score >= 0.5 else None

    def resolve_by_xpath(self, xpath: str) -> Optional[FusedElement]:
        if not xpath:
            return None
        for elem in self.elements:
            if elem.xpath == xpath:
                return elem
        return None


def build_fused_perception(
    *,
    vision_response: Any,
    dom_entries: Optional[list[dict]],
    observation_token: int,
    intent_labels: Iterable[str] = (),
) -> FusedPerception:
    """Convenience wrapper: pull bboxes off a VisionResponse-like object."""
    bboxes: list[Any] = []
    if vision_response is not None:
        raw = getattr(vision_response, "bboxes", None)
        if raw is None and isinstance(vision_response, dict):
            raw = vision_response.get("bboxes")
        if raw:
            bboxes = [b for b in raw if b is not None]
    return FusedPerception.build(
        vision_bboxes=bboxes,
        dom_entries=dom_entries or (),
        observation_token=observation_token,
        intent_labels=intent_labels,
    )


# ---------------------------------------------------------------- blocker detection


_DISMISS_VERB_RE = re.compile(
    r"\b(continue|accept|agree|got\s*it|okay?|close|not\s*now|skip|"
    r"confirm|dismiss|allow|proceed|understand|no\s+thanks)\b",
    re.IGNORECASE,
)


@dataclass
class BlockerInfo:
    """A detected "wall" the brain must address before pursuing the task.

    `dismiss_hint` is the best-guess label text of the button that will
    remove the wall. `source` names which detection path fired — useful
    for telemetry + tests. `reason` carries any extra signal string the
    detector collected (flag names, page_type, bbox count) so downstream
    captions can explain *why* we think this is a blocker.
    """

    source: str  # "scene" | "flags" | "page_type" | "sparse_heuristic"
    dismiss_hint: str
    layer_id: Optional[str] = None
    reason: str = ""


_BLOCKER_PAGE_TYPES: frozenset[str] = frozenset({
    "error_page", "captcha_challenge", "login_wall",
})


def _sniff_dismiss_label(bboxes: list[Any]) -> str:
    """Pick the first bbox label that reads like a dismiss button."""
    for b in bboxes:
        label = (getattr(b, "label", "") or "").strip()
        if not label:
            continue
        if _DISMISS_VERB_RE.search(label):
            return label[:80]
    return ""


def detect_active_blocker(vision_resp: Any) -> Optional[BlockerInfo]:
    """Best-effort "is there a wall on the page right now?" check.

    Ordered so the strongest signal wins:
      1. scene.active_blocker_layer_id — Gemini told us explicitly.
      2. flags.modal_open / login_wall / error_banner / captcha_present
         — structured booleans, reliable when set.
      3. page_type in {error_page, captcha_challenge, login_wall} —
         classification was confident enough to name the page.
      4. Sparse bbox list (≤ 3) whose labels include a dismiss verb —
         a full-viewport error/consent/block page that *looks* like
         content to vision but is clearly a wall.

    Returns None when none of the above fires. A caller that wants
    "is the brain currently hallucinating around a wall?" should check
    this against the proposed action's intent / resolved target label.
    """
    if vision_resp is None:
        return None

    bboxes = list(getattr(vision_resp, "bboxes", None) or [])

    # 1) Scene graph — authoritative.
    scene = getattr(vision_resp, "scene", None)
    if scene is not None:
        blocker_id = getattr(scene, "active_blocker_layer_id", None)
        if blocker_id:
            scene_hint = ""
            for layer in getattr(scene, "layers", []) or []:
                if getattr(layer, "id", None) == blocker_id:
                    scene_hint = (getattr(layer, "dismiss_hint", "") or "").strip()
                    break
            hint = scene_hint or _sniff_dismiss_label(bboxes)
            if hint:
                return BlockerInfo(
                    source="scene",
                    dismiss_hint=hint[:80],
                    layer_id=str(blocker_id),
                    reason="active_blocker_layer_id",
                )

    # 2) Flag bundle.
    flags = getattr(vision_resp, "flags", None)
    flag_signals: list[str] = []
    if flags is not None:
        if getattr(flags, "modal_open", False):
            flag_signals.append("modal_open")
        if getattr(flags, "login_wall", False):
            flag_signals.append("login_wall")
        if getattr(flags, "captcha_present", False):
            flag_signals.append("captcha_present")
        if getattr(flags, "error_banner", None):
            flag_signals.append("error_banner")

    # 3) page_type classification.
    page_type = (getattr(vision_resp, "page_type", "") or "").strip().lower()
    page_signal = page_type if page_type in _BLOCKER_PAGE_TYPES else ""

    if flag_signals or page_signal:
        hint = _sniff_dismiss_label(bboxes)
        if hint:
            return BlockerInfo(
                source="page_type" if page_signal else "flags",
                dismiss_hint=hint,
                reason=",".join(flag_signals + ([page_signal] if page_signal else [])),
            )

    # 4) Sparse-page heuristic: full-viewport walls (geo-blocks, "just
    # a moment", "you are offline") emit very few bboxes and one of
    # them is usually the continue/ok/close button. If vision returned
    # ≤ 3 bboxes and one looks like a dismiss, call it a blocker even
    # without flags/page_type corroboration.
    if len(bboxes) <= 3:
        hint = _sniff_dismiss_label(bboxes)
        if hint:
            return BlockerInfo(
                source="sparse_heuristic",
                dismiss_hint=hint,
                reason=f"sparse_bboxes={len(bboxes)}",
            )

    return None


__all__ = [
    "Rect",
    "FusedElement",
    "FusedPerception",
    "build_fused_perception",
    "BlockerInfo",
    "detect_active_blocker",
    "_label_overlap",
]
