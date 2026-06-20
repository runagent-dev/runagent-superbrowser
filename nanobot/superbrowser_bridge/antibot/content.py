"""HTML post-processing — back-compat shim over the ``extract`` subpackage.

The real implementation now lives in ``antibot/extract/`` (a deeper port of
crawl4ai's DefaultMarkdownGenerator, PruningContentFilter, BM25ContentFilter
and image scoring). This module preserves the original public surface so the
existing callers — ``fetch_auto.py``, ``tools.py``, and the browser
``interactive_session`` (``browser_get_markdown``) — keep working unchanged.

Prefer importing from ``antibot.extract`` directly in new code.
"""

from __future__ import annotations

import re

from .extract import _html
from .extract import bm25 as _bm25
from .extract import markdown as _markdown
from .extract import pruning as _pruning
from .extract.structured import (  # re-exported, unchanged signatures
    extract_by_css,
    extract_json_ld,
    extract_meta_title_description,
    extract_opengraph,
)

__all__ = [
    "to_markdown",
    "prune_html",
    "bm25_filter",
    "extract_json_ld",
    "extract_opengraph",
    "extract_meta_title_description",
    "extract_by_css",
]


def to_markdown(html: str) -> str:
    """Convert an HTML document to clean markdown.

    Strips `<script>`, `<style>`, `<nav>`, `<footer>`, `<aside>`, `<form>`
    (same boilerplate the old implementation dropped), then renders with
    html2text. Links stay inline `[text](url)` — no citation glyphs in this
    legacy path.
    """
    tree = _html.parse(html)
    if tree is None:
        md, _ = _markdown.html_to_markdown(html or "", citations=False)
        return md
    _html.strip_drop_tags(tree)  # nav/footer/aside/form/script/style/...
    cleaned = _html.to_html(tree)
    md, _ = _markdown.html_to_markdown(cleaned, citations=False)
    return md


def prune_html(html: str, min_words: int = 5) -> str:
    """Return HTML with likely-boilerplate subtrees removed (composite-score
    pruning). Returns the surviving blocks joined as HTML."""
    tree = _html.parse(html)
    if tree is None:
        return html
    blocks = _pruning.prune(tree, min_word_threshold=min_words)
    if not blocks:
        return html
    return "\n".join(f"<div>{b}</div>" for b in blocks)


def bm25_filter(
    html_or_markdown: str,
    query: str,
    *,
    top_k: int = 20,
    min_words: int = 6,
    min_score: float = 0.0,
) -> str:
    """Return the top-k passages BM25-ranked by query, in document order.

    Now tag-weighted (headers score higher) via the ``extract.bm25`` port.
    Accepts raw HTML or plain text/markdown. ``min_score`` maps to the BM25
    threshold; the default (0.0) preserves the old "loose top-k by score"
    behavior.
    """
    text = html_or_markdown or ""
    if "<" in text and ">" in text:
        tree = _html.parse(text)
    else:
        # Plain text/markdown: rebuild paragraph blocks so chunk extraction works.
        paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        body = "".join(f"<p>{p}</p>" for p in paras)
        tree = _html.parse(f"<body>{body}</body>")
    if tree is None:
        return ""
    return _bm25.filter(
        tree,
        query,
        bm25_threshold=min_score,
        min_words=min_words,
        top_k=top_k,
        use_stemming=False,  # the legacy path never stemmed
    )
