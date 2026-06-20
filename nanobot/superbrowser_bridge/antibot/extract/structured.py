"""Structured-data extraction: JSON-LD, OpenGraph/Twitter, meta, CSS schema.

Moved out of the original ``content.py`` (signatures preserved). Every
function accepts either an HTML string or an already-parsed lxml tree, so
the top-level ``extract()`` can pass the tree it already parsed once.
"""

from __future__ import annotations

import json

from . import _html


def extract_json_ld(html_or_tree) -> list[dict]:
    """Pull JSON-LD blocks out of the HTML. Returns list of parsed objects."""
    tree = _html.as_tree(html_or_tree)
    if tree is None:
        return []
    out: list[dict] = []
    for node in tree.xpath('.//script[@type="application/ld+json"]'):
        raw = (node.text or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, list):
            out.extend(d for d in data if isinstance(d, dict))
        elif isinstance(data, dict):
            out.append(data)
    return out


def extract_opengraph(html_or_tree) -> dict[str, str]:
    """Pull OpenGraph `og:*` and Twitter Card `twitter:*` meta into a dict."""
    tree = _html.as_tree(html_or_tree)
    if tree is None:
        return {}
    out: dict[str, str] = {}
    for m in tree.xpath(".//meta"):
        prop = m.get("property") or m.get("name") or ""
        if prop.startswith(("og:", "twitter:")):
            content = m.get("content") or ""
            if content:
                out[prop] = content.strip()
    return out


def extract_meta_title_description(html_or_tree) -> dict[str, str]:
    """Return {title, description, canonical} from the page head."""
    tree = _html.as_tree(html_or_tree)
    if tree is None:
        return {}
    out: dict[str, str] = {}
    titles = tree.xpath(".//title")
    if titles:
        t = _html.clean_text(titles[0].text)
        if t:
            out["title"] = t
    for m in tree.xpath('.//meta[@name="description"]'):
        c = (m.get("content") or "").strip()
        if c:
            out["description"] = c
            break
    for link in tree.xpath('.//link[@rel="canonical"]'):
        h = (link.get("href") or "").strip()
        if h:
            out["canonical"] = h
            break
    return out


def extract_by_css(html_or_tree, schema: dict[str, str]) -> dict[str, list[str]]:
    """Given a `{field: css_selector}` schema, return `{field: [matched text, ...]}`.

    Thin port of crawl4ai's JsonCssExtractionStrategy for the agent's
    common case: "pull out the prices / titles / descriptions as a list."
    """
    tree = _html.as_tree(html_or_tree)
    if tree is None:
        return {k: [] for k in schema}
    out: dict[str, list[str]] = {}
    for field, selector in schema.items():
        try:
            els = tree.cssselect(selector)
        except Exception:
            out[field] = []
            continue
        vals: list[str] = []
        for el in els:
            text = _html.clean_text("".join(el.itertext()))
            if text:
                vals.append(text)
        out[field] = vals
    return out


def extract_all(html_or_tree, base_url: str | None = None) -> dict:
    """Bundle the three metadata extractors into one structured dict."""
    tree = _html.as_tree(html_or_tree)
    return {
        "json_ld": extract_json_ld(tree),
        "opengraph": extract_opengraph(tree),
        "meta": extract_meta_title_description(tree),
    }
