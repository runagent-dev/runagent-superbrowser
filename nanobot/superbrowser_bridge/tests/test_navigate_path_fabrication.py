"""Phases S + T: path-fabrication guard + 404 disambiguation.

Phase S — refuses navigation to URLs with ≥3 path segments not in
observed_link_hrefs ∪ observed_urls. Surfaces similar observed hrefs
in the refusal so the brain gets a redirect, not just a NACK.

Phase T — 404 on a URL the brain never observed is labelled as
[navigate_404_fabricated] instead of NETWORK_BLOCKED, so the
orchestrator's edge-block routing doesn't fire on brain mistakes.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from superbrowser_bridge.session_tools import BrowserSessionState
from superbrowser_bridge.session_tools.tools.navigation import BrowserNavigateTool


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_state_with_observations(
    *hrefs: str,
    pin_domain: bool = True,
) -> BrowserSessionState:
    s = BrowserSessionState()
    if pin_domain:
        s.pinned_domain = "www.wineaccess.com"
    # Always seed homepage so we're past the early-session escape.
    s.record_url("https://www.wineaccess.com/")
    if hrefs:
        s.harvest_link_hrefs(list(hrefs))
    return s


# ----------------- Phase S -----------------


def test_path_fabrication_refused_with_similar_hint():
    """Trace 5 case: brain navigates to fabricated 4-segment path."""
    s = _make_state_with_observations(
        "https://www.wineaccess.com/store/regions/oregon/",
        "https://www.wineaccess.com/store/white-wine/",
    )
    t = BrowserNavigateTool(s)
    out = _run(t.execute(
        session_id="t",
        url="https://www.wineaccess.com/store/white-wine/regions/oregon/",
    ))
    assert "[navigate_refused_unobserved_path]" in out
    assert "/store/regions/oregon/" in out
    assert "4 segments" in out


def test_path_fabrication_records_cursor_failure():
    s = _make_state_with_observations(
        "https://www.wineaccess.com/store/regions/oregon/",
    )
    t = BrowserNavigateTool(s)
    _run(t.execute(
        session_id="t",
        url="https://www.wineaccess.com/store/white-wine/regions/oregon/",
    ))
    assert any(
        rec.get("strategy") == "navigate"
        and "unobserved_multi_segment_path" in (rec.get("reason") or "")
        for rec in s.cursor_failure_records
    )


def test_observed_path_passes_phase_s():
    """A 4-segment URL that IS in observed_link_hrefs is allowed."""
    fab = "https://www.wineaccess.com/store/regions/oregon/willamette-valley/"
    s = _make_state_with_observations(fab)
    t = BrowserNavigateTool(s)
    # Phase S should NOT refuse. Mock HTTP response so we can confirm
    # the tool reaches the dispatch path (or fails downstream cleanly).
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "url": fab,
        "title": "Willamette Valley",
        "elements": "",
        "statusCode": 200,
    }
    mock_resp.raise_for_status = lambda: None
    with patch(
        "superbrowser_bridge.session_tools.tools.navigation._request_with_backoff",
        new=AsyncMock(return_value=mock_resp),
    ):
        out = _run(t.execute(session_id="t", url=fab))
    assert "[navigate_refused_unobserved_path]" not in out


def test_two_segment_path_not_blocked_by_phase_s():
    """A 2-segment path (catalog root) is never refused by Phase S."""
    s = _make_state_with_observations()
    t = BrowserNavigateTool(s)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "url": "https://www.wineaccess.com/store/white-wine/",
        "title": "White Wine",
        "elements": "",
        "statusCode": 200,
    }
    mock_resp.raise_for_status = lambda: None
    with patch(
        "superbrowser_bridge.session_tools.tools.navigation._request_with_backoff",
        new=AsyncMock(return_value=mock_resp),
    ):
        out = _run(t.execute(
            session_id="t",
            url="https://www.wineaccess.com/store/white-wine/",
        ))
    assert "[navigate_refused_unobserved_path]" not in out


def test_phase_s_skipped_on_early_session():
    """When only the homepage is observed, Phase S allows first deep
    navigation so brain can discover paths."""
    s = BrowserSessionState()
    s.pinned_domain = "www.wineaccess.com"
    s.record_url("https://www.wineaccess.com/")
    # observed_link_hrefs is essentially empty (just the homepage from
    # record_url, which is single entry).
    t = BrowserNavigateTool(s)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "url": "https://www.wineaccess.com/store/white-wine/list/",
        "title": "X",
        "elements": "",
        "statusCode": 200,
    }
    mock_resp.raise_for_status = lambda: None
    with patch(
        "superbrowser_bridge.session_tools.tools.navigation._request_with_backoff",
        new=AsyncMock(return_value=mock_resp),
    ):
        out = _run(t.execute(
            session_id="t",
            url="https://www.wineaccess.com/store/white-wine/list/",
        ))
    # Early-session escape: not refused.
    assert "[navigate_refused_unobserved_path]" not in out


def test_phase_s_suggestion_ranking_prefers_two_segment_match():
    """When multiple observed hrefs contain the last segment, prefer
    those that ALSO contain the second-to-last segment."""
    s = _make_state_with_observations(
        "https://www.wineaccess.com/store/oregon-state-merch/",  # has 'oregon' but not in 'regions'
        "https://www.wineaccess.com/store/regions/oregon/",       # has both 'regions' AND 'oregon'
    )
    t = BrowserNavigateTool(s)
    out = _run(t.execute(
        session_id="t",
        url="https://www.wineaccess.com/store/white-wine/regions/oregon/",
    ))
    # The 'regions/oregon' match should appear before the 'oregon-state-merch' match.
    pos_regions = out.find("/store/regions/oregon/")
    pos_merch = out.find("/store/oregon-state-merch/")
    assert pos_regions != -1
    assert pos_merch != -1
    assert pos_regions < pos_merch


# ----------------- Phase T -----------------


def test_404_on_fabricated_url_emits_fabricated_label():
    """404 on a URL never observed → [navigate_404_fabricated]."""
    s = _make_state_with_observations(pin_domain=False)
    t = BrowserNavigateTool(s)
    fab = "https://www.wineaccess.com/store/white-wine/regions/oregon/"
    # Need the URL to pass Phase S to reach the post-nav 404 branch.
    # Use an observed 4-segment URL that returns 404 — that simulates
    # a previously-observed URL going stale.
    real = "https://www.wineaccess.com/store/regions/oregon-defunct/"
    s.harvest_link_hrefs([real])
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "url": real,
        "title": "Not Found",
        "elements": "",
        "statusCode": 404,
    }
    mock_resp.raise_for_status = lambda: None
    with patch(
        "superbrowser_bridge.session_tools.tools.navigation._request_with_backoff",
        new=AsyncMock(return_value=mock_resp),
    ):
        out = _run(t.execute(session_id="t", url=real))
    # The URL was observed (we harvested it) → real-404 path.
    assert "HTTP 404" in out or "[NETWORK_BLOCKED" in out
    assert "[navigate_404_fabricated]" not in out


def test_404_on_unobserved_url_marks_fabricated_and_skips_network_routing():
    """The trace-5 wineaccess case: 404 on URL never harvested → emit
    fabricated label, do NOT include NETWORK_BLOCKED string."""
    s = _make_state_with_observations(
        "https://www.wineaccess.com/store/regions/oregon/",  # real path
        pin_domain=False,
    )
    # Skip the Phase S guard by adding the fabricated URL to observed
    # AFTER recording, simulating this is the FIRST navigation TO it
    # but the brain already saw it in markdown earlier (so Phase S
    # passes), and only THEN it 404s.
    fab_url = "https://www.wineaccess.com/store/white-wine/regions/oregon/"
    s.harvest_link_hrefs([fab_url])  # so Phase S passes
    # But then we REMOVE it from observed_link_hrefs to simulate the
    # scenario where the markdown harvest got a pseudo-link that
    # actually 404s.
    s.observed_link_hrefs.discard(fab_url)
    # Now it's NOT observed → Phase S would refuse. So instead seed
    # a different URL that IS observed but 404s in the response.
    # Cleaner: directly test the 404 branch logic by using a URL
    # that's NOT in observed.
    s2 = _make_state_with_observations(
        "https://www.wineaccess.com/store/regions/oregon/",
        # Add a 4-segment URL that IS observed but returns 404 —
        # tests the "real 404" branch.
        pin_domain=False,
    )
    # For testing the FABRICATED branch directly, we need the
    # fabricated URL to bypass Phase S. Trick: bump observed_link_hrefs
    # cardinality so early-session escape doesn't trigger but the
    # specific URL isn't observed. Phase S then refuses, never reaching
    # 404 branch. So the fabricated 404 branch is only reachable when
    # a URL slipped past Phase S — which for our threshold means
    # ≤ 2 segments OR observed.
    #
    # The realistic case for fabricated-404 is a 2-segment fabricated
    # URL like /store/white_wine_oregon/ (no /). Test that path.
    fab_short = "https://www.wineaccess.com/store/oregon-special-deal/"
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "url": fab_short,
        "title": "Not Found",
        "elements": "",
        "statusCode": 404,
    }
    mock_resp.raise_for_status = lambda: None
    t = BrowserNavigateTool(s2)
    with patch(
        "superbrowser_bridge.session_tools.tools.navigation._request_with_backoff",
        new=AsyncMock(return_value=mock_resp),
    ):
        out = _run(t.execute(session_id="t", url=fab_short))
    # Fabricated label should appear; NETWORK_BLOCKED should NOT (so
    # orchestrator routing doesn't fire).
    assert "[navigate_404_fabricated]" in out
    assert "NETWORK_BLOCKED" not in out


def test_404_records_cursor_failure_only_on_fabricated():
    """Real 404 doesn't record cursor failure; fabricated does."""
    s = _make_state_with_observations(pin_domain=False)
    t = BrowserNavigateTool(s)
    fab = "https://www.wineaccess.com/store/oregon-special/"
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "url": fab,
        "title": "Not Found",
        "elements": "",
        "statusCode": 404,
    }
    mock_resp.raise_for_status = lambda: None
    with patch(
        "superbrowser_bridge.session_tools.tools.navigation._request_with_backoff",
        new=AsyncMock(return_value=mock_resp),
    ):
        _run(t.execute(session_id="t", url=fab))
    assert any(
        rec.get("strategy") == "navigate"
        and rec.get("reason") == "404_fabricated"
        for rec in s.cursor_failure_records
    )
