"""Pin-down test for BBox.to_pixels — guards against denormalization
drift when restoring v2's vision pipeline.

v2 and v3 share the exact denormalization formula (verified at
``/root/runagent-superbrowser/nanobot/vision_agent/schemas.py:231-263``
and the v3 mirror). This test bakes in the contract so a future
refactor that changes scale order, DPR application, or rounding
will fail loudly.
"""

from __future__ import annotations

import pytest

from vision_agent.schemas import BBox


@pytest.mark.parametrize(
    "image_w,image_h,dpr,box_2d,expected",
    [
        # 1000-px image, no DPR — identity case, model coords pass through.
        (1000, 1000, 1.0, [100, 200, 200, 300], (200, 100, 300, 200)),
        # Common viewport: 1280x800 CSS @ DPR=1 → screenshot 1280x800.
        (1280, 800, 1.0, [500, 500, 600, 600], (640, 400, 768, 480)),
        # Retina (DPR=2): screenshot 2560x1600 physical, CSS pixels 1280x800.
        (2560, 1600, 2.0, [500, 500, 600, 600], (640, 400, 768, 480)),
        # 3x DPR (Pixel etc.) — screenshot 3840x2400, CSS 1280x800.
        (3840, 2400, 3.0, [500, 500, 600, 600], (640, 400, 768, 480)),
        # Empty box (ymin==ymax, xmin==xmax) → guaranteed non-empty.
        (1000, 1000, 1.0, [500, 500, 500, 500], (500, 500, 501, 501)),
        # Tight 1-pixel box at top-left.
        (1000, 1000, 1.0, [0, 0, 1, 1], (0, 0, 1, 1)),
        # Bottom-right corner of a 1920x1080 @ DPR=1 viewport.
        (1920, 1080, 1.0, [990, 990, 1000, 1000], (1901, 1069, 1920, 1080)),
        # Tall narrow column on a 1024x4000 long-page screenshot.
        (1024, 4000, 1.0, [100, 200, 900, 250], (205, 400, 256, 3600)),
    ],
)
def test_to_pixels_table(image_w, image_h, dpr, box_2d, expected):
    bbox = BBox(label="test", box_2d=box_2d, clickable=True)
    got = bbox.to_pixels(image_w, image_h, dpr=dpr)
    assert got == expected, (
        f"image=({image_w}x{image_h}) dpr={dpr} box_2d={box_2d}: "
        f"got {got}, expected {expected}"
    )


def test_to_pixels_dpr_zero_does_not_explode():
    """dpr=0.0 would divide by zero; the implementation clamps to 1e-6."""
    bbox = BBox(label="t", box_2d=[100, 200, 200, 300], clickable=True)
    # Should not raise — clamped denominator yields huge but finite coords.
    x0, y0, x1, y1 = bbox.to_pixels(1000, 1000, dpr=0.0)
    assert isinstance(x0, int)


def test_to_pixels_no_dpr_kwarg_defaults_to_one():
    bbox = BBox(label="t", box_2d=[100, 200, 200, 300], clickable=True)
    assert bbox.to_pixels(1000, 1000) == (200, 100, 300, 200)
