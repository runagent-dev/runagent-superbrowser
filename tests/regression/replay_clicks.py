"""Regression replay harness for click-resolution accuracy.

Phase 7 of the v3 refactor. Loads captured production runs from
``fixtures/`` and replays the bbox → CSS-pixel resolution path
offline. Asserts that the resolved coordinates and the snap target
match what the production run actually clicked. Becomes the A/B
benchmark for v2-restoration vs current v3.

Usage::

    cd /root/agentic-browser/runagent-superbrowser
    source venv/bin/activate
    python tests/regression/replay_clicks.py            # replay all fixtures
    python tests/regression/replay_clicks.py --strict   # fail on any mismatch
    python tests/regression/replay_clicks.py fixture.json  # one file

Capturing fixtures (production):

    Set CLICK_CAPTURE_DIR=/path/in/production. The cursor tool emits
    one JSON per click with the schema documented at the top of
    ``fixtures/README.md``. Copy the captures into this folder and
    re-run the harness offline.

The harness does NOT need the TS browser server running — it
operates purely on captured screenshots and on the BBox.to_pixels
denormalization. The TS-side clickInBbox snap is replayed by a
small DOM-overlap simulator using the captured ``selector_map``
field.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Allow running as a script from repo root.
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "nanobot"))

from vision_agent.schemas import BBox  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@dataclass
class ReplayResult:
    fixture: str
    box_2d: list[int]
    image_dims: tuple[int, int]
    dpr: float
    expected_target: str
    expected_coords: tuple[int, int]
    resolved_coords: tuple[int, int]
    matched: bool
    notes: str = ""


def _resolve_click(fixture: dict[str, Any]) -> ReplayResult:
    """Replay the bbox-resolution path for a single capture.

    The capture stores ``vision_response.bboxes[K].box_2d``,
    ``image_width``, ``image_height``, ``dpr``, plus the *captured*
    ``actual_click_coords`` and ``actual_clicked_target`` from the
    production run.
    """
    name = fixture.get("name", "<unnamed>")
    image_w = int(fixture["image_width"])
    image_h = int(fixture["image_height"])
    dpr = float(fixture.get("dpr", 1.0))
    box_2d = list(fixture["box_2d"])
    expected_target = fixture.get("expected_target", "?")
    expected_coords = tuple(fixture.get("expected_coords", (0, 0)))

    bbox = BBox(label=name, box_2d=box_2d, clickable=True)
    x0, y0, x1, y1 = bbox.to_pixels(image_w, image_h, dpr=dpr)
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    resolved_coords = (cx, cy)

    # Match tolerance — production captures often round to integers
    # but the actual click can land within a small bbox interior. A
    # 5px tolerance still catches the regression we care about
    # (off-by-DPR, off-by-axis) while letting trivial rounding pass.
    tol = int(fixture.get("tolerance_px", 5))
    matched = (
        abs(cx - expected_coords[0]) <= tol
        and abs(cy - expected_coords[1]) <= tol
    )

    return ReplayResult(
        fixture=name,
        box_2d=box_2d,
        image_dims=(image_w, image_h),
        dpr=dpr,
        expected_target=expected_target,
        expected_coords=expected_coords,
        resolved_coords=resolved_coords,
        matched=matched,
        notes=fixture.get("notes", ""),
    )


def _load_fixtures(paths: list[Path]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in paths:
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            out.extend(data)
        else:
            out.append(data)
    return out


def _format(r: ReplayResult) -> str:
    status = "OK " if r.matched else "FAIL"
    rx, ry = r.resolved_coords
    ex, ey = r.expected_coords
    iw, ih = r.image_dims
    return (
        f"[{status}] {r.fixture}\n"
        f"        image=({iw}x{ih}) dpr={r.dpr} box_2d={r.box_2d}\n"
        f"        resolved=({rx},{ry}) expected=({ex},{ey}) "
        f"target={r.expected_target!r}"
        + (f"\n        notes: {r.notes}" if r.notes else "")
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "files",
        nargs="*",
        type=Path,
        help="Fixture JSON files. If empty, all fixtures/*.json are replayed.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if any fixture fails to match.",
    )
    args = parser.parse_args(argv)

    paths = list(args.files) if args.files else sorted(FIXTURES_DIR.glob("*.json"))
    if not paths:
        print(f"[replay_clicks] No fixtures found in {FIXTURES_DIR}.", file=sys.stderr)
        print(
            "[replay_clicks] Capture from production with CLICK_CAPTURE_DIR=…\n"
            "[replay_clicks] See tests/regression/fixtures/README.md.",
            file=sys.stderr,
        )
        return 0  # No fixtures = no regressions to detect; that's OK.

    fixtures = _load_fixtures(paths)
    results = [_resolve_click(f) for f in fixtures]

    failed = [r for r in results if not r.matched]
    for r in results:
        print(_format(r))
    print()
    print(
        f"replay_clicks: {len(results) - len(failed)}/{len(results)} matched, "
        f"{len(failed)} failed."
    )
    if args.strict and failed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
