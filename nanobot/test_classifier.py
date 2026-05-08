"""
Unit tests for the task classifier (_classify_task).

Run with:
    cd /root/agentic-browser/runagent-superbrowser/nanobot
    python3 -m pytest test_classifier.py -v

No pytest? Fall back to standalone:
    python3 test_classifier.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make superbrowser_bridge importable whether run from nanobot/ or elsewhere.
sys.path.insert(0, str(Path(__file__).parent))

from superbrowser_bridge.routing import (
    _classify_task,
    _looks_blocked,
    _rewrite_for_search,
)


# (name, instructions, url, expected_approach) — one row per behavior.
CASES = [
    # --- Aggregation / data-retrieval → search --------------------------
    (
        "mercari_average_price",
        "Calculate the average price of used iphone 16 pro listings on mercari.com",
        "https://mercari.com",
        "search",
    ),
    (
        "compare_prices",
        "Compare prices of the Dyson V15 across amazon, bestbuy, and target",
        None,
        "search",
    ),
    (
        "cheapest_flights",
        "Find the cheapest flights from SFO to Tokyo next weekend",
        None,
        # Flight queries for specific dates need live fares — Google's
        # cached snippets are stale by hours. Browser via Kayak/Google
        # Flights is the correct path even though "cheapest" is an
        # aggregation verb.
        "browser",
    ),
    (
        "list_restaurants",
        "List the top-rated restaurants in Chicago",
        None,
        "search",
    ),
    (
        "total_units_sold",
        "What is the total number of iPhone 15 units sold worldwide in 2024",
        None,
        "search",
    ),
    # --- Factual lookups → search ---------------------------------------
    (
        "super_bowl_winner",
        "Who won the 2024 Super Bowl",
        None,
        "search",
    ),
    (
        "what_year",
        "What year was Python invented",
        None,
        "search",
    ),
    (
        "summarize_reviews",
        "Summarize the customer reviews of the Sony WH-1000XM5 headphones",
        None,
        "search",
    ),
    # --- Action verbs on a URL → browser --------------------------------
    (
        "book_flight",
        "Book a flight on kayak.com from NYC to LAX for December 15",
        "https://kayak.com",
        "browser",
    ),
    (
        "submit_form",
        "Open https://httpbin.org/forms/post and submit the form with name=Alice",
        "https://httpbin.org/forms/post",
        "browser",
    ),
    (
        "sign_in_dashboard",
        "Sign in to my account on example.com and check the dashboard",
        "https://example.com",
        "browser",
    ),
    (
        "add_to_cart",
        "Add the black widget to cart on widget.shop",
        "https://widget.shop",
        "browser",
    ),
    # --- Visual-only → browser ------------------------------------------
    (
        "screenshot_page",
        "Take a screenshot of the homepage of stripe.com",
        "https://stripe.com",
        "browser",
    ),
    (
        "what_does_it_look_like",
        "What does the checkout page of shopify.com look like",
        "https://shopify.com",
        "browser",
    ),
    # --- Hybrid ---------------------------------------------------------
    (
        "find_on_site",
        "Find the return policy on zappos.com",
        "https://zappos.com",
        "hybrid",
    ),
    # --- Defaults -------------------------------------------------------
    (
        "bare_url",
        "Check example.com status",
        "https://example.com",
        "browser",  # URL given, no other strong signal → default browser
    ),
    (
        "open_ended_no_url",
        "Tell me about recent developments in quantum computing",
        None,
        "search",  # no URL → default search
    ),
    # --- Transactional / live-inventory (rule 1.5) → browser ----------
    (
        "gozayaan_hotel_dates",
        "go to gozayan and tell me about the sylhet hotel price for 2 nights from 15th of april to 17th of april for one person.",
        None,  # user didn't give a URL, just the brand
        "browser",
    ),
    (
        "hotel_in_paris_date",
        "Find a hotel in Paris for April 15-17, one guest",
        None,
        "browser",
    ),
    (
        "flight_date_range",
        "Flight from NYC to SFO next weekend for one person",
        None,
        "browser",
    ),
    (
        "room_for_nights",
        "Book a room for 3 nights in Bali from May 1 to May 4",
        None,
        "browser",  # action verb "Book" + dates — also hits rule 1 if URL given
    ),
    # --- Known brand mention without URL (rule 1.6) → browser ----------
    (
        "amazon_brand_mention",
        "Check the Dyson V15 on amazon",
        None,
        "browser",
    ),
    (
        "airbnb_bare",
        "Find a 3-bedroom airbnb in Austin under $200/night",
        None,
        "browser",  # airbnb brand — even though "find under $/night" looks aggregation-y
    ),
    # --- Sanity: factual reviews still go to search --------------------
    (
        "summarize_hotel_reviews",
        "Summarize customer reviews of the Park Hyatt Tokyo",
        None,
        "search",  # factual lookup wins over brand mention when brand isn't explicit
    ),
    # --- Generalized company/product targeting (rule 4.5 with fallback URL) ---
    (
        "unknown_brand_go_to",
        "go to chaldal and tell me what grocery items are available",
        None,
        "browser",  # "go to chaldal" → explicit target command, unknown brand
    ),
    (
        "unknown_brand_visit",
        "Visit foodpanda and check delivery options near Dhanmondi",
        None,
        "browser",  # explicit "Visit X" with unknown brand
    ),
    (
        "bare_domain",
        "pull up zalora.com.bd and check the men's section",
        None,
        "browser",  # bare domain detection
    ),
    (
        "prep_capitalized_brand",
        "Is the PlayStation 5 available on BestMart",
        None,
        "browser",  # "on BestMart" — capitalized unknown brand via prep
    ),
    # --- Sanity: prep-skip-words don't false-trigger --------------------
    (
        "not_a_brand_sunday",
        "What sports are on Sunday",
        None,
        "search",  # "on Sunday" must NOT be treated as a brand
    ),
]


def _run_case(name: str, instructions: str, url: str | None, expected: str) -> bool:
    result = _classify_task(instructions, url)
    got = result["approach"]
    ok = got == expected
    status = "PASS" if ok else "FAIL"
    print(
        f"[{status}] {name}: expected={expected}, got={got}, "
        f"conf={result['confidence']:.2f}, reason={result['reason']}"
    )
    return ok


def test_rewrite_strips_action_verbs():
    out = _rewrite_for_search("Open mercari.com and click the electronics tab", "https://mercari.com")
    assert "open" not in out.lower()
    assert "click" not in out.lower()
    assert "mercari.com" in out.lower()


# --- Blocked-content detector (Layer 5.3) ---------------------------------

# Real-page sample big enough to clear every length gate (>500 chars, >200
# visible chars after HTML strip).
_REAL_PAGE = (
    "<html><head><title>iPhone 16 Pro — Mercari</title></head><body>"
    "<h1>iPhone 16 Pro 256GB Natural Titanium</h1>"
    "<p>Price: $899.00. Condition: Used - Like New. Seller: authorized_reseller.</p>"
    "<p>Ships from California. Returns accepted within 30 days. Buyer pays shipping.</p>"
    "<p>Model: A3083. Carrier: Unlocked. Battery health: 98%.</p>"
    "<p>Face ID works perfectly, cameras tested, no scratches on the display. "
    "Phone has been factory-reset and includes original box and charging cable. "
    "I am listing this because I upgraded to the Pro Max. Happy to answer any "
    "questions before purchase — serious buyers only please.</p>"
    "</body></html>"
)

_CLOUDFLARE_STUB = (
    "<!DOCTYPE html><html><head><title>Just a moment...</title>"
    "<meta http-equiv=\"refresh\" content=\"3\"></head>"
    "<body><div id=\"cf-wrapper\">Checking your browser before accessing mercari.com...</div>"
    "<script>window._cf_chl_opt = {...};</script></body></html>"
) * 2  # ensure >500 chars so length gate doesn't short-circuit

_403_PAGE = "<html><body><h1>403 Forbidden</h1><p>Access Denied</p></body></html>"

_JS_ONLY_SPA = (
    "<!DOCTYPE html><html><head><title>Loading...</title>"
    "<script src=\"/app.js\"></script></head>"
    "<body><div id=\"root\"></div>"
    "<script>console.log('init')</script></body></html>"
) * 2

_BLOCK_CASES = [
    ("real_product_page", _REAL_PAGE, False),
    ("cloudflare_stub", _CLOUDFLARE_STUB, True),
    ("403_page", _403_PAGE, True),
    ("js_only_spa", _JS_ONLY_SPA, True),
    ("empty", "", True),
    ("captcha_marker", "<html><body>" + ("<p>Please verify you are human.</p>" * 40) + "</body></html>", True),
]


def _run_block_case(name: str, content: str, expected_blocked: bool) -> bool:
    blocked, reason = _looks_blocked(content)
    ok = blocked == expected_blocked
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] block::{name}: expected_blocked={expected_blocked}, got={blocked}, reason={reason!r}")
    return ok


def test_block_detector():
    failures = [name for name, c, exp in _BLOCK_CASES if not _run_block_case(name, c, exp)]
    assert not failures, f"Block-detector failures: {failures}"


def test_all_cases():
    failures: list[str] = []
    for name, instructions, url, expected in CASES:
        if not _run_case(name, instructions, url, expected):
            failures.append(name)
    assert not failures, f"Failed cases: {failures}"


if __name__ == "__main__":
    # Standalone runner for environments without pytest.
    failures: list[str] = []
    for name, instructions, url, expected in CASES:
        if not _run_case(name, instructions, url, expected):
            failures.append(name)
    print()
    print(f"Results: {len(CASES) - len(failures)}/{len(CASES)} passed")
    if failures:
        print(f"Failed: {failures}")
        sys.exit(1)
    # Also run the rewrite test.
    try:
        test_rewrite_strips_action_verbs()
        print("rewrite test: PASS")
    except AssertionError as exc:
        print(f"rewrite test: FAIL ({exc})")
        sys.exit(1)
    # Block-detector cases.
    print()
    block_failures = [
        name for name, c, exp in _BLOCK_CASES
        if not _run_block_case(name, c, exp)
    ]
    if block_failures:
        print(f"Block-detector failed: {block_failures}")
        sys.exit(1)
    print("ALL TESTS PASSED")
