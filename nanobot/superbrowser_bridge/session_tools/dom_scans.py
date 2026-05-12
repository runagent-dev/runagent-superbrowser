"""DOM-side scanners that surface interactive items as synthetic V_n.

The bridge runs these scanners after specific tool actions (typing into
a search box, clicking a date trigger, opening a modal). Each scanner
produces a list of `ScanResult` items — CSS-pixel rects + label + role.
`inject_scan_as_synthetic_bboxes` denormalizes each rect into a
`vision_agent.schemas.BBox` and registers it via
`BrowserSessionState.inject_synthetic_bboxes`.

The brain then sees the items as regular V_n bboxes in tool-result
captions and clicks them via `browser_click_at(vision_index=V_n)`. This
closes the gap where vision (Gemini) misses small dynamic items the
DOM clearly exposes — without forcing the brain to fall back to
`browser_eval` / `browser_run_script` (which trip bot detection).

Scanners run in the page via the existing `/evaluate` endpoint —
they're plain JS strings, not Playwright/Puppeteer scripts.

Registry is keyed by scan_kind:
  - "autocomplete"      → search/combobox suggestion lists (already in input_text.py as _AUTOCOMPLETE_SCAN_JS; wrapped here)
  - "calendar_grid"     → react-datepicker, MUI DatePicker, native [role=gridcell]
  - "modal_cta"         → primary buttons inside an open [role=dialog] / [aria-modal=true]
  - "custom_dropdown"   → ARIA menus + Headless UI menu items
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .http_client import SUPERBROWSER_URL, _request_with_backoff


# ---------------------------------------------------------------------
# ScanResult — what every scanner returns per item.
# ---------------------------------------------------------------------


@dataclass
class ScanResult:
    """One interactive item found by a DOM scan.

    `x`, `y` are the CSS-pixel center; `w`, `h` are the CSS-pixel size.
    Used by `_css_rect_to_box_2d` to construct a normalized box_2d.
    """

    text: str
    x: float
    y: float
    w: float
    h: float
    kind: str  # autocomplete | calendar_cell | modal_cta | custom_option
    role: str = "option"


# ---------------------------------------------------------------------
# Scanner JS bodies. Each is a self-invoking async expression that
# returns `{items: [{text, x, y, w, h, role}], detected: bool}`.
# Kept terse — these run on every relevant action and the brain pays
# only the DOM-scan latency, not vision dollars.
# ---------------------------------------------------------------------


# Mirrors input_text.py:_AUTOCOMPLETE_SCAN_JS but returns full rects so
# we can build proper bboxes. The original scan is kept (for legacy
# callers) but we re-run via this version when we need bbox dims.
_AUTOCOMPLETE_FULL_JS = """
(async () => {
  await new Promise(r => requestAnimationFrame(() => r()));
  await new Promise(r => setTimeout(r, 300));
  const seen = new Set();
  const out = [];
  const selectors = [
    '[role="listbox"] [role="option"]',
    '[role="combobox"] + * li',
    '[role="combobox"] + * [role="option"]',
    '[role="option"]:not([aria-hidden="true"])',
    '[aria-selected]:not([aria-hidden="true"])',
    '.autocomplete-suggestions li, .autocomplete li',
    'ul.suggestions li, .suggestions li',
    '.MuiAutocomplete-listbox li',
    '[aria-live] li',
    '.dropdown-menu.show li, .dropdown-menu[style*="display: block"] li',
    '.ui-autocomplete li',
    '[class*="autocomplete"][class*="option"]',
    '[class*="suggestion"] li, [class*="suggestions"] li',
    '.ais-Hits-list .ais-Hits-item',
    '[class*="ais-Hits-item"]',
    '[class*="aa-Item"]',
    '[class*="aa-Suggestion"]',
    '[id^="downshift"] [role="option"]',
    '[id^="downshift"] li',
    '[class*="select__option"]',
    '[id*="-option-"]',
    '[data-reach-combobox-option]',
    '[id^="headlessui-listbox-option-"]',
    '[id^="headlessui-combobox-option-"]',
  ];
  for (const sel of selectors) {
    let nodes;
    try { nodes = document.querySelectorAll(sel); } catch { continue; }
    nodes.forEach(el => {
      const r = el.getBoundingClientRect();
      if (r.width < 30 || r.height < 10) return;
      if (r.top > window.innerHeight * 1.5) return;
      const cs = window.getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return;
      const txt = (el.innerText || el.textContent || '').trim();
      if (!txt || txt.length > 120 || seen.has(txt)) return;
      seen.add(txt);
      out.push({
        text: txt,
        x: r.left + r.width / 2,
        y: r.top + r.height / 2,
        w: r.width,
        h: r.height,
        role: 'option',
      });
    });
  }
  return {items: out.slice(0, 8), detected: out.length > 0};
})();
"""


_CALENDAR_GRID_JS = """
(async () => {
  await new Promise(r => requestAnimationFrame(() => r()));
  await new Promise(r => setTimeout(r, 150));
  const seen = new Set();
  const out = [];
  // Common datepicker selectors. Order matters — more specific first
  // so we don't double-emit (the seen-by-coord check below also dedupes).
  const selectors = [
    '[role="gridcell"][data-date]',
    '[role="gridcell"] button:not([disabled])',
    '.react-datepicker__day:not(.react-datepicker__day--disabled)',
    '.MuiPickersDay-root:not(.Mui-disabled)',
    '.flatpickr-day:not(.flatpickr-disabled):not(.prevMonthDay):not(.nextMonthDay)',
    'td[role="gridcell"]:not([aria-disabled="true"])',
    '[class*="datepicker"][class*="day"]:not([class*="disabled"])',
    '[class*="calendar"] [class*="day"]:not([class*="disabled"])',
  ];
  for (const sel of selectors) {
    let nodes;
    try { nodes = document.querySelectorAll(sel); } catch { continue; }
    nodes.forEach(el => {
      const r = el.getBoundingClientRect();
      if (r.width < 12 || r.height < 12 || r.width > 80 || r.height > 80) return;
      if (r.top < 0 || r.top > window.innerHeight) return;
      const cs = window.getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return;
      const txt = (el.innerText || el.textContent || el.getAttribute('aria-label') || '').trim();
      if (!txt || txt.length > 40) return;
      const k = Math.round(r.left) + ':' + Math.round(r.top);
      if (seen.has(k)) return;
      seen.add(k);
      // Prefer aria-label for full date context (e.g., "May 15, 2026"
      // vs. just "15"). Fall back to innerText.
      const label = el.getAttribute('aria-label') || txt;
      out.push({
        text: label.slice(0, 40),
        x: r.left + r.width / 2,
        y: r.top + r.height / 2,
        w: r.width,
        h: r.height,
        role: 'gridcell',
      });
    });
  }
  return {items: out.slice(0, 42), detected: out.length > 0};
})();
"""


_MODAL_CTA_JS = """
(async () => {
  await new Promise(r => requestAnimationFrame(() => r()));
  await new Promise(r => setTimeout(r, 120));
  // Find any open modal/dialog. Prefer the most-recently-stacked one
  // (highest z-index) so nested dialogs don't surface the parent's CTAs.
  const candidates = [];
  document.querySelectorAll(
    '[role="dialog"], [role="alertdialog"], [aria-modal="true"], dialog[open]'
  ).forEach(d => {
    const r = d.getBoundingClientRect();
    const cs = window.getComputedStyle(d);
    if (r.width < 50 || r.height < 50) return;
    if (cs.display === 'none' || cs.visibility === 'hidden') return;
    const z = parseInt(cs.zIndex, 10) || 0;
    candidates.push({el: d, z, area: r.width * r.height});
  });
  if (!candidates.length) return {items: [], detected: false};
  candidates.sort((a, b) => (b.z - a.z) || (b.area - a.area));
  const modal = candidates[0].el;
  const out = [];
  const seen = new Set();
  modal.querySelectorAll(
    'button:not([disabled]), [role="button"]:not([aria-disabled="true"]), a[href]'
  ).forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.width < 20 || r.height < 12) return;
    const cs = window.getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return;
    const txt = (el.innerText || el.textContent || el.getAttribute('aria-label') || '').trim();
    if (!txt || txt.length > 80) return;
    if (seen.has(txt)) return;
    seen.add(txt);
    out.push({
      text: txt,
      x: r.left + r.width / 2,
      y: r.top + r.height / 2,
      w: r.width,
      h: r.height,
      role: 'button',
    });
  });
  return {items: out.slice(0, 10), detected: out.length > 0};
})();
"""


_CUSTOM_DROPDOWN_JS = """
(async () => {
  await new Promise(r => requestAnimationFrame(() => r()));
  await new Promise(r => setTimeout(r, 150));
  const seen = new Set();
  const out = [];
  const selectors = [
    '[role="menu"][aria-expanded="true"] [role="menuitem"]',
    '[role="menu"]:not([hidden]) [role="menuitem"]',
    '[id^="headlessui-menu-item-"]',
    '[id^="headlessui-listbox-option-"]:not([aria-disabled="true"])',
    '[role="combobox"][aria-expanded="true"] ~ * [role="option"]',
    '[data-radix-popper-content-wrapper] [role="menuitem"]',
    '[data-state="open"] [role="menuitem"]',
  ];
  for (const sel of selectors) {
    let nodes;
    try { nodes = document.querySelectorAll(sel); } catch { continue; }
    nodes.forEach(el => {
      const r = el.getBoundingClientRect();
      if (r.width < 30 || r.height < 14) return;
      if (r.top < 0 || r.top > window.innerHeight * 1.5) return;
      const cs = window.getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return;
      const txt = (el.innerText || el.textContent || el.getAttribute('aria-label') || '').trim();
      if (!txt || txt.length > 100) return;
      if (seen.has(txt)) return;
      seen.add(txt);
      out.push({
        text: txt,
        x: r.left + r.width / 2,
        y: r.top + r.height / 2,
        w: r.width,
        h: r.height,
        role: 'menuitem',
      });
    });
  }
  return {items: out.slice(0, 12), detected: out.length > 0};
})();
"""


# ---------------------------------------------------------------------
# Scanner registry.
# ---------------------------------------------------------------------


SCAN_REGISTRY: dict[str, dict[str, Any]] = {
    "autocomplete": {"js": _AUTOCOMPLETE_FULL_JS, "default_role": "option"},
    "calendar_grid": {"js": _CALENDAR_GRID_JS, "default_role": "gridcell"},
    "modal_cta": {"js": _MODAL_CTA_JS, "default_role": "button"},
    "custom_dropdown": {"js": _CUSTOM_DROPDOWN_JS, "default_role": "menuitem"},
}


async def run_dom_scan(session_id: str, scan_kind: str) -> list[ScanResult]:
    """Execute the scanner identified by `scan_kind` in the page.

    Returns a list of ScanResult; empty on error or no matches.
    """
    if scan_kind not in SCAN_REGISTRY:
        return []
    js = SCAN_REGISTRY[scan_kind]["js"]
    default_role = SCAN_REGISTRY[scan_kind]["default_role"]
    try:
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
            json={"script": js},
            timeout=8.0,
        )
        if r.status_code != 200:
            return []
        body = r.json()
        got = body.get("result") if isinstance(body, dict) else None
        if not isinstance(got, dict):
            return []
        items = got.get("items") or []
        out: list[ScanResult] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            text = (it.get("text") or "").strip()
            if not text:
                continue
            try:
                out.append(ScanResult(
                    text=text,
                    x=float(it.get("x") or 0.0),
                    y=float(it.get("y") or 0.0),
                    w=float(it.get("w") or 4.0),
                    h=float(it.get("h") or 4.0),
                    kind=scan_kind,
                    role=str(it.get("role") or default_role),
                ))
            except (TypeError, ValueError):
                continue
        return out
    except Exception as exc:
        print(f"  [dom_scan {scan_kind} failed: {exc}]")
        return []


# ---------------------------------------------------------------------
# CSS rect → normalized box_2d.
# ---------------------------------------------------------------------


def _css_rect_to_box_2d(
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    image_w: int,
    image_h: int,
    dpr: float,
) -> list[int]:
    """Inverse of `BBox.to_pixels`. Given a CSS-pixel center+size, return
    the normalized [ymin, xmin, ymax, xmax] in [0, 1000].

    `image_w` / `image_h` are the SCREENSHOT pixel dimensions and `dpr`
    is the viewport device pixel ratio at capture time. We invert the
    same `scale = image / dpr` factor used in `to_pixels`.
    """
    if image_w <= 0 or image_h <= 0:
        return [0, 0, 0, 0]
    scale_w = image_w / max(dpr, 1e-6)
    scale_h = image_h / max(dpr, 1e-6)
    x0 = x - w / 2.0
    y0 = y - h / 2.0
    x1 = x + w / 2.0
    y1 = y + h / 2.0
    xmin = int(round(max(0.0, x0) / scale_w * 1000.0))
    ymin = int(round(max(0.0, y0) / scale_h * 1000.0))
    xmax = int(round(max(0.0, x1) / scale_w * 1000.0))
    ymax = int(round(max(0.0, y1) / scale_h * 1000.0))
    xmin = max(0, min(1000, xmin))
    ymin = max(0, min(1000, ymin))
    xmax = max(xmin + 1, min(1000, xmax))
    ymax = max(ymin + 1, min(1000, ymax))
    return [ymin, xmin, ymax, xmax]


async def _resolve_viewport_dims(session_id: str) -> tuple[int, int, float]:
    """Fall-back viewport probe when no vision response is available
    yet to source image_width / image_height / dpr from. Returns
    `(image_w, image_h, dpr)` — at worst `(0, 0, 1.0)` if the probe
    fails, in which case caller should skip injection.

    Uses the same IIFE pattern the autocomplete scan uses so the TS
    `/evaluate` endpoint resolves it to a value reliably (a bare
    parenthesised object literal can get swallowed by Puppeteer's
    function-wrapping heuristic).
    """
    js = (
        "(() => ({"
        "w: Math.round((window.innerWidth || 0) * (window.devicePixelRatio || 1)),"
        "h: Math.round((window.innerHeight || 0) * (window.devicePixelRatio || 1)),"
        "dpr: window.devicePixelRatio || 1"
        "}))()"
    )
    try:
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
            json={"script": js},
            timeout=3.0,
        )
        if r.status_code != 200:
            print(f"  [viewport_probe failed: status={r.status_code}]")
            return (0, 0, 1.0)
        body = r.json()
        got = body.get("result") if isinstance(body, dict) else None
        if not isinstance(got, dict):
            print(f"  [viewport_probe failed: result not dict, got={type(got).__name__}]")
            return (0, 0, 1.0)
        return (
            int(got.get("w") or 0),
            int(got.get("h") or 0),
            float(got.get("dpr") or 1.0),
        )
    except Exception as exc:
        print(f"  [viewport_probe failed: {exc}]")
        return (0, 0, 1.0)


# ---------------------------------------------------------------------
# Top-level injection helper.
# ---------------------------------------------------------------------


async def inject_scan_as_synthetic_bboxes(
    state: Any,
    session_id: str,
    scan_kind: str,
    *,
    anchor_v: Optional[int] = None,
    prefetched_items: Optional[list[dict]] = None,
    ttl_turns: int = 3,
) -> list[tuple[int, ScanResult]]:
    """Run a DOM scan and inject each result as a synthetic V_n bbox.

    Returns a list of `(v_n, ScanResult)` pairs in injection order. The
    caller uses the V_n to compose its tool-result caption ("Items: V_n
    'label', V_m 'label'") so the brain can click directly.

    `prefetched_items` lets the caller pass already-scanned items (e.g.,
    `_scan_autocomplete_suggestions` was already invoked in input_text.py)
    to avoid double JS round-trip. Each item is a dict with at least
    `text` / `x` / `y`; `w` / `h` default to 4 if missing — vision-bbox
    click snaps to the actual interactive element via `clickInBbox` so
    the bbox size only needs to be large enough to contain the target's
    centre.
    """
    # Source dims from the most recent vision response, falling back to
    # a live viewport probe, then to a 1280x800 default. Failing entirely
    # would silently drop the synthetic V_n which is worse than an
    # imprecise bbox — clickInBbox snaps to the interactive element
    # inside the bbox anyway, so a 4×4-px-around-center placeholder still
    # lands correctly.
    image_w = 0
    image_h = 0
    dpr = 1.0
    last = getattr(state, "_last_vision_response", None)
    if last is not None:
        image_w = int(getattr(last, "image_width", 0) or 0)
        image_h = int(getattr(last, "image_height", 0) or 0)
        dpr = float(getattr(last, "dpr", 1.0) or 1.0)
    src = "vision_resp"
    if image_w <= 0 or image_h <= 0:
        epoch = getattr(state, "_vision_epoch_response", None)
        if epoch is not None:
            image_w = int(getattr(epoch, "image_width", 0) or 0)
            image_h = int(getattr(epoch, "image_height", 0) or 0)
            dpr = float(getattr(epoch, "dpr", 1.0) or 1.0)
            src = "epoch_resp"
    if image_w <= 0 or image_h <= 0:
        image_w, image_h, dpr = await _resolve_viewport_dims(session_id)
        src = "viewport_probe"
    if image_w <= 0 or image_h <= 0:
        # Last-resort defaults so we still inject. clickInBbox will snap
        # to the interactive element under the bbox center, so a
        # slightly-off normalized rect still lands correctly.
        image_w = 1280
        image_h = 800
        dpr = 1.0
        src = "default_fallback"

    # Collect ScanResult list either from caller-provided prefetched
    # items (avoid double scan) or by running the registered scanner.
    results: list[ScanResult] = []
    if prefetched_items is not None:
        default_role = SCAN_REGISTRY.get(scan_kind, {}).get("default_role", "option")
        for it in prefetched_items:
            if not isinstance(it, dict):
                continue
            text = (it.get("text") or "").strip()
            if not text:
                continue
            try:
                results.append(ScanResult(
                    text=text,
                    x=float(it.get("x") or 0.0),
                    y=float(it.get("y") or 0.0),
                    w=float(it.get("w") or 4.0),
                    h=float(it.get("h") or 4.0),
                    kind=scan_kind,
                    role=str(it.get("role") or default_role),
                ))
            except (TypeError, ValueError):
                continue
    else:
        results = await run_dom_scan(session_id, scan_kind)
    if not results:
        print(f"  [dom_scan {scan_kind} aborted: zero results after normalization]")
        return []

    # Lazy import to avoid a hard dependency from this module on the
    # vision_agent package. Try both import paths — `vision_agent` is
    # the runtime convention (sys.path includes the nanobot dir);
    # `nanobot.vision_agent` works when the test harness runs from the
    # parent dir.
    BBox = None
    try:
        from vision_agent.schemas import BBox  # type: ignore[no-redef]
    except Exception:
        try:
            from nanobot.vision_agent.schemas import BBox  # type: ignore[no-redef]
        except Exception as exc:
            print(f"  [dom_scan injection skipped: BBox import failed: {exc}]")
            return []

    bboxes: list[Any] = []
    construct_errors: list[str] = []
    for sr in results:
        try:
            box_2d = _css_rect_to_box_2d(
                sr.x, sr.y, sr.w, sr.h,
                image_w=image_w, image_h=image_h, dpr=dpr,
            )
            bb = BBox(
                label=sr.text[:80],
                box_2d=box_2d,
                clickable=True,
                role=sr.role,
                confidence=0.85,
                intent_relevant=True,
                role_in_scene="target",
            )
            bboxes.append(bb)
        except Exception as exc:
            construct_errors.append(f"{sr.text[:30]!r}: {exc}")
            continue
    if construct_errors:
        print(
            f"  [dom_scan {scan_kind} construct errors ({len(construct_errors)}): "
            f"{'; '.join(construct_errors[:3])}]"
        )
    if not bboxes:
        print(
            f"  [dom_scan {scan_kind} aborted: 0 bboxes constructed from "
            f"{len(results)} results]"
        )
        return []

    v_indices = state.inject_synthetic_bboxes(
        bboxes,
        scan_kind=scan_kind,
        anchor_v=anchor_v,
        ttl_turns=ttl_turns,
    )
    if v_indices:
        print(
            f"  [dom_scan {scan_kind} injected {len(v_indices)} synthetic V_n: "
            f"{v_indices[:5]}{'...' if len(v_indices) > 5 else ''}]"
        )
    else:
        print(
            f"  [dom_scan {scan_kind} produced {len(results)} results but "
            f"state.inject_synthetic_bboxes returned [] — check state setup]"
        )
    return list(zip(v_indices, results))
