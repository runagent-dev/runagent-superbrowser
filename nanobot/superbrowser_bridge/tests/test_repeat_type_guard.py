"""Cross-index repeat-type ledger tests + detail-nav refusal tests.

Covers the Phase-2/Phase-3 fixes layered on top of TaskBrief:
  * BrowserSessionState.check_repeat_type / record_typed_value
  * BrowserNavigateTool's [DETAIL_NAV_REFUSED] guard
"""

from __future__ import annotations

import time
import pytest

from superbrowser_bridge.session_tools import BrowserSessionState


# ---------------------------- repeat-type ledger ------------------------


def test_first_two_types_pass_third_is_refused():
    s = BrowserSessionState()
    assert s.check_repeat_type("40") is None
    s.record_typed_value("40")
    assert s.check_repeat_type("40") is None
    s.record_typed_value("40")
    blocked = s.check_repeat_type("40")
    assert blocked is not None
    assert "REPEAT_TYPE_REJECTED" in blocked
    assert "40" in blocked


def test_different_values_dont_collide():
    s = BrowserSessionState()
    s.record_typed_value("40")
    s.record_typed_value("80")
    assert s.check_repeat_type("40") is None
    assert s.check_repeat_type("80") is None


def test_normalization_strips_whitespace_and_currency_punct():
    s = BrowserSessionState()
    s.record_typed_value("  40 ")
    s.record_typed_value("40$")
    blocked = s.check_repeat_type("40,")
    assert blocked is not None  # all three normalize to '40'


def test_window_expires():
    s = BrowserSessionState()
    s.recent_typed_values = [("40", time.time() - 100)]  # > 30s ago
    s.record_typed_value("40")
    # Only 1 fresh entry — third still refused at 2 priors, but we only
    # have 1 visible. Should pass.
    assert s.check_repeat_type("40") is None


def test_empty_text_is_ignored():
    s = BrowserSessionState()
    s.record_typed_value("")
    s.record_typed_value("   ")
    assert s.recent_typed_values == []
    assert s.check_repeat_type("") is None


def test_ledger_caps_size():
    s = BrowserSessionState()
    for i in range(200):
        s.recent_typed_values.append((f"v{i}", time.time()))
    # check_repeat_type triggers GC and cap.
    s.check_repeat_type("xyz")
    assert len(s.recent_typed_values) <= 50


# ----------------------- detail-nav refusal logic -----------------------
#
# We test the guard logic by building a minimal stub state with a brief
# attached, then calling the guard directly. Hitting BrowserNavigateTool's
# execute() would require the TS server up — instead we factor the
# detail-detection into a small inline check that mirrors the production
# code path exactly.


from urllib.parse import urlparse


def _looks_detail(url: str) -> bool:
    """Mirrors the inline check in BrowserNavigateTool.execute."""
    try:
        path = (urlparse(url).path or "").lower()
    except Exception:
        path = ""
    detail_roots = (
        "/catalog/", "/product/", "/products/",
        "/p/", "/item/", "/items/",
        "/article/", "/articles/", "/post/", "/posts/",
        "/listing/", "/listings/",
        "/recipe/", "/recipes/",
    )
    return any(
        path.startswith(r) and len(path.rstrip("/").split("/")) >= 3
        for r in detail_roots
    )


def test_detail_url_classification():
    assert _looks_detail("https://wineaccess.com/catalog/2023-ponzi-vineyards/")
    assert _looks_detail("https://store.com/product/xyz-123")
    assert _looks_detail("https://blog.com/article/some-post-slug")
    # NOT detail pages — listing roots:
    assert not _looks_detail("https://wineaccess.com/store/white-wine/")
    assert not _looks_detail("https://wineaccess.com/")
    assert not _looks_detail("https://wineaccess.com/catalog/")
    assert not _looks_detail("https://wineaccess.com/p/")


# ----------------------- filter-hack URL classification -----------------
#
# Mirrors the inline check in BrowserNavigateTool.execute. We test the
# pure logic so it stays exercised by CI even though the full guard
# requires a live TS server.


def _classify_filter_hack(url: str) -> tuple[bool, int]:
    """Returns (multi_value, filter_keys_seen) for the URL's query string.

    The production guard refuses when ``multi_value or filter_keys_seen >= 2``.
    """
    from urllib.parse import urlparse, parse_qsl
    try:
        parsed = urlparse(url)
        params = parse_qsl(parsed.query or "", keep_blank_values=False)
    except Exception:
        return False, 0
    multi = any(
        "," in v for (k, v) in params
        if k.lower() not in ("scope", "code", "state", "redirect_uri", "state_token")
    )
    filter_key_patterns = (
        "category", "region", "country", "type", "kind",
        "color", "size", "brand", "price", "min_", "max_",
        "from_", "to_", "before_", "after_", "in_",
        "filter", "tag", "feature", "amenity", "pairing",
        "rating", "score", "year", "date_",
    )
    seen = sum(
        1 for (k, _v) in params
        if any(p in k.lower() for p in filter_key_patterns)
    )
    return multi, seen


def test_filter_hack_refuses_constructed_multifilter_url():
    # The exact pattern observed in the wineaccess.com trace.
    multi, seen = _classify_filter_hack(
        "https://www.wineaccess.com/store/search/?category__in=white-wine"
        "&ordering=-expert_rating&regions=oregon&food_pairings=fish,dessert&price=0,40"
    )
    assert multi is True
    assert seen >= 2


def test_filter_hack_refuses_two_filter_keys_no_multivalue():
    multi, seen = _classify_filter_hack(
        "https://x.com/?region=oregon&max_price=40"
    )
    assert multi is False
    assert seen >= 2


def test_filter_hack_refuses_multivalue_alone():
    multi, seen = _classify_filter_hack(
        "https://x.com/?regions=oregon,washington"
    )
    assert multi is True


def test_filter_hack_allows_single_filter_param():
    multi, seen = _classify_filter_hack(
        "https://x.com/?region=oregon"
    )
    assert multi is False
    assert seen == 1


def test_filter_hack_ignores_oauth_state():
    # OAuth flows legitimately have ?state=&code=&scope= — these aren't filters
    # AND `state` is in the multi-value exemption list.
    multi, seen = _classify_filter_hack(
        "https://accounts.example.com/oauth/callback?"
        "state=abc,def&code=xyz&scope=read,write"
    )
    assert multi is False  # state and scope are exempt
    assert seen == 0


def test_filter_hack_allows_pagination_and_search_keys():
    # `q`, `page`, `sort` shouldn't be flagged as filters.
    multi, seen = _classify_filter_hack(
        "https://x.com/?q=oregon+wine&page=2&sort=score"
    )
    assert multi is False
    assert seen == 0
