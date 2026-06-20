"""Markdown + citation tests for antibot.extract.markdown.

    source venv/bin/activate && \
        python -m pytest nanobot/superbrowser_bridge/tests/test_extract_markdown.py
"""

from __future__ import annotations

from superbrowser_bridge.antibot.extract import extract
from superbrowser_bridge.antibot.extract.markdown import html_to_markdown

_HTML = """
<html><head><title>Doc</title></head><body>
<nav><a href="/home">Home</a></nav>
<article>
<h1>Guide</h1>
<p>See the <a href="/sales">sales page</a> for details and read the
<a href="https://acme.com/docs">docs</a> plus the <a href="/docs">docs</a> again.</p>
<p>Email <a href="mailto:x@acme.com">us</a> or open <a href="data:text/plain,hi">this</a>.</p>
</article>
</body></html>
"""


def test_citations_numbered_and_referenced():
    raw, refs = html_to_markdown(_HTML, base_url="https://acme.com/guide", citations=True)
    assert "⟨1⟩" in raw  # at least one inline citation
    assert "## References" in refs
    # The relative /sales link resolves against the origin.
    assert "https://acme.com/sales" in refs


def test_duplicate_url_deduped_to_one_reference():
    _raw, refs = html_to_markdown(_HTML, base_url="https://acme.com/guide", citations=True)
    # /docs and https://acme.com/docs are the same resolved URL -> one entry.
    assert refs.count("https://acme.com/docs") == 1


def test_mailto_and_data_excluded_from_references():
    _raw, refs = html_to_markdown(_HTML, base_url="https://acme.com/guide", citations=True)
    assert "mailto:" not in refs
    assert "data:text/plain" not in refs


def test_citations_off_keeps_inline_links():
    raw, refs = html_to_markdown(_HTML, base_url="https://acme.com/guide", citations=False)
    assert refs == ""
    assert "⟨" not in raw


def test_extract_result_carries_markdown_and_refs():
    r = extract(_HTML, url="https://acme.com/guide")
    assert "Guide" in r.raw_markdown
    assert r.references  # references block populated
    assert r.word_count > 0


if __name__ == "__main__":  # allow plain `python test_extract_markdown.py`
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
