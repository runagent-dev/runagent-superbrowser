"""Typed schemas for the vision preprocessor response.

The brain-facing shape is deliberately flat and small:
  - `summary`        — 1-3 sentence prose describing what's on screen
  - `relevant_text`  — visible text the brain may need to reason about
  - `bboxes`         — list of interactive / notable regions with coords
  - `flags`          — page-state booleans the brain checks cheaply

Coordinates are in CSS pixels of the rendered page (the same coord space
the existing `browser_click` tool accepts). Providers MUST return integer
pixel values — we clamp negatives and cast to int defensively on parse.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# Canonical role vocabulary the brain-facing renderer + captcha solver
# care about. The schema accepts ANY string and coerces to this set —
# cheap vision models (Gemini Flash especially) invent roles like
# "cookie_banner" or "modal_button" that we'd rather map to "other"
# than fail validation over.
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
    # Common aliases models emit — keep this small; anything else → other.
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
    """One visible region on the page, usually clickable."""

    # Accept whatever extra keys the model emits — don't fail on them.
    model_config = ConfigDict(extra="ignore")

    label: str = Field(default="", description="Short label — visible text or ARIA name.")
    x: int = Field(default=0)
    y: int = Field(default=0)
    w: int = Field(default=0)
    h: int = Field(default=0)
    clickable: bool = Field(default=False)
    role: str = Field(
        default="other",
        description="Coarse element kind. Coerced to the canonical set.",
    )
    confidence: float = Field(default=0.5)
    intent_relevant: bool = Field(default=False)

    @field_validator("x", "y", "w", "h", mode="before")
    @classmethod
    def _coerce_int(cls, v: object) -> int:
        try:
            i = int(round(float(v)))
        except (TypeError, ValueError):
            return 0
        return max(0, i)

    @field_validator("role", mode="before")
    @classmethod
    def _canonicalize_role(cls, v: object) -> str:
        return _coerce_role(v)

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, v: object) -> float:
        # Models sometimes return "high", "medium", "low" or 0-100 range.
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

    def center(self) -> tuple[int, int]:
        return (self.x + self.w // 2, self.y + self.h // 2)


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
            # Some models emit `error_banner: false` meaning "no banner".
            return None
        if isinstance(v, str):
            s = v.strip()
            if not s or s.lower() in ("none", "null", "false"):
                return None
            return s[:500]
        return str(v)[:500]


class VisionResponse(BaseModel):
    """What the VisionAgent returns to the Python bridge."""

    model_config = ConfigDict(extra="ignore")

    summary: str = Field(default="")
    relevant_text: str = Field(default="")
    bboxes: list[BBox] = Field(default_factory=list)
    flags: PageFlags = Field(default_factory=PageFlags)
    intent: str = Field(default="observe")
    cached: bool = False
    duration_ms: int = 0
    tokens_used: Optional[int] = None
    model: Optional[str] = None
    provider: Optional[str] = None

    @field_validator("summary", "relevant_text", "intent", mode="before")
    @classmethod
    def _coerce_str(cls, v: object) -> str:
        if v is None:
            return ""
        if isinstance(v, list):
            # Some models return `relevant_text` as a list of strings.
            return " ".join(str(x) for x in v)[:4000]
        return str(v)[:4000]

    @field_validator("bboxes", mode="before")
    @classmethod
    def _coerce_bboxes(cls, v: object) -> list[dict]:
        if v is None:
            return []
        if isinstance(v, dict):
            # Single bbox emitted instead of a list.
            return [v]
        if isinstance(v, list):
            # Drop non-dict entries rather than fail the whole response.
            return [item for item in v if isinstance(item, dict)]
        return []

    def as_brain_text(self, max_bboxes: int = 30) -> str:
        """Render into a compact text block the nanobot brain consumes.

        Bboxes are ranked intent-relevant first, then clickable, then by
        confidence. The truncation cap stops a 100-element dashboard from
        blowing the brain's context window.
        """
        header = (
            f"[VISION  intent={self.intent}  cached={str(self.cached).lower()}  "
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
            # Lower tuple sorts first: (!intent_relevant, !clickable, -conf)
            return (
                0 if b.intent_relevant else 1,
                0 if b.clickable else 1,
                -b.confidence,
            )

        ordered = sorted(self.bboxes, key=_rank)[:max_bboxes]
        elements_lines: list[str] = []
        for i, b in enumerate(ordered, start=1):
            marker = "  ← matches intent" if b.intent_relevant else ""
            elements_lines.append(
                f"  [V{i}] {b.role:<14s} "
                f"{b.label!r:<40s} "
                f"({b.x},{b.y} {b.w}x{b.h})"
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
        if self.relevant_text:
            parts.append(f"Visible text: {self.relevant_text}")
        if elements_lines:
            parts.append("Interactive elements:")
            parts.extend(elements_lines)
            if truncated:
                parts.append(truncated)
        return "\n".join(parts)
