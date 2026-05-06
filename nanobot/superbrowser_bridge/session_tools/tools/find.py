"""browser_find_target — locate a label across viewport boundaries.

Phase B1 of the wineaccess revamp.

Vision sees only the current viewport. Markdown sees the full DOM
text. The brain often "knows" a target exists (it appears in markdown)
but has no V_n bbox to point at — because the target is below the
fold, above the fold (post-scroll), or hidden inside a collapsed
accordion section. This tool bridges the gap.

Pipeline:
  1. Search current vision bboxes for the label (exact + token-overlap).
     If a confident match exists, return its V_n immediately — no
     server round-trip needed.
  2. Otherwise call the TS `/find-target` endpoint, which:
       * walks the DOM for label-matching interactive elements,
       * computes each candidate's viewport-relative position,
       * detects collapsed-accordion ancestors,
       * resolves section path for disambiguation,
       * (optionally) auto-scrolls the best candidate into view.
  3. If we scrolled, schedule a fresh vision prefetch and return the
     section path + structured next-action hint so the brain knows
     whether to expand a section first or click directly.
"""

from __future__ import annotations

from ._common import *  # noqa: F401,F403


def _bbox_label_match(bbox_label: str, want: str) -> float:
    """Return [0..1] match score between a vision bbox label and target.

    1.0 = exact substring (after lowercasing).
    Otherwise = Jaccard token overlap. Below 0.5 → considered no match.
    """
    a = (bbox_label or "").strip().lower()
    b = (want or "").strip().lower()
    if not a or not b:
        return 0.0
    if b in a:
        # Reward shorter labels — "Oregon" inside "Oregon (12)" should
        # outrank "Oregon" inside "Oregon Wine Country Tours".
        return min(1.0, 0.6 + 0.4 * (len(b) / max(len(a), 1)))
    a_tokens = set(_re_word.findall(a))
    b_tokens = set(_re_word.findall(b))
    if not a_tokens or not b_tokens:
        return 0.0
    overlap = len(a_tokens & b_tokens)
    union = len(a_tokens | b_tokens)
    return overlap / union if union else 0.0


# Pre-compiled tokenizer; module-level to avoid per-call recompiles.
import re as _re
_re_word = _re.compile(r"\w+", _re.UNICODE)


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        label=StringSchema(
            "The visible text or aria-label of the target you're looking "
            "for. e.g. 'Oregon', 'Submit', 'Add to cart', 'Region'."
        ),
        section=StringSchema(
            "Optional disambiguating section header. e.g. label='Oregon' "
            "section='Region' restricts the search to inside the Region "
            "filter section, ignoring matches in headlines, footers, etc.",
            nullable=True,
        ),
        role=StringSchema(
            "Optional role/tag filter. e.g. 'checkbox', 'button', 'link'. "
            "Restricts to elements matching this role.",
            nullable=True,
        ),
        auto_scroll=BooleanSchema(
            description=(
                "If true (default), automatically scroll the best "
                "candidate into mid-viewport so the next browser_screenshot "
                "captures it as a V_n. Set false if you only want to "
                "discover whether the target exists without moving the "
                "page."
            ),
            default=True,
        ),
        required=["session_id", "label"],
    )
)
class BrowserFindTargetTool(Tool):
    """Find a labelled target across viewport boundaries.

    Returns structured candidate info so the brain knows:
      * is the target visible right now? → click it directly via V_n
      * is it above/below the fold? → tool auto-scrolled (next
        screenshot will surface it as a fresh V_n)
      * is it inside a collapsed accordion? → click the section header
        first, THEN re-find / re-screenshot to surface the option

    Designed to short-circuit the "click_at fails → eval probe → fabricate
    URL" cascade observed in the wineaccess.com production trace, where
    the brain knew "Oregon" was on the page but had no first-class way
    to locate it across viewport + accordion boundaries.
    """

    name = "browser_find_target"
    description = (
        "Locate a target by label across viewport + accordion boundaries. "
        "**Use this when** you know what you're looking for (a filter "
        "value, a button) but no V_n in your last screenshot labels it — "
        "the target may be below the fold or in a collapsed section. "
        "**Pipeline:** searches vision bboxes first (free), then walks "
        "the DOM to find candidates, reports their viewport position + "
        "section path + collapsed-accordion ancestor (if any), and "
        "auto-scrolls the best candidate into view. Returns structured "
        "next-action hints. **Don't** use this for elements already "
        "visible as V_n in your last screenshot — click those directly "
        "with browser_click_at(vision_index)."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        label: str,
        section: str | None = None,
        role: str | None = None,
        auto_scroll: bool = True,
        **kw: Any,
    ) -> str:
        label_clean = (label or "").strip()
        if not label_clean:
            return "[find_target_failed:empty_label] Provide the visible text you're looking for."
        print(f"\n>> browser_find_target(label={label_clean!r}, section={section!r})")

        # Step 1: scan current vision bboxes for a fast hit. Free path.
        # Skip if the section= filter is set — bbox labels don't carry
        # section context, so we'd need the DOM walk anyway to verify.
        if not section:
            resp = self.s.vision_for_target_resolution()
            best_bbox: tuple[int, str, float] | None = None  # (V_n, label, score)
            if resp is not None and getattr(resp, "bboxes", None):
                for i, b in enumerate(resp.bboxes, start=1):
                    score = _bbox_label_match(getattr(b, "label", ""), label_clean)
                    if score >= 0.6 and (best_bbox is None or score > best_bbox[2]):
                        best_bbox = (i, getattr(b, "label", "") or "", score)
            if best_bbox is not None:
                v_n, b_label, b_score = best_bbox
                self.s.record_step(
                    "browser_find_target",
                    f"label={label_clean!r}",
                    f"vision_hit V{v_n} score={b_score:.2f}",
                )
                return (
                    f"[find_target_hit_vision V{v_n} label={b_label!r} "
                    f"score={b_score:.2f}]\n"
                    f"Target {label_clean!r} matched V{v_n} from your "
                    "current vision response. Click it with "
                    f"browser_click_at(vision_index={v_n})."
                )

        # Step 2: server-side DOM walk for candidates across the page.
        payload: dict[str, Any] = {
            "label": label_clean,
            "autoScroll": bool(auto_scroll),
            "maxCandidates": 5,
        }
        if section:
            payload["section"] = section.strip()
        if role:
            payload["role"] = role.strip()

        try:
            r = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/find-target",
                json=payload,
                timeout=15.0,
            )
        except Exception as exc:
            return (
                f"[find_target_failed:network] {exc}\nFallback: "
                "browser_get_markdown(outline=true) to see the page "
                "structure, then browser_scroll_until(target_text=...)."
            )

        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            return f"[find_target_failed:server] {err}"

        data = r.json()
        candidates = data.get("candidates") or []
        scrolled_to = data.get("scrolledTo")
        page_height = data.get("pageHeight", 0)
        viewport_height = data.get("viewportHeight", 0)
        url = data.get("url", self.s.current_url) or ""
        if url:
            self.s.record_url(url)

        if not candidates:
            self.s.record_step(
                "browser_find_target",
                f"label={label_clean!r}",
                "not_in_dom",
            )
            return (
                f"[find_target_not_found label={label_clean!r}]\n"
                f"No DOM elements match {label_clean!r}"
                + (f" within section={section!r}" if section else "")
                + ". The target may not exist on the current page. "
                "Consider: (a) browser_navigate to a different URL, "
                "(b) browser_scroll(direction='down', amount=full_page) "
                "to load lazy content, or (c) browser_get_markdown to "
                "verify what's actually on the page."
            )

        best = candidates[0]
        position = best.get("viewportPosition", "unknown")
        section_path = best.get("sectionPath") or []
        collapsed = best.get("collapsedAncestor") or None
        requires_expand = bool(best.get("requiresExpand"))
        css_hint = best.get("cssSelectorHint", "")

        # If we scrolled, schedule a vision prefetch so the next screenshot
        # is warm. The brain still has to call browser_screenshot to get
        # the new V_n labels.
        if scrolled_to is not None:
            try:
                _schedule_vision_prefetch(self.s, session_id)
            except Exception as exc:
                print(f"  [find_target: vision prefetch failed: {exc}]")
            # find_target with auto_scroll IS a viewport mutation —
            # subsequent V_n indices are stale.
            self.s._mutation_needs_observation = True

        section_str = (
            " > ".join(s for s in section_path[:4])
            if section_path
            else "(no section path)"
        )

        if requires_expand and collapsed:
            ancestor_text = (collapsed.get("text") or "").strip()[:40]
            self.s.record_step(
                "browser_find_target",
                f"label={label_clean!r}",
                f"in_collapsed_section path={section_str}",
            )
            return (
                f"[find_target_in_collapsed_section label={label_clean!r}]\n"
                f"  position: in_collapsed_section\n"
                f"  section_path: {section_str}\n"
                f"  ancestor_label: {ancestor_text!r}\n"
                f"  candidates: {len(candidates)}\n"
                f"  page_height: {page_height}px  viewport: {viewport_height}px\n"
                "Next action: call browser_screenshot to capture the "
                "current view, find the V_n for the section header "
                f"(text matching {ancestor_text!r}), and click it to "
                "expand. Then call browser_find_target again — the "
                "expanded options will be searchable."
            )

        if scrolled_to is not None:
            self.s.record_step(
                "browser_find_target",
                f"label={label_clean!r}",
                f"scrolled to y={scrolled_to}",
            )
            return (
                f"[find_target_scrolled label={label_clean!r}]\n"
                f"  position_before_scroll: {position}\n"
                f"  scrolled_to: y={scrolled_to}px (from y={data.get('scrolledFrom', 0)}px)\n"
                f"  section_path: {section_str}\n"
                f"  best_candidate: <{best.get('tag','?')} role='{best.get('role','')}' "
                f"text={best.get('text','')[:50]!r}>\n"
                f"  candidates: {len(candidates)}\n"
                "Next action: call browser_screenshot — the target is "
                "now in view and will appear as a fresh V_n. Click it "
                "with browser_click_at(vision_index=V_n)."
            )

        # Visible — return its position so the brain can re-screenshot
        # if its current vision is stale.
        self.s.record_step(
            "browser_find_target",
            f"label={label_clean!r}",
            f"visible at y={best.get('yOffsetFromViewportTop', 0)}",
        )
        return (
            f"[find_target_visible label={label_clean!r}]\n"
            f"  position: visible\n"
            f"  y_offset: {best.get('yOffsetFromViewportTop', 0)}px from viewport top\n"
            f"  section_path: {section_str}\n"
            f"  candidates: {len(candidates)}\n"
            "Next action: if your last vision response was fresh, V_n "
            "for this target should already exist — search the bbox "
            "list for the label. Otherwise call browser_screenshot to "
            "refresh."
        )
