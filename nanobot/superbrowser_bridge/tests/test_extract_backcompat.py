"""Back-compat: content.py shim must keep its old public surface intact.

These guard the existing callers (fetch_auto.py, tools.py, the browser
interactive_session) that import antibot.content.

    source venv/bin/activate && \
        python -m pytest nanobot/superbrowser_bridge/tests/test_extract_backcompat.py
"""

from __future__ import annotations

from superbrowser_bridge.antibot import content as c

_HTML = """
<html><head><title>T</title>
<meta name="description" content="desc">
<meta property="og:title" content="OG">
<script type="application/ld+json">{"@type":"Thing","name":"N"}</script>
</head><body>
<nav><a href="/a">Home</a></nav>
<article><h2>Refund Policy</h2>
<p>You can request a full refund within thirty days of purchase for any reason at all.</p>
<p>Shipping costs are calculated at checkout based on the destination and order weight here.</p>
</article>
<footer>footer text</footer></body></html>
"""


def test_to_markdown_strips_boilerplate_returns_str():
    md = c.to_markdown(_HTML)
    assert isinstance(md, str)
    assert "Refund Policy" in md
    assert "Home" not in md  # nav dropped


def test_prune_html_returns_str_without_nav():
    out = c.prune_html(_HTML)
    assert isinstance(out, str)
    assert "Home" not in out


def test_bm25_filter_returns_relevant_str():
    out = c.bm25_filter(_HTML, "refund policy", top_k=3)
    assert isinstance(out, str)
    assert "refund" in out.lower()


def test_structured_extractors_types():
    assert isinstance(c.extract_json_ld(_HTML), list)
    assert c.extract_json_ld(_HTML)[0]["@type"] == "Thing"
    assert isinstance(c.extract_opengraph(_HTML), dict)
    assert c.extract_opengraph(_HTML).get("og:title") == "OG"
    meta = c.extract_meta_title_description(_HTML)
    assert meta.get("title") == "T" and meta.get("description") == "desc"
    css = c.extract_by_css(_HTML, {"h": "h2"})
    assert css == {"h": ["Refund Policy"]}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
