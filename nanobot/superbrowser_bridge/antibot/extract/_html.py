"""Shared lxml parsing/cleaning helpers for the extract pipeline.

These were factored out of the original ``antibot/content.py`` so the
markdown / pruning / bm25 / media / structured modules can share one
parse + clean implementation. Stdlib + lxml only.
"""

from __future__ import annotations

import copy
import re
from typing import Optional

from lxml import etree, html as lxml_html

_WS = re.compile(r"\s+")

# Tags whose entire subtree we drop before rendering markdown. Boilerplate
# that rarely carries the signal an LLM is looking for.
DROP_TAGS = frozenset({
    "script", "style", "noscript", "template", "iframe",
    "nav", "footer", "aside",
    "form",  # the agent isn't submitting forms from a read-only fetch
})


def clean_text(s: Optional[str]) -> str:
    if not s:
        return ""
    return _WS.sub(" ", s).strip()


def parse(html: str):
    """Parse an HTML string into an lxml element, tolerating fragments."""
    if not html:
        return None
    try:
        return lxml_html.fromstring(html)
    except (etree.ParserError, ValueError):
        try:
            return lxml_html.fromstring(f"<div>{html}</div>")
        except Exception:
            return None


def as_tree(html_or_tree):
    """Accept either an HTML string or an already-parsed lxml element."""
    if html_or_tree is None:
        return None
    if isinstance(html_or_tree, str):
        return parse(html_or_tree)
    return html_or_tree


def strip_drop_tags(tree, tags=DROP_TAGS) -> None:
    """Remove ``tags`` subtrees and comments from ``tree`` in place."""
    if tree is None:
        return
    for tag in tags:
        for el in tree.xpath(f".//{tag}"):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
    for el in tree.xpath(".//comment()"):
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)


def find_body(tree):
    """Return the document <body> if present, else the tree root itself."""
    if tree is None:
        return None
    bodies = tree.xpath("//body")
    if bodies:
        return bodies[0]
    return tree


def to_html(el) -> str:
    try:
        return lxml_html.tostring(el, encoding="unicode")
    except Exception:
        return ""


def deepcopy_tree(tree):
    """A defensive deep copy — pruning mutates the tree, callers may not want that."""
    return copy.deepcopy(tree)
