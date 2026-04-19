"""Typed schemas for the vision preprocessor response.

The brain-facing shape is deliberately flat and small:
  - `summary`        — 1-3 sentence prose describing what's on screen
  - `relevant_text`  — visible text the brain may need to reason about
  - `bboxes`         — list of interactive / notable regions with coords
  - `flags`          — page-state booleans the brain checks cheaply

Bbox coordinates use Gemini's normalized `box_2d` format:
  [ymin, xmin, ymax, xmax] integers in [0, 1000], top-left origin,
  measured against the screenshot dimensions.

Gemini is trained natively in this space and is dramatically more
accurate here than at arbitrary absolute pixel resolutions. Denormalize
to CSS pixels via `BBox.to_pixels(image_w, image_h)` at the consumer
side — `VisionResponse` carries the source image dimensions so brain-
text rendering and downstream click dispatch produce sub-pixel-correct
viewport coordinates.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator


# Canonical role vocabulary the brain-facing renderer + captcha solver
# care about. The schema accepts ANY string and coerces to this set —
# Gemini occasionally invents roles like "cookie_banner" or "modal_button"
# that we'd rather map to "other" than fail validation over.
BBoxRole = Literal[
    "button",
    "link",
    "input",
    "checkbox",
    "captcha_tile",
    "captcha_widget",
    "slider_handle",
    "image",
    "text_block",
    "other",
]
_ALLOWED_ROLES = set(BBoxRole.__args__)  # type: ignore[attr-defined]


def _coerce_role(value: object) -> str:
    if not isinstance(value, str):
        return "other"
    v = value.strip().lower().replace("-", "_").replace(" ", "_")
    if v in _ALLOWED_ROLES:
        return v
    aliases = {
        "btn": "button",
        "anchor": "link",
        "a": "link",
        "textbox": "input",
        "text_input": "input",
        "tile": "captcha_tile",
        "slider": "slider_handle",
        "handle": "slider_handle",
        "widget": "captcha_widget",
        "img": "image",
        "heading": "text_block",
        "label": "text_block",
        "text": "text_block",
    }
    return aliases.get(v, "other")


class BBox(BaseModel):
    """One visible region on the page, usually clickable.

    Coordinates use Gemini's normalized `box_2d` format:
      [ymin, xmin, ymax, xmax] in [0, 1000], top-left origin.

    Use `to_pixels(image_w, image_h)` to denormalize to CSS pixels.
    """

    model_config = ConfigDict(extra="ignore")

    label: str = Field(default="", description="Short label — visible text or ARIA name.")
    box_2d: list[int] = Field(
        default_factory=lambda: [0, 0, 0, 0],
        description="[ymin, xmin, ymax, xmax] normalized to [0, 1000].",
    )
    clickable: bool = Field(default=False)
    role: str = Field(
        default="other",
        description="Coarse element kind. Coerced to the canonical set.",
    )
    confidence: float = Field(default=0.5)
    intent_relevant: bool = Field(default=False)

    @field_validator("box_2d", mode="before")
    @classmethod
    def _coerce_box(cls, v: object) -> list[int]:
        # Accept tuple/list of 4 numbers; clamp each to [0, 1000].
        if not isinstance(v, (list, tuple)) or len(v) != 4:
            return [0, 0, 0, 0]
        out: list[int] = []
        for x in v:
            try:
                i = int(round(float(x)))
            except (TypeError, ValueError):
                i = 0
            out.append(max(0, min(1000, i)))
        ymin, xmin, ymax, xmax = out
        # Swap if reversed — models occasionally emit [ymax, xmax, ymin, xmin].
        if ymax < ymin:
            ymin, ymax = ymax, ymin
        if xmax < xmin:
            xmin, xmax = xmax, xmin
        return [ymin, xmin, ymax, xmax]

    @field_validator("role", mode="before")
    @classmethod
    def _canonicalize_role(cls, v: object) -> str:
        return _coerce_role(v)

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, v: object) -> float:
        if isinstance(v, str):
            s = v.strip().lower()
            m = {"high": 0.9, "medium": 0.6, "med": 0.6, "low": 0.3, "none": 0.0}
            if s in m:
                return m[s]
            try:
                f = float(s)
            except ValueError:
                return 0.5
        else:
            try:
                f = float(v)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return 0.5
        if f > 1.0:
            f = f / 100.0
        return max(0.0, min(1.0, f))

    @field_validator("label", mode="before")
    @classmethod
    def _coerce_label(cls, v: object) -> str:
        if v is None:
            return ""
        return str(v)[:200]

    @field_validator("clickable", "intent_relevant", mode="before")
    @classmethod
    def _coerce_bool(cls, v: object) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ("true", "yes", "1", "t", "y")
        if isinstance(v, (int, float)):
            return bool(v)
        return False

    def to_pixels(self, image_w: int, image_h: int) -> tuple[int, int, int, int]:
        """Denormalize box_2d to CSS pixel rect (x0, y0, x1, y1)."""
        ymin, xmin, ymax, xmax = self.box_2d
        x0 = int(round(xmin / 1000.0 * image_w))
        y0 = int(round(ymin / 1000.0 * image_h))
        x1 = int(round(xmax / 1000.0 * image_w))
        y1 = int(round(ymax / 1000.0 * image_h))
        # Guarantee non-empty box even if model emitted ymin==ymax.
        if x1 <= x0:
            x1 = x0 + 1
        if y1 <= y0:
            y1 = y0 + 1
        return x0, y0, x1, y1

    def center_pixels(self, image_w: int, image_h: int) -> tuple[int, int]:
        x0, y0, x1, y1 = self.to_pixels(image_w, image_h)
        return ((x0 + x1) // 2, (y0 + y1) // 2)


class PageFlags(BaseModel):
    """Boolean page-state signals the brain checks before deciding its next move."""

    model_config = ConfigDict(extra="ignore")

    captcha_present: bool = False
    captcha_type: Optional[str] = Field(default=None)
    captcha_widget_bbox: Optional[BBox] = None
    modal_open: bool = False
    error_banner: Optional[str] = Field(default=None)
    loading: bool = False
    login_wall: bool = Field(default=False)

    @field_validator(
        "captcha_present", "modal_open", "loading", "login_wall",
        mode="before",
    )
    @classmethod
    def _coerce_bool(cls, v: object) -> bool:
        if isinstance(v, bool):
            return v
        if v is None:
            return False
        if isinstance(v, str):
            return v.strip().lower() in ("true", "yes", "1", "t", "y")
        if isinstance(v, (int, float)):
            return bool(v)
        return False

    @field_validator("captcha_type", "error_banner", mode="before")
    @classmethod
    def _coerce_optional_str(cls, v: object) -> Optional[str]:
        if v is None:
            return None
        if isinstance(v, bool):
            return None
        if isinstance(v, str):
            s = v.strip()
            if not s or s.lower() in ("none", "null", "false"):
                return None
            return s[:500]
        return str(v)[:500]


class SuggestedAction(BaseModel):
    """An action the vision agent recommends based on the page state.

    Unlike bboxes (which describe what's visible), suggested_actions say
    what to DO — e.g., "dismiss cookie banner at bbox V3 first".
    """

    model_config = ConfigDict(extra="ignore")

    action: str = Field(
        default="wait",
        description="click|type|scroll|dismiss|wait|navigate",
    )
    target_bbox_index: Optional[int] = Field(
        default=None,
        description="0-based index into the bboxes array.",
    )
    description: str = Field(default="")
    priority: int = Field(default=3, ge=1, le=3)

    @field_validator("action", mode="before")
    @classmethod
    def _coerce_action(cls, v: object) -> str:
        if not isinstance(v, str):
            return "wait"
        s = v.strip().lower()
        valid = {"click", "type", "scroll", "dismiss", "wait", "navigate"}
        return s if s in valid else "wait"

    @field_validator("priority", mode="before")
    @classmethod
    def _coerce_priority(cls, v: object) -> int:
        try:
            p = int(v)
        except (TypeError, ValueError):
            return 3
        return max(1, min(3, p))


class VisionResponse(BaseModel):
    """What the VisionAgent returns to the Python bridge."""

    model_config = ConfigDict(extra="ignore")

    summary: str = Field(default="")
    relevant_text: str = Field(default="")
    page_type: str = Field(
        default="other",
        description=(
            "Inferred page type — one of captcha_challenge, login_form, "
            "signup_form, search_results, product_listing, product_detail, "
            "checkout_form, cart, home_landing, article, map_or_booking, "
            "dashboard, error_page, other. Used to bias bbox selection."
        ),
    )
    bboxes: list[BBox] = Field(default_factory=list)
    flags: PageFlags = Field(default_factory=PageFlags)
    suggested_actions: list[SuggestedAction] = Field(default_factory=list)
    changes_from_previous: str = Field(
        default="",
        description="What changed since the previous screenshot.",
    )
    intent: str = Field(default="observe")
    cached: bool = False
    duration_ms: int = 0
    tokens_used: Optional[int] = None
    model: Optional[str] = None
    provider: Optional[str] = None

    # Source image dimensions used to denormalize box_2d to CSS pixels.
    # Set via `with_image_dims()` after parsing. PrivateAttr keeps these
    # out of the validation/serialization surface.
    _image_width: int = PrivateAttr(default=0)
    _image_height: int = PrivateAttr(default=0)

    def with_image_dims(self, width: int, height: int) -> "VisionResponse":
        self._image_width = int(width)
        self._image_height = int(height)
        return self

    @property
    def image_width(self) -> int:
        return self._image_width

    @property
    def image_height(self) -> int:
        return self._image_height

    @field_validator("summary", "relevant_text", "intent", mode="before")
    @classmethod
    def _coerce_str(cls, v: object) -> str:
        if v is None:
            return ""
        if isinstance(v, list):
            return " ".join(str(x) for x in v)[:4000]
        return str(v)[:4000]

    @field_validator("page_type", mode="before")
    @classmethod
    def _coerce_page_type(cls, v: object) -> str:
        if not isinstance(v, str):
            return "other"
        s = v.strip().lower().replace("-", "_").replace(" ", "_")
        allowed = {
            "captcha_challenge", "login_form", "signup_form", "search_results",
            "product_listing", "product_detail", "checkout_form", "cart",
            "home_landing", "article", "map_or_booking", "dashboard",
            "error_page", "other",
        }
        return s if s in allowed else "other"

    @field_validator("bboxes", mode="before")
    @classmethod
    def _coerce_bboxes(cls, v: object) -> list:
        if v is None:
            return []
        if isinstance(v, (dict, BaseModel)):
            return [v]
        if isinstance(v, list):
            return [item for item in v if isinstance(item, (dict, BaseModel))]
        return []

    @field_validator("suggested_actions", mode="before")
    @classmethod
    def _coerce_actions(cls, v: object) -> list:
        if v is None:
            return []
        if isinstance(v, (dict, BaseModel)):
            return [v]
        if isinstance(v, list):
            return [item for item in v if isinstance(item, (dict, BaseModel))]
        return []

    @field_validator("changes_from_previous", mode="before")
    @classmethod
    def _coerce_changes(cls, v: object) -> str:
        if v is None:
            return ""
        return str(v)[:1000]

    def as_brain_text(self, max_bboxes: int = 30) -> str:
        """Render into a compact text block the nanobot brain consumes.

        Bboxes are ranked intent-relevant first, then clickable, then by
        confidence. The truncation cap stops a 100-element dashboard from
        blowing the brain's context window. Pixel coords are computed
        from `box_2d` using the attached image dimensions; if dims are
        zero (test fixture without an image), normalized 0-1000 coords
        are shown instead so the brain can still ground.
        """
        header = (
            f"[VISION  intent={self.intent}  page_type={self.page_type}  "
            f"cached={str(self.cached).lower()}  "
            f"model={self.model or '?'}  dur={self.duration_ms}ms]"
        )
        flags = self.flags
        flag_bits = [
            f"captcha={flags.captcha_type or 'none'}",
            f"modal={str(flags.modal_open).lower()}",
            f"error={flags.error_banner or 'none'}",
            f"loading={str(flags.loading).lower()}",
            f"login_wall={str(flags.login_wall).lower()}",
        ]
        flags_line = "Flags: " + "  ".join(flag_bits)

        def _rank(b: BBox) -> tuple[int, int, float]:
            return (
                0 if b.intent_relevant else 1,
                0 if b.clickable else 1,
                -b.confidence,
            )

        ordered = sorted(self.bboxes, key=_rank)[:max_bboxes]
        elements_lines: list[str] = []
        iw, ih = self._image_width, self._image_height
        for i, b in enumerate(ordered, start=1):
            marker = "  ← matches intent" if b.intent_relevant else ""
            if iw > 0 and ih > 0:
                x0, y0, x1, y1 = b.to_pixels(iw, ih)
                coord_text = f"({x0},{y0} → {x1},{y1})"
            else:
                ymin, xmin, ymax, xmax = b.box_2d
                coord_text = f"(box_2d=[{ymin},{xmin},{ymax},{xmax}])"
            elements_lines.append(
                f"  [V{i}] {b.role:<14s} "
                f"{b.label!r:<40s} "
                f"{coord_text}"
                f"{marker}"
            )
        truncated = (
            f"  … {len(self.bboxes) - max_bboxes} more bboxes truncated"
            if len(self.bboxes) > max_bboxes
            else ""
        )

        parts = [
            header,
            f"Summary: {self.summary}",
            flags_line,
        ]
        if self.changes_from_previous:
            parts.append(f"Changes: {self.changes_from_previous}")
        if self.relevant_text:
            parts.append(f"Visible text: {self.relevant_text}")
        if elements_lines:
            parts.append("Interactive elements:")
            parts.extend(elements_lines)
            if truncated:
                parts.append(truncated)
        if self.suggested_actions:
            parts.append("Suggested actions:")
            for sa in sorted(self.suggested_actions, key=lambda a: a.priority):
                target = f" -> bbox V{sa.target_bbox_index + 1}" if sa.target_bbox_index is not None else ""
                parts.append(f"  [P{sa.priority}] {sa.action}{target}: {sa.description}")
        # Captcha auto-escalation: if vision flagged a captcha, surface an
        # imperative block so the worker LLM doesn't try to click through it.
        if getattr(flags, "captcha_present", False):
            ct = flags.captcha_type or "unknown"
            parts.append(
                f"[CAPTCHA_DETECTED type={ct}] Call "
                f"browser_solve_captcha(method='auto') NOW — do NOT attempt "
                f"to click the captcha widget manually via the bboxes above. "
                f"If auto-solve fails, browser_ask_user for a human handoff."
            )
        return "\n".join(parts)

    def get_bbox(self, vision_index_1based: int) -> Optional[BBox]:
        """Look up a bbox by the [V_i] index used in `as_brain_text()`.

        The brain references bboxes as [V1], [V2], ... — same ranking as
        `as_brain_text()`. Mirror that ordering here so a downstream tool
        call like browser_click_at(bbox=V3) resolves to the same element
        the brain saw.
        """
        if vision_index_1based < 1:
            return None
        ordered = sorted(self.bboxes, key=lambda b: (
            0 if b.intent_relevant else 1,
            0 if b.clickable else 1,
            -b.confidence,
        ))
        idx = vision_index_1based - 1
        if idx >= len(ordered):
            return None
        return ordered[idx]
