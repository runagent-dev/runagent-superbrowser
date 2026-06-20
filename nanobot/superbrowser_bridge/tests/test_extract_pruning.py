"""Pruning ("fit markdown") tests for antibot.extract.pruning + extract().

    source venv/bin/activate && \
        python -m pytest nanobot/superbrowser_bridge/tests/test_extract_pruning.py
"""

from __future__ import annotations

from superbrowser_bridge.antibot.extract import extract
from superbrowser_bridge.antibot.extract.pruning import prune
from superbrowser_bridge.antibot.extract import _html

_PAGE = """
<html><body>
<nav class="site-nav"><a href="/a">Home</a> <a href="/b">About</a> <a href="/c">Blog</a></nav>
<aside class="sidebar"><a href="/x">Ad one</a> <a href="/y">Ad two</a> <a href="/z">Promo three</a></aside>
<article>
<h1>The Real Article</h1>
<p>This is the genuine body of the article with several sentences of real
information that a reader would actually care about and want to keep here.</p>
<p>A second substantial paragraph continues the discussion with more useful
detail, examples, and context so the extracted content is clearly meaningful.</p>
</article>
<footer class="footer"><a href="/p">Privacy</a> <a href="/t">Terms</a> <a href="/s">Sitemap</a></footer>
</body></html>
"""


def test_fit_markdown_keeps_article_drops_boilerplate():
    r = extract(_PAGE, url="https://x.com/post")
    assert "genuine body of the article" in r.fit_markdown
    assert "Home" not in r.fit_markdown
    assert "Privacy" not in r.fit_markdown
    assert "Ad one" not in r.fit_markdown


def test_prune_returns_blocks():
    tree = _html.parse(_PAGE)
    blocks = prune(tree)
    joined = "\n".join(blocks)
    assert "Real Article" in joined
    assert "site-nav" not in joined


def test_over_prune_falls_back_to_raw():
    # An all-boilerplate page: pruning should empty it, so fit falls back to raw.
    boiler = (
        '<html><body><nav class="nav"><a href="/a">x</a></nav>'
        '<footer class="footer"><a href="/b">y</a></footer></body></html>'
    )
    r = extract(boiler, url="https://x.com")
    # fit_markdown must never be empty — it falls back to raw_markdown.
    assert r.fit_markdown == r.raw_markdown


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
