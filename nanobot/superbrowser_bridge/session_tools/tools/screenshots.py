"""Screenshot tools — full screenshot, captcha screenshot, image-region."""

from __future__ import annotations

from ._common import *  # noqa: F401,F403

@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        intent=StringSchema(
            "Optional hint for the vision agent. Only used when "
            "VISION_ENABLED=1.",
            nullable=True,
        ),
        required=["session_id"],
    )
)
class BrowserScreenshotTool(Tool):
    name = "browser_screenshot"
    description = "Take a screenshot. COSTS MONEY. Use browser_get_markdown or browser_eval to verify instead."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, intent: str | None = None, **kw: Any) -> Any:
        # Force-fresh: the brain is explicitly asking to re-observe.
        # Bypass the vision agent's cache so the bboxes we return reflect
        # the *current* page state, not a prior cached pass. Set the flag
        # BEFORE triggering the prefetch — the prefetch reads it and
        # salts the cache key with a nonce. Combined with the
        # MutationObserver-driven domDirty signal from the previous
        # /state call, this closes the "page changed silently" gap.
        self.s._force_vision_refresh = True
        # Schedule a fresh vision pass and immediately await it so the
        # screenshot caption that goes to the brain is paired with the
        # newly-computed bbox set rather than the prior one.
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        sync_block = await self.s.ensure_vision_synced(reason="browser_screenshot")
        if sync_block:
            return sync_block
        # Mark this turn as a deliberation point — the navigate gate
        # uses this to detect "the brain just looked at the page".
        self.s.last_deliberation_turn = self.s._brain_turn_counter
        self.s._mutation_needs_observation = False
        self.s._scripts_since_observation = 0
        # Peek current page content so dedup keys on (url, content_hash)
        # — a reload or DOM change produces a different hash and unblocks.
        peek_hash = ""
        try:
            peek_elements = await _fetch_elements(session_id, self.s)
            peek_hash = BrowserSessionState.hash_page_content(peek_elements)
        except Exception:
            pass

        allowed, reason = self.s.should_allow_screenshot(self.s.current_url, peek_hash)
        if not allowed:
            self.s.log_activity("screenshot(BLOCKED)", reason[:60])
            return reason

        self.s.screenshot_budget -= 1
        r = await _request_with_backoff(
            "GET",
            f"{SUPERBROWSER_URL}/session/{session_id}/state",
            # bounds=true returns selectorEntries (with x/y/width/height) +
            # devicePixelRatio so we can draw bbox overlays before the
            # screenshot goes to the vision LLM.
            # settle=true waits for waitForPageReady before snapshotting
            # so the screenshot reflects post-React-commit state, not a
            # mid-transition frame. settleMs=4000 is enough for SPA
            # route changes (wineaccess /store/search/... after Enter)
            # without hanging on broken pages.
            params={
                "vision": "true",
                "bounds": "true",
                "settle": "true",
                "settleMs": "4000",
            },
            timeout=20.0,
        )
        r.raise_for_status()
        data = r.json()

        actual_url = data.get("url", self.s.current_url)
        if actual_url:
            self.s.mark_screenshot_taken(
                actual_url,
                self.s.hash_page_content(data.get("elements", "")),
            )
        self.s.log_activity(f"screenshot({actual_url[:50] if actual_url else '?'})")
        self.s.record_step("browser_screenshot", "", f"url={actual_url[:60] if actual_url else '?'}")
        caption = _format_state(data, self.s)
        caption += f"\n[Screenshots remaining: {self.s.screenshot_budget}]"
        if data.get("screenshot"):
            entries = data.get("selectorEntries") or []
            dpr = float(data.get("devicePixelRatio") or 1.0)
            # Rename tagName → tag for the overlay (both naming schemes work
            # but tag is the overlay's canonical key). Phase R: also
            # carry the full `attributes` dict and `text` so the
            # post-vision enrichment step can copy href / aria-* /
            # name onto each Gemini bbox.
            overlay_elements = [
                {
                    "index": e.get("index"),
                    "tag": e.get("tagName") or e.get("tag"),
                    "role": e.get("role") or (e.get("attributes") or {}).get("role"),
                    "bounds": e.get("bounds"),
                    "attributes": e.get("attributes") or {},
                    "text": (e.get("text") or "")[:160],
                }
                for e in entries
                if e.get("bounds") and e.get("index") is not None
            ]
            return await self.s.build_tool_result_blocks(
                data["screenshot"],
                caption,
                intent=intent or "observe page",
                url=actual_url,
                elements=data.get("elements"),
                elements_with_bounds=overlay_elements,
                device_pixel_ratio=dpr,
            )
        return caption


@tool_parameters(
    tool_parameters_schema(session_id=StringSchema("Session ID"), required=["session_id"])
)
class BrowserCaptchaScreenshotTool(Tool):
    name = "browser_captcha_screenshot"
    description = "Take a close-up screenshot of the captcha area."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, **kw: Any) -> Any:
        r = await _request_with_backoff(
            "GET",
            f"{SUPERBROWSER_URL}/session/{session_id}/captcha/screenshot",
            timeout=10.0,
        )
        if r.status_code == 404:
            return "No captcha area found."
        r.raise_for_status()
        # t3 returns a dict with image_base64; t1 returns raw JPEG bytes.
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else None
        if isinstance(body, dict) and body.get("image_base64"):
            b64 = body["image_base64"]
        else:
            b64 = base64.b64encode(r.content).decode()
        return await self.s.build_tool_result_blocks(
            b64,
            "Captcha area — analyze to solve",
            intent="solve captcha — locate widget + tiles + handles",
            url=self.s.current_url,
        )


# --- Iterative captcha loop -------------------------------------------------
# Shared between BrowserSolveCaptchaTool (Python path, method='vision') and
# antibot/captcha/solve_vision.py (T3 HTTP path, POST /captcha/solve). Both
# entry points end up here so a fix to the "click every tile blindly" bug
# cannot regress in one path while the other is left behind.
@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        bbox_json=StringSchema(
            "JSON object {x, y, w, h} describing the viewport region to crop, in CSS pixels."
        ),
        quality=IntegerSchema("JPEG quality 1–100, default 80.", nullable=True),
        required=["session_id", "bbox_json"],
    )
)

class BrowserImageRegionTool(Tool):
    name = "browser_image_region"
    description = (
        "Screenshot a bounded region of the viewport and return base64 JPEG. "
        "Cheaper than a full-page Gemini pass for solvers that need to "
        "template-match a captcha piece, OCR a small area, or run a tiny "
        "focused vision query."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        bbox_json: str,
        quality: int | None = None,
        **kw: Any,
    ) -> str:
        try:
            bbox = json.loads(bbox_json)
        except (TypeError, ValueError) as exc:
            return f"[image_region_failed] bbox_json is not valid JSON: {exc}"
        for k in ("x", "y", "w", "h"):
            if not isinstance(bbox.get(k), (int, float)):
                return f"[image_region_failed] bbox must have numeric {k}"

        payload: dict[str, Any] = {"bbox": bbox}
        if quality is not None:
            payload["quality"] = quality
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/image-region",
            json=payload,
            timeout=15.0,
        )
        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            return f"[image_region_failed] {err}"
        data = r.json()
        b64 = data.get("base64", "")
        return (
            f"image_region: {bbox['w']}x{bbox['h']} at ({bbox['x']},{bbox['y']}), "
            f"base64_len={len(b64)}\n{b64[:200]}..."
        )


