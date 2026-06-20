"""BM25 tag-weighting + metadata-fallback tests for antibot.extract.bm25.

    source venv/bin/activate && \
        python -m pytest nanobot/superbrowser_bridge/tests/test_extract_bm25.py
"""

from __future__ import annotations

from superbrowser_bridge.antibot.extract import _html
from superbrowser_bridge.antibot.extract.bm25 import extract_page_query, filter as bm25_filter

# A header and a paragraph both contain the query terms; the header should win
# on tag weighting (h2 = 4.0x).
_HTML = """
<html><head><title>Store Help</title></head><body><article>
<h2>Refund policy details</h2>
<p>Shipping is calculated at checkout based on destination and total order weight.</p>
<p>We mention the words refund policy here too inside a normal paragraph body.</p>
</article></body></html>
"""

# A page whose title/h1 share terms with the body, so the metadata-derived
# query (used when no explicit query is passed) actually matches content.
_META_HTML = """
<html><head><title>Refund Policy Help</title></head><body><article>
<h1>Refund Policy</h1>
<p>You can request a full refund within thirty days of purchase for any reason here.</p>
<p>Returns are easy and your refund is processed promptly to the original payment method.</p>
</article></body></html>
"""


def test_tag_weighting_ranks_header_first():
    out = bm25_filter(_html.parse(_HTML), "refund policy", use_stemming=False, top_k=1)
    # With top_k=1 the single highest-scoring chunk wins — the h2 header.
    assert "Refund policy details" in out


def test_metadata_fallback_when_no_query():
    out = bm25_filter(_html.parse(_META_HTML), None, use_stemming=False)
    # No explicit query -> derives one from title/h1/meta and still returns content.
    assert out.strip() != ""
    assert "refund" in out.lower()


def test_extract_page_query_uses_title_and_header():
    tree = _html.parse(_META_HTML)
    q = extract_page_query(tree, _html.find_body(tree))
    assert "Refund Policy Help" in q  # title
    assert "Refund Policy" in q       # h1


def test_irrelevant_query_returns_empty():
    out = bm25_filter(_html.parse(_HTML), "quantum chromodynamics tensor", use_stemming=False)
    assert out == ""


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
