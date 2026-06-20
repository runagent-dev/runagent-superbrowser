"""Pruning content filter — the source of "fit markdown".

lxml port of crawl4ai's PruningContentFilter (content_filter_strategy.py:
541-786). Recursively scores DOM subtrees on a composite of text density,
link density, tag weight, negative class/id hints, and text length, then
drops subtrees below a dynamic threshold. Boilerplate (nav/sidebar/footer/
promo) scores low and is removed; the article body survives.

crawl4ai uses BeautifulSoup; we port to lxml (already a dep) so we can reuse
the single parsed tree and avoid a bs4 dependency. Deliberate deviations
from the original are commented inline.
"""

from __future__ import annotations

import math
import re

from lxml import html as lxml_html

from . import _html

# crawl4ai's RelevantContentFilter.excluded_tags
_EXCLUDED_TAGS = frozenset({
    "nav", "footer", "header", "aside", "script", "style", "form", "iframe", "noscript",
})

# crawl4ai's negative_patterns. We match with .search() (not the original's
# anchored .match()) so "site-nav" / "main-footer" style classes are caught too.
_NEGATIVE = re.compile(
    r"nav|footer|header|sidebar|ads|comment|promo|advert|social|share", re.I
)

_TAG_WEIGHTS = {
    "div": 0.5, "p": 1.0, "article": 1.5, "section": 1.0, "span": 0.3,
    "li": 0.5, "ul": 0.5, "ol": 0.5,
    "h1": 1.2, "h2": 1.1, "h3": 1.0, "h4": 0.9, "h5": 0.8, "h6": 0.7,
}
_TAG_IMPORTANCE = {
    "article": 1.5, "main": 1.4, "section": 1.3, "p": 1.2,
    "h1": 1.4, "h2": 1.3, "h3": 1.2, "div": 0.7, "span": 0.6,
}
_WEIGHTS = {
    "text_density": 0.4, "link_density": 0.2, "tag_weight": 0.2,
    "class_id_weight": 0.1, "text_length": 0.1,
}


def _stripped_len(node) -> int:
    """len of the node's text with inter-fragment whitespace stripped
    (lxml analogue of bs4 ``get_text(strip=True)``)."""
    return len("".join(t.strip() for t in node.itertext() if t and t.strip()))


def _inner_html_len(node) -> int:
    """Length of the node's INNER html (children only) — matches crawl4ai's
    ``encode_contents()`` rather than ``tostring(node)`` which includes the
    node's own tag."""
    try:
        inner = node.text or ""
        for c in node:
            inner += lxml_html.tostring(c, encoding="unicode")
        return len(inner)
    except Exception:
        return 0


def _link_text_len(node) -> int:
    """Total text length inside DIRECT-child <a> tags (crawl4ai uses
    recursive=False). We take full anchor text via itertext (slightly more
    robust than the original's ``a.string`` which is None for nested anchors)."""
    total = 0
    for c in node:
        if isinstance(c.tag, str) and c.tag == "a":
            total += len("".join(c.itertext()).strip())
    return total


def _class_id_weight(node) -> float:
    score = 0.0
    cls = node.get("class")
    if cls and _NEGATIVE.search(cls):
        score -= 0.5
    el_id = node.get("id")
    if el_id and _NEGATIVE.search(el_id):
        score -= 0.5
    return score


def _composite_score(node, text_len, tag_len, link_text_len, min_word_threshold) -> float:
    if min_word_threshold:
        wc = len(" ".join(node.itertext()).split())
        if wc < min_word_threshold:
            return -1.0  # guaranteed removal
    score = 0.0
    total = 0.0

    density = text_len / tag_len if tag_len > 0 else 0
    score += _WEIGHTS["text_density"] * density
    total += _WEIGHTS["text_density"]

    link_density = 1 - (link_text_len / text_len if text_len > 0 else 0)
    score += _WEIGHTS["link_density"] * link_density
    total += _WEIGHTS["link_density"]

    tag_score = _TAG_WEIGHTS.get(node.tag, 0.5)
    score += _WEIGHTS["tag_weight"] * tag_score
    total += _WEIGHTS["tag_weight"]

    class_score = _class_id_weight(node)
    score += _WEIGHTS["class_id_weight"] * max(0.0, class_score)
    total += _WEIGHTS["class_id_weight"]

    score += _WEIGHTS["text_length"] * math.log(text_len + 1)
    total += _WEIGHTS["text_length"]

    return score / total if total > 0 else 0.0


def _prune_node(node, threshold, threshold_type, min_word_threshold) -> None:
    tag = node.tag
    if not isinstance(tag, str):
        return

    text_len = _stripped_len(node)
    tag_len = _inner_html_len(node)
    link_text_len = _link_text_len(node)
    score = _composite_score(node, text_len, tag_len, link_text_len, min_word_threshold)

    if threshold_type == "fixed":
        should_remove = score < threshold
    else:  # dynamic
        importance = _TAG_IMPORTANCE.get(tag, 0.7)
        text_ratio = text_len / tag_len if tag_len > 0 else 0
        link_ratio = link_text_len / text_len if text_len > 0 else 1
        th = threshold
        if importance > 1:
            th *= 0.8
        if text_ratio > 0.4:
            th *= 0.9
        if link_ratio > 0.6:
            th *= 1.2
        should_remove = score < th

    if should_remove:
        parent = node.getparent()
        if parent is not None:
            parent.remove(node)
        return

    for child in list(node):
        if isinstance(child.tag, str):
            _prune_node(child, threshold, threshold_type, min_word_threshold)


def prune(
    tree,
    *,
    query: str | None = None,        # accepted for API symmetry; pruning is query-agnostic
    threshold: float = 0.48,
    threshold_type: str = "dynamic",  # crawl4ai default is "fixed"; dynamic is the upgrade
    min_word_threshold: int | None = None,
) -> list[str]:
    """Prune ``tree`` (MUTATES it — pass a copy) and return the surviving
    top-level block HTML strings."""
    if tree is None:
        return []
    _html.strip_drop_tags(tree, _EXCLUDED_TAGS)
    body = _html.find_body(tree)
    if body is None:
        return []

    # Prune the body's CHILDREN (never the body itself, so a near-empty page
    # can't nuke its own root before we read it).
    for child in list(body):
        if isinstance(child.tag, str):
            _prune_node(child, threshold, threshold_type, min_word_threshold)

    blocks: list[str] = []
    for el in list(body):
        if not isinstance(el.tag, str):
            continue
        if (el.text_content() or "").strip():
            html_str = _html.to_html(el)
            if html_str:
                blocks.append(html_str)
    return blocks
