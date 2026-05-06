"""Phase R: BBox metadata enrichment via DOM IoU match.

The brain reads `[V_n]` lines and decides what to click. Before
enrichment, the line was just `[V_n] role 'label' (coords)` — when the
brain wanted the URL, it had to guess (and on wineaccess it guessed a
fabricated UUID). After enrichment the line carries `idx=N href=/real/url`
so the brain can navigate directly to the real URL or fall back to
`browser_click(index=N)`.
"""

from __future__ import annotations

from superbrowser_bridge.session_tools.state import (
    _enrich_bboxes_with_dom_metadata,
)
from vision_agent.schemas import BBox, PageFlags, VisionResponse


def _make_response(bboxes: list[BBox]) -> VisionResponse:
    resp = VisionResponse(
        summary="t",
        relevant_text="",
        page_type="unknown",
        intent="other",
        flags=PageFlags(),
        bboxes=bboxes,
    )
    return resp.with_image_dims(1280, 1100, dpr=1.0)


def test_enrichment_copies_href_and_dom_index():
    bb = BBox(
        label="Anderson Family Chardonnay",
        box_2d=[200, 300, 400, 700],  # → CSS (384,220 → 896,440)
        clickable=True,
        role="link",
    )
    resp = _make_response([bb])
    elements = [
        {
            "index": 43,
            "tag": "a",
            "bounds": {"x": 380, "y": 220, "w": 510, "h": 220},
            "attributes": {
                "href": "/catalog/2019-anderson_a7c9d9f8-350d-440f-b4bc-4acf9b1ec7b8/",
            },
            "text": "Anderson Family",
        },
    ]
    _enrich_bboxes_with_dom_metadata(
        resp, elements, image_w=1280, image_h=1100, dpr=1.0,
    )
    assert resp.bboxes[0].dom_index == 43
    assert resp.bboxes[0].href is not None
    assert "a7c9d9f8" in resp.bboxes[0].href


def test_enrichment_copies_aria_expanded():
    bb = BBox(
        label="Region",
        box_2d=[500, 100, 530, 300],
        clickable=True,
        role="button",
    )
    resp = _make_response([bb])
    elements = [
        {
            "index": 12,
            "tag": "button",
            "bounds": {"x": 128, "y": 548, "w": 256, "h": 38},
            "attributes": {"aria-expanded": "false"},
            "text": "Region",
        },
    ]
    _enrich_bboxes_with_dom_metadata(
        resp, elements, image_w=1280, image_h=1100, dpr=1.0,
    )
    assert resp.bboxes[0].aria_expanded == "false"
    # Render in brain text — should show 'collapsed'.
    text = resp.as_brain_text()
    assert "collapsed" in text


def test_enrichment_marks_active_filter():
    """A checkbox with aria-checked='true' is an active filter chip."""
    bb = BBox(
        label="Oregon",
        box_2d=[200, 200, 220, 280],
        clickable=True,
        role="checkbox",
    )
    resp = _make_response([bb])
    elements = [
        {
            "index": 88,
            "tag": "input",
            "bounds": {"x": 256, "y": 220, "w": 102, "h": 22},
            "attributes": {"aria-checked": "true", "type": "checkbox"},
            "text": "Oregon",
        },
    ]
    _enrich_bboxes_with_dom_metadata(
        resp, elements, image_w=1280, image_h=1100, dpr=1.0,
    )
    assert resp.bboxes[0].is_active_filter is True
    assert "active=true" in resp.as_brain_text()


def test_enrichment_skips_low_iou_matches():
    """Distant element shouldn't enrich the bbox."""
    bb = BBox(
        label="Submit",
        box_2d=[100, 100, 200, 200],  # CSS (128,110 → 256,220)
        clickable=True,
        role="button",
    )
    resp = _make_response([bb])
    elements = [
        {
            "index": 99,
            "tag": "button",
            # Far away — IoU == 0.
            "bounds": {"x": 800, "y": 800, "w": 100, "h": 50},
            "attributes": {"href": "/some-other-button"},
            "text": "Other",
        },
    ]
    _enrich_bboxes_with_dom_metadata(
        resp, elements, image_w=1280, image_h=1100, dpr=1.0,
    )
    assert resp.bboxes[0].dom_index is None
    assert resp.bboxes[0].href is None


def test_enrichment_handles_disabled_attribute():
    bb = BBox(
        label="Buy",
        box_2d=[300, 300, 400, 500],
        clickable=True,
        role="button",
    )
    resp = _make_response([bb])
    elements = [
        {
            "index": 7,
            "tag": "button",
            "bounds": {"x": 384, "y": 330, "w": 256, "h": 110},
            "attributes": {"disabled": "", "aria-disabled": "true"},
            "text": "Sold out",
        },
    ]
    _enrich_bboxes_with_dom_metadata(
        resp, elements, image_w=1280, image_h=1100, dpr=1.0,
    )
    assert resp.bboxes[0].is_disabled is True
    assert "DISABLED" in resp.as_brain_text()


def test_enrichment_handles_dpr_2():
    """Retina viewport: image is 2× CSS but bounds are still CSS."""
    bb = BBox(
        label="X",
        box_2d=[200, 300, 400, 700],
        clickable=True,
        role="link",
    )
    resp = _make_response([bb])
    # image_width / image_height are PrivateAttrs — set via the helper.
    resp.with_image_dims(2560, 2200, dpr=2.0)
    elements = [
        {
            "index": 5,
            "tag": "a",
            # CSS coords — bbox at (384,220→896,440) when iw=2560, dpr=2:
            #   ax0 = 300/1000 * (2560/2) = 384  ✓
            "bounds": {"x": 384, "y": 220, "w": 510, "h": 220},
            "attributes": {"href": "/x"},
            "text": "x",
        },
    ]
    _enrich_bboxes_with_dom_metadata(
        resp, elements, image_w=2560, image_h=2200, dpr=2.0,
    )
    assert resp.bboxes[0].dom_index == 5


def test_enrichment_no_elements_is_safe_noop():
    bb = BBox(label="x", box_2d=[100, 100, 200, 200], clickable=True, role="button")
    resp = _make_response([bb])
    _enrich_bboxes_with_dom_metadata(resp, [], image_w=1280, image_h=1100, dpr=1.0)
    assert resp.bboxes[0].dom_index is None


def test_brain_text_renders_metadata_inline():
    """End-to-end: render check shows the metadata in the V_n line."""
    bb = BBox(
        label="Anderson",
        box_2d=[200, 300, 400, 700],
        clickable=True,
        role="link",
    )
    resp = _make_response([bb])
    elements = [
        {
            "index": 43,
            "bounds": {"x": 380, "y": 220, "w": 510, "h": 220},
            "attributes": {"href": "/catalog/anderson_uuid/"},
            "text": "Anderson",
        },
    ]
    _enrich_bboxes_with_dom_metadata(
        resp, elements, image_w=1280, image_h=1100, dpr=1.0,
    )
    text = resp.as_brain_text()
    assert "idx=43" in text
    assert "href=/catalog/anderson_uuid/" in text
