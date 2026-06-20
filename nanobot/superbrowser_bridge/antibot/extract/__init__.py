"""Engine-agnostic HTML -> rich extraction pipeline.

One ``extract(html, ...)`` call turns whatever HTML an engine fetched
(plain httpx, curl_cffi, patchright, archive) into a consistent, LLM-ready
result: raw markdown with numbered citations, a pruned "fit" markdown,
the references block, scored images, and structured metadata.

Patterns ported from crawl4ai (DefaultMarkdownGenerator,
PruningContentFilter, BM25ContentFilter, LXMLWebScrapingStrategy image
scoring) — reimplemented on lxml + html2text, no crawl4ai dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import _html
from . import bm25 as _bm25
from . import markdown as _markdown
from . import media as _media
from . import pruning as _pruning
from . import structured as _structured

# Above this size we skip the expensive passes (prune/bm25/images) and just
# return raw markdown — guards against pathological multi-MB pages.
_MAX_INPUT_CHARS = 3_000_000

# If fit markdown comes back this thin, the pruner probably over-pruned;
# fall back to raw markdown so the agent always gets something.
_FIT_MIN_WORDS = 20


@dataclass
class ExtractResult:
    raw_markdown: str = ""
    fit_markdown: str = ""
    references: str = ""
    media: list = field(default_factory=list)
    structured: dict = field(default_factory=dict)
    fit_html: str = ""
    word_count: int = 0

    def as_dict(self) -> dict:
        return {
            "raw_markdown": self.raw_markdown,
            "fit_markdown": self.fit_markdown,
            "references": self.references,
            "media": self.media,
            "structured": self.structured,
            "word_count": self.word_count,
        }


def extract(
    html: str,
    *,
    url: str | None = None,
    query: str | None = None,
    prune: bool = True,
    score_images: bool = True,
    citations: bool = True,
) -> ExtractResult:
    """Convert HTML into an :class:`ExtractResult`.

    ``url`` is the (post-redirect) base URL used to resolve relative links and
    image srcs. ``query`` makes ``fit_markdown`` query-focused (BM25). The
    pipeline is best-effort — any sub-step failure degrades gracefully rather
    than raising.
    """
    if not html:
        return ExtractResult()

    base_url = url or ""
    oversize = len(html) > _MAX_INPUT_CHARS

    tree = _html.parse(html)
    if tree is None:
        # html2text can still produce something even if lxml choked.
        raw_md, refs = _markdown.html_to_markdown(html, base_url=base_url, citations=citations)
        return ExtractResult(
            raw_markdown=raw_md, fit_markdown=raw_md, references=refs,
            word_count=len(raw_md.split()),
        )

    structured = _structured.extract_all(tree, url)
    raw_markdown, references = _markdown.html_to_markdown(
        html, base_url=base_url, citations=citations
    )

    fit_markdown = ""
    fit_html = ""
    if prune and not oversize:
        try:
            blocks = _pruning.prune(_html.deepcopy_tree(tree), query=query)
        except Exception:
            blocks = []
        if blocks:
            fit_html = "\n".join(f"<div>{b}</div>" for b in blocks)
            fit_markdown, _ = _markdown.html_to_markdown(
                fit_html, base_url=base_url, citations=False
            )

    # Query focus supersedes generic pruning (matches the old _post_process:
    # a query wins over plain markdown). A non-empty BM25 hit is authoritative
    # even when short — that's a focused answer, not over-pruning.
    query_hit = False
    if query and not oversize:
        try:
            bm = _bm25.filter(tree, query, base_url=url)
        except Exception:
            bm = ""
        if bm:
            fit_markdown = bm
            query_hit = True

    # Fallback — never hand back nothing. For the generic (no query) path, a
    # too-thin result means the pruner ate the page, so fall back to raw.
    if not fit_markdown:
        fit_markdown = raw_markdown
    elif not query_hit and len(fit_markdown.split()) < _FIT_MIN_WORDS:
        fit_markdown = raw_markdown

    media: list = []
    if score_images and not oversize:
        try:
            media = _media.score_images(tree, url)
        except Exception:
            media = []

    return ExtractResult(
        raw_markdown=raw_markdown,
        fit_markdown=fit_markdown,
        references=references,
        media=media,
        structured=structured,
        fit_html=fit_html,
        word_count=len(fit_markdown.split()),
    )


__all__ = ["ExtractResult", "extract"]
