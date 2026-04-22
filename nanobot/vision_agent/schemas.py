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
    role_in_scene: Literal[
        "blocker", "target", "chrome", "content", "unknown",
    ] = Field(
        default="unknown",
        description=(
            "What this bbox IS relative to the current task — `blocker` for "
            "cookie/consent buttons, `target` for the actual goal element, "
            "`chrome` for navigation/footer, `content` for article body, "
            "`unknown` when uncertain. Drives the action planner."
        ),
    )
    layer_id: Optional[str] = Field(
        default=None,
        description="ID of the SceneLayer this bbox belongs to. None when scene is absent.",
    )

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

    @field_validator("role_in_scene", mode="before")
    @classmethod
    def _coerce_role_in_scene(cls, v: object) -> str:
        if not isinstance(v, str):
            return "unknown"
        s = v.strip().lower().replace("-", "_").replace(" ", "_")
        valid = {"blocker", "target", "chrome", "content", "unknown"}
        if s in valid:
            return s
        aliases = {
            "overlay": "blocker",
            "modal": "blocker",
            "popup": "blocker",
            "dismiss": "blocker",
            "goal": "target",
            "main": "target",
            "nav": "chrome",
            "navigation": "chrome",
            "header": "chrome",
            "footer": "chrome",
            "body": "content",
            "article": "content",
        }
        return aliases.get(s, "unknown")

    @field_validator("layer_id", mode="before")
    @classmethod
    def _coerce_layer_id(cls, v: object) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s[:40] if s else None

    def to_pixels(
        self,
        image_w: int,
        image_h: int,
        *,
        dpr: float = 1.0,
    ) -> tuple[int, int, int, int]:
        """Denormalize box_2d to CSS pixel rect (x0, y0, x1, y1).

        `image_w` / `image_h` are the SCREENSHOT pixel dimensions (the
        physical PNG/JPEG resolution). `dpr` is the device pixel ratio
        of the viewport the screenshot was taken against.

        When the screenshot is captured at DPR > 1 (retina/HiDPI) the
        PNG dims are DPR × the CSS viewport. CDP `Input.dispatchMouseEvent`
        and JS `elementFromPoint` both expect CSS pixels, so we divide
        by DPR when denormalizing for click dispatch. Leave `dpr=1.0`
        for uses that genuinely want image-pixel coords (e.g. drawing
        overlays on the raw screenshot).
        """
        ymin, xmin, ymax, xmax = self.box_2d
        scale_w = image_w / max(dpr, 1e-6)
        scale_h = image_h / max(dpr, 1e-6)
        x0 = int(round(xmin / 1000.0 * scale_w))
        y0 = int(round(ymin / 1000.0 * scale_h))
        x1 = int(round(xmax / 1000.0 * scale_w))
        y1 = int(round(ymax / 1000.0 * scale_h))
        # Guarantee non-empty box even if model emitted ymin==ymax.
        if x1 <= x0:
            x1 = x0 + 1
        if y1 <= y0:
            y1 = y0 + 1
        return x0, y0, x1, y1

    def center_pixels(
        self,
        image_w: int,
        image_h: int,
        *,
        dpr: float = 1.0,
    ) -> tuple[int, int]:
        x0, y0, x1, y1 = self.to_pixels(image_w, image_h, dpr=dpr)
        return ((x0 + x1) // 2, (y0 + y1) // 2)


class SceneLayer(BaseModel):
    """A single visual layer in the page's current stack.

    Layers are ordered top-most first (painter order: layers[0] sits above
    layers[1]). A `blocks_interaction_below=true` layer is one the user
    must dismiss before elements in layers below can be reached — cookie
    consent walls, login modals, newsletter popups, captcha challenges.

    Emitted by the vision agent when it can distinguish a stacked scene;
    absent on flat content pages, which the client treats as a single
    implicit content layer.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default="L0_content")
    kind: Literal[
        "modal", "drawer", "toast", "banner", "sticky_header", "content",
    ] = Field(default="content")
    bbox: Optional[BBox] = Field(default=None)
    blocks_interaction_below: bool = Field(default=False)
    dismiss_hint: Optional[str] = Field(
        default=None,
        description=(
            "Label of the best-guess dismiss button inside this layer "
            "(e.g. 'Accept all', 'Close', 'Not now')."
        ),
    )

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_id(cls, v: object) -> str:
        if v is None:
            return "L0_content"
        s = str(v).strip()
        return s[:40] if s else "L0_content"

    @field_validator("kind", mode="before")
    @classmethod
    def _coerce_kind(cls, v: object) -> str:
        if not isinstance(v, str):
            return "content"
        s = v.strip().lower().replace("-", "_").replace(" ", "_")
        valid = {"modal", "drawer", "toast", "banner", "sticky_header", "content"}
        if s in valid:
            return s
        aliases = {
            "popup": "modal",
            "dialog": "modal",
            "overlay": "modal",
            "sidebar": "drawer",
            "notification": "toast",
            "cookie_banner": "banner",
            "consent": "banner",
            "header": "sticky_header",
        }
        return aliases.get(s, "content")

    @field_validator("blocks_interaction_below", mode="before")
    @classmethod
    def _coerce_blocks(cls, v: object) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ("true", "yes", "1", "t", "y")
        if isinstance(v, (int, float)):
            return bool(v)
        return False

    @field_validator("dismiss_hint", mode="before")
    @classmethod
    def _coerce_dismiss_hint(cls, v: object) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        if not s or s.lower() in ("none", "null"):
            return None
        return s[:120]


class SceneGraph(BaseModel):
    """The layered structure of the current page as the eyes see it."""

    model_config = ConfigDict(extra="ignore")

    layers: list[SceneLayer] = Field(
        default_factory=list,
        description="Painter order, top-most first.",
    )
    active_blocker_layer_id: Optional[str] = Field(
        default=None,
        description=(
            "The layer the user MUST interact with before the page below "
            "is reachable. Usually layers[0].id when layers[0].blocks_"
            "interaction_below. None if the scene is unblocked."
        ),
    )

    @field_validator("layers", mode="before")
    @classmethod
    def _coerce_layers(cls, v: object) -> list:
        if v is None:
            return []
        if isinstance(v, (dict, BaseModel)):
            return [v]
        if isinstance(v, list):
            return [item for item in v if isinstance(item, (dict, BaseModel))]
        return []

    @field_validator("active_blocker_layer_id", mode="before")
    @classmethod
    def _coerce_blocker_id(cls, v: object) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        if not s or s.lower() in ("none", "null"):
            return None
        return s[:40]

    def top_blocker(self) -> Optional[SceneLayer]:
        """Return the top-most layer that blocks interaction, or None."""
        for layer in self.layers:
            if layer.blocks_interaction_below:
                return layer
        return None


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


class NextAction(BaseModel):
    """The single next action the vision agent commits to, during captcha
    step-mode.

    Populated only when the caller asks for `solve_captcha_step` intent.
    The captcha loop consumes exactly one of these per iteration —
    click_tile drives a center-click at `target_bbox`, drag_slider starts
    a drag from `target_bbox` center toward the widget's right edge,
    submit clicks the verify button, done exits the loop, stuck
    escalates to human handoff.

    The model reasons about "what changed since my last click" via the
    `last_action` hint in the user prompt; `expect_change` describes what
    the model thinks should happen after this step so the loop can pick
    an appropriate wait strategy.
    """

    model_config = ConfigDict(extra="ignore")

    action_type: Literal[
        "click_tile", "drag_slider", "type_text", "submit", "done", "stuck",
    ] = Field(default="stuck")
    target_bbox: Optional[BBox] = Field(default=None)
    # For type_text only: the input field to type into. Vision reads the
    # distorted-word image, transcribes it into `type_value`, and gives
    # us the input bbox here so the loop can POST /type-at directly.
    target_input_bbox: Optional[BBox] = Field(default=None)
    type_value: str = Field(
        default="",
        description=(
            "For action_type=type_text: the exact string to type into "
            "target_input_bbox — usually vision's best-effort transcription "
            "of a distorted-word captcha image."
        ),
    )
    label: str = Field(default="", description="Human-readable target label.")
    reasoning: str = Field(default="", description="Short why-this-action text.")
    expect_change: Literal[
        "static", "new_tile", "widget_replace", "page_nav",
    ] = Field(default="static")

    @field_validator("action_type", mode="before")
    @classmethod
    def _coerce_action_type(cls, v: object) -> str:
        if not isinstance(v, str):
            return "stuck"
        s = v.strip().lower().replace("-", "_").replace(" ", "_")
        valid = {"click_tile", "drag_slider", "type_text", "submit", "done", "stuck"}
        if s in valid:
            return s
        aliases = {
            "click": "click_tile",
            "tile_click": "click_tile",
            "tap": "click_tile",
            "drag": "drag_slider",
            "slide": "drag_slider",
            "slider": "drag_slider",
            "type": "type_text",
            "fill": "type_text",
            "enter": "type_text",
            "verify": "submit",
            "finish": "done",
            "complete": "done",
            "give_up": "stuck",
            "bail": "stuck",
        }
        return aliases.get(s, "stuck")

    @field_validator("expect_change", mode="before")
    @classmethod
    def _coerce_expect_change(cls, v: object) -> str:
        if not isinstance(v, str):
            return "static"
        s = v.strip().lower().replace("-", "_").replace(" ", "_")
        valid = {"static", "new_tile", "widget_replace", "page_nav"}
        return s if s in valid else "static"

    @field_validator("label", "reasoning", mode="before")
    @classmethod
    def _coerce_short_str(cls, v: object) -> str:
        if v is None:
            return ""
        return str(v)[:400]

    @field_validator("type_value", mode="before")
    @classmethod
    def _coerce_type_value(cls, v: object) -> str:
        if v is None:
            return ""
        # Captcha answers are short — cap at 40 chars so a hallucinated
        # paragraph can't become a typed value.
        s = str(v).strip()
        return s[:40]


class DiffInfo(BaseModel):
    """Structured snapshot of what changed between two vision passes.

    Computed deterministically by the vision client after parsing —
    NOT emitted by the model. Lets the planner decide "did my last
    action actually do something?" without parsing prose.
    """

    model_config = ConfigDict(extra="ignore")

    bboxes_added: list[str] = Field(
        default_factory=list,
        description="Labels that appeared this pass that weren't present last pass.",
    )
    bboxes_removed: list[str] = Field(
        default_factory=list,
        description="Labels present last pass that are gone this pass.",
    )
    url_changed: bool = Field(default=False)
    modal_state: Literal["same", "opened", "closed"] = Field(
        default="same",
        description=(
            "Relative change to the top-most blocking layer: "
            "`opened` when a blocker appeared this pass, `closed` when "
            "one went away, `same` otherwise."
        ),
    )


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
    next_action: Optional[NextAction] = Field(
        default=None,
        description=(
            "Single-step action commitment for captcha step-mode. Populated "
            "only when intent is solve_captcha_step; null for every other "
            "intent so non-captcha flows stay unchanged."
        ),
    )
    changes_from_previous: str = Field(
        default="",
        description="What changed since the previous screenshot.",
    )
    diff_from_previous: Optional[DiffInfo] = Field(
        default=None,
        description=(
            "Structured diff computed client-side (not emitted by the "
            "model) comparing this pass's bboxes against the previous "
            "pass for the same session. None on the first pass. Gives "
            "the planner a boolean-friendly 'did my action work?' "
            "signal that doesn't require parsing prose."
        ),
    )
    screenshot_freshness: Literal["fresh", "uncertain", "stale"] = Field(
        default="fresh",
        description=(
            "Self-reported freshness of the screenshot. `fresh` = the image "
            "matches the Page URL and is fully rendered; `uncertain` = "
            "loading spinners or placeholders cover the content; `stale` = "
            "page chrome contradicts the URL. The bridge gates actions on "
            "this field — stale/uncertain frames trigger a re-capture "
            "rather than a click."
        ),
    )
    scene: Optional[SceneGraph] = Field(
        default=None,
        description=(
            "Layered structure of the page — modal/toast/banner/sticky_header/"
            "content layers, painter order top-most first. Absent when Gemini "
            "didn't emit one; the client derives a degenerate single-layer "
            "scene in that case so downstream planners never see a None."
        ),
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
    # Device-pixel-ratio of the viewport the screenshot was taken
    # against. 1.0 on standard viewports; >1 on retina/HiDPI. When set,
    # `BBox.to_pixels(..., dpr=dpr)` divides the denormalized coords so
    # CDP click dispatch lands in CSS pixel space. Default 1.0 keeps
    # existing call sites correct for the common DPR=1 config.
    _dpr: float = PrivateAttr(default=1.0)

    def with_image_dims(
        self,
        width: int,
        height: int,
        *,
        dpr: float | None = None,
    ) -> "VisionResponse":
        self._image_width = int(width)
        self._image_height = int(height)
        if dpr is not None:
            try:
                self._dpr = float(dpr) if float(dpr) > 0 else 1.0
            except (TypeError, ValueError):
                self._dpr = 1.0
        return self

    @property
    def image_width(self) -> int:
        return self._image_width

    @property
    def image_height(self) -> int:
        return self._image_height

    @property
    def dpr(self) -> float:
        return self._dpr

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

    @field_validator("screenshot_freshness", mode="before")
    @classmethod
    def _coerce_freshness(cls, v: object) -> str:
        if not isinstance(v, str):
            return "fresh"
        s = v.strip().lower().replace("-", "_").replace(" ", "_")
        if s in {"fresh", "uncertain", "stale"}:
            return s
        aliases = {
            "ok": "fresh", "ready": "fresh", "current": "fresh",
            "loading": "uncertain", "pending": "uncertain",
            "partial": "uncertain", "unknown": "uncertain",
            "old": "stale", "mismatch": "stale", "wrong": "stale",
        }
        return aliases.get(s, "uncertain")

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

        def _rank(b: BBox) -> tuple[int, int, int, float]:
            # Blocker bboxes rank first so "dismiss cookie banner" never
            # gets buried behind 20 content elements.
            role_rank = 0 if b.role_in_scene == "blocker" else (
                1 if b.role_in_scene == "target" else 2
            )
            return (
                role_rank,
                0 if b.intent_relevant else 1,
                0 if b.clickable else 1,
                -b.confidence,
            )

        ordered = sorted(self.bboxes, key=_rank)[:max_bboxes]
        elements_lines: list[str] = []
        iw, ih = self._image_width, self._image_height
        for i, b in enumerate(ordered, start=1):
            if b.role_in_scene == "blocker":
                role_tag = " [BLOCKER]"
            elif b.role_in_scene == "target":
                role_tag = " [TARGET]"
            elif b.intent_relevant:
                role_tag = "  ← matches intent"
            else:
                role_tag = ""
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
                f"{role_tag}"
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
        if self.scene and self.scene.layers:
            # Walk layers top-most first so the brain sees what's on top
            # before what's underneath. Index each layer to [V...] labels
            # the brain can reference — matches the ordered bbox list below.
            ordered_ids = [b.layer_id for b in ordered]
            scene_lines = ["Scene:"]
            for layer in self.scene.layers:
                blocks = "blocks=true" if layer.blocks_interaction_below else "blocks=false"
                label_q = f'"{layer.dismiss_hint}"' if layer.dismiss_hint else "-"
                # Find bboxes in this layer among the rendered ones.
                vrefs = [
                    f"V{j+1}" for j, lid in enumerate(ordered_ids)
                    if lid and lid == layer.id
                ]
                vref_str = ("  bboxes=" + ",".join(vrefs)) if vrefs else ""
                scene_lines.append(
                    f"  {layer.id} [{layer.kind}, {blocks}]  dismiss_hint={label_q}"
                    f"{vref_str}"
                )
            if self.scene.active_blocker_layer_id:
                scene_lines.append(
                    f"  active_blocker={self.scene.active_blocker_layer_id} "
                    f"(must dismiss before pursuing main goal)"
                )
            parts.extend(scene_lines)
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
        def _rank(b: BBox) -> tuple[int, int, int, float]:
            role_rank = 0 if b.role_in_scene == "blocker" else (
                1 if b.role_in_scene == "target" else 2
            )
            return (
                role_rank,
                0 if b.intent_relevant else 1,
                0 if b.clickable else 1,
                -b.confidence,
            )
        ordered = sorted(self.bboxes, key=_rank)
        idx = vision_index_1based - 1
        if idx >= len(ordered):
            return None
        return ordered[idx]
