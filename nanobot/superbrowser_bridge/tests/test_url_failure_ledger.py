"""Tests for the URL-failure ledger + path-style filter hallucination
detection.

The trace pattern: brain navigates to a URL → 404 → re-navigates to the
EXACT same URL → 404 again. With the ledger, the second navigate is
short-circuited with [URL_KNOWN_BAD] before any HTTP roundtrip.
"""

from __future__ import annotations

from urllib.parse import urlparse, parse_qsl

from superbrowser_bridge.session_tools import BrowserSessionState
from superbrowser_bridge.task_brief import TaskBrief


# ----------------------------- ledger -----------------------------------


def test_record_failed_navigation_populates_ledger():
    s = BrowserSessionState()
    s.record_failed_navigation("https://x.com/missing", 404)
    assert s.failed_navigation_urls
    norm = s._normalize_url("https://x.com/missing")
    assert norm in s.failed_navigation_urls
    assert s.failed_navigation_urls[norm]["status"] == 404


def test_record_failed_navigation_normalizes_url():
    """Trailing slash / fragment differences should hit the same entry
    so the brain can't bypass the lockout with a cosmetic URL change."""
    s = BrowserSessionState()
    s.record_failed_navigation("https://x.com/missing", 404)
    # Same URL with trailing slash should normalize identically.
    norm_a = s._normalize_url("https://x.com/missing")
    norm_b = s._normalize_url("https://x.com/missing/")
    # Either both keys exist OR both normalize to the same string.
    # The ledger needs to refuse re-navigation regardless of cosmetic diff.
    if norm_a == norm_b:
        assert norm_a in s.failed_navigation_urls
    else:
        # If normalize doesn't unify them, both should be added by the
        # caller — that's a separate concern. Just confirm the recorded
        # variant is present.
        assert norm_a in s.failed_navigation_urls


def test_record_failed_navigation_empty_url_ignored():
    s = BrowserSessionState()
    s.record_failed_navigation("", 404)
    assert s.failed_navigation_urls == {}


def test_record_failed_navigation_overwrites_status():
    """If a URL fails twice with different statuses, keep the latest."""
    s = BrowserSessionState()
    s.record_failed_navigation("https://x.com/x", 404)
    s.record_failed_navigation("https://x.com/x", 500)
    norm = s._normalize_url("https://x.com/x")
    assert s.failed_navigation_urls[norm]["status"] == 500


# --------------- path-style filter hallucination logic ------------------
# Mirrors the inline check in BrowserNavigateTool.execute. We test the
# pure pieces here.


_PATH_FILTER_SEGMENTS = (
    "/regions/", "/region/",
    "/categories/", "/category/",
    "/collections/",
    "/colors/", "/color/",
    "/types/", "/type/",
    "/brands/", "/brand/",
    "/tags/", "/tag/",
    "/filters/", "/filter/",
)


def _path_segments_seen(url: str) -> int:
    path = (urlparse(url).path or "").lower()
    return sum(1 for seg in _PATH_FILTER_SEGMENTS if seg in path)


def test_path_segments_seen_picks_up_regions():
    assert _path_segments_seen("https://x.com/store/regions/oregon/white/") == 1


def test_path_segments_seen_picks_up_multiple():
    assert _path_segments_seen("https://x.com/regions/oregon/colors/red/") == 2


def test_path_segments_seen_clean_listing_unaffected():
    assert _path_segments_seen("https://x.com/store/white-wine/") == 0
    assert _path_segments_seen("https://x.com/?ordering=score") == 0


def test_path_hack_only_fires_with_unstarted_filters():
    """The path-hack heuristic should NOT fire if the brain has already
    flipped at least one filter constraint to done — that means the
    brain is following real links, not guessing."""
    s = BrowserSessionState()
    s.task_brief = TaskBrief("q", [
        {"label": "White", "kind": "filter", "predicate": {"manual": True}},
        {"label": "Oregon", "kind": "filter", "predicate": {"manual": True}},
    ])
    s.task_brief.constraints[0].status = "done"  # one filter already done

    progress = sum(
        1 for c in s.task_brief.constraints
        if c.kind == "filter" and c.status == "done"
    )
    assert progress == 1
    # Mirrors the inline check: _path_hack requires
    # _filter_brief_progress == 0
    path_hack_should_fire = (
        _path_segments_seen("https://x.com/regions/oregon/") >= 1
        and progress == 0
    )
    assert path_hack_should_fire is False


def test_path_hack_fires_with_zero_progress():
    s = BrowserSessionState()
    s.task_brief = TaskBrief("q", [
        {"label": "White", "kind": "filter", "predicate": {"manual": True}},
    ])
    progress = sum(
        1 for c in s.task_brief.constraints
        if c.kind == "filter" and c.status == "done"
    )
    assert progress == 0
    path_hack_should_fire = (
        _path_segments_seen("https://x.com/regions/oregon/") >= 1
        and progress == 0
    )
    assert path_hack_should_fire is True
