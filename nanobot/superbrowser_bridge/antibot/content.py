"""HTML post-processing: clean markdown, BM25 relevance filter, structured data.

Patterns ported from crawl4ai:
  - DefaultMarkdownGenerator (markdown_generation_strategy.py:55-146)
  - PruningContentFilter + BM25ContentFilter (content_filter_strategy.py:381-542)

Stdlib + lxml only (lxml already in the venv). No BeautifulSoup, no
rank_bm25, no html2text — those are either unnecessary or easy to
reimplement inline.

Design choice: produce "semi-markdown" — paragraph text with heading
prefixes, lists, and links preserved. Not 100% spec-compliant Markdown,
but dramatically cleaner than raw HTML for LLM consumption and ~100x
faster to generate.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Optional

from lxml import etree, html as lxml_html

# Tags whose entire subtree we drop before rendering. These are boilerplate
# that rarely carry the signal the LLM is looking for.
_DROP_TAGS = frozenset({
    "script", "style", "noscript", "template", "iframe",
    "nav", "footer", "aside",
    "form",  # drop forms — the agent isn't submitting them from Tier 2/3
})

# Tags whose rendered output gets a blank-line boundary before and after.
_BLOCK_TAGS = frozenset({
    "p", "div", "section", "article", "main", "header",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "table", "tr", "blockquote",
    "pre", "hr", "dl", "dd", "dt",
})

_HEADING_LEVELS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}

_WS = re.compile(r"\s+")


def _clean_text(s: Optional[str]) -> str:
    if not s:
        return ""
    return _WS.sub(" ", s).strip()


def _parse(html: str):
    if not html:
        return None
    try:
        return lxml_html.fromstring(html)
    except (etree.ParserError, ValueError):
        try:
            return lxml_html.fromstring(f"<div>{html}</div>")
        except Exception:
            return None


def _strip_drop_tags(tree) -> None:
    for tag in _DROP_TAGS:
        for el in tree.xpath(f".//{tag}"):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
    # Comments too.
    for el in tree.xpath(".//comment()"):
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)


def _render(el, out: list[str]) -> None:
    tag = getattr(el, "tag", None)
    if not isinstance(tag, str):
        # Comments, processing instructions — skip, but keep tail text.
        if el.tail:
            out.append(el.tail)
        return

    # Headings.
    if tag in _HEADING_LEVELS:
        level = _HEADING_LEVELS[tag]
        txt = _clean_text("".join(el.itertext()))
        if txt:
            out.append("\n\n" + "#" * level + " " + txt + "\n\n")
        if el.tail:
            out.append(el.tail)
        return

    # Links become [text](href).
    if tag == "a":
        href = (el.get("href") or "").strip()
        txt = _clean_text("".join(el.itertext()))
        if txt and href and not href.startswith("#"):
            out.append(f"[{txt}]({href})")
        elif txt:
            out.append(txt)
        if el.tail:
            out.append(el.tail)
        return

    # Images → ![alt](src). Usually high-signal for product pages.
    if tag == "img":
        src = (el.get("src") or "").strip()
        alt = (el.get("alt") or "").strip()
        if src:
            out.append(f"![{alt}]({src})")
        if el.tail:
            out.append(el.tail)
        return

    # Code + pre.
    if tag == "pre":
        body = "".join(el.itertext())
        out.append("\n\n```\n" + body.strip() + "\n```\n\n")
        if el.tail:
            out.append(el.tail)
        return
    if tag == "code":
        body = "".join(el.itertext())
        out.append("`" + body.strip() + "`")
        if el.tail:
            out.append(el.tail)
        return

    # Emphasis.
    if tag in ("strong", "b"):
        out.append("**" + _clean_text("".join(el.itertext())) + "**")
        if el.tail:
            out.append(el.tail)
        return
    if tag in ("em", "i"):
        out.append("*" + _clean_text("".join(el.itertext())) + "*")
        if el.tail:
            out.append(el.tail)
        return

    # List items.
    if tag == "li":
        out.append("\n- ")
        if el.text:
            out.append(el.text)
        for child in el:
            _render(child, out)
        if el.tail:
            out.append(el.tail)
        return

    # Generic block.
    if tag in _BLOCK_TAGS:
        out.append("\n")
        if el.text:
            out.append(el.text)
        for child in el:
            _render(child, out)
        out.append("\n")
        if el.tail:
            out.append(el.tail)
        return

    # Inline.
    if el.text:
        out.append(el.text)
    for child in el:
        _render(child, out)
    if el.tail:
        out.append(el.tail)


def to_markdown(html: str) -> str:
    """Convert an HTML document to clean markdown-ish plain text.

    Strips `<script>`, `<style>`, `<nav>`, `<footer>`, `<aside>`, `<form>`.
    Preserves headings, links, emphasis, lists, paragraphs, code blocks.
    """
    tree = _parse(html)
    if tree is None:
        return ""
    _strip_drop_tags(tree)
    out: list[str] = []
    _render(tree, out)
    text = "".join(out)
    # Collapse runs of whitespace/blank lines.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# --- Pruning content filter --------------------------------------------------
#
# Heuristic: drop DOM subtrees whose text density + link-to-text ratio + class
# name suggest they're boilerplate (nav, sidebar, promo, share bar).

_BOILERPLATE_CLASS_HINTS = re.compile(
    r"(?:^|[-_ ])(nav|menu|header|footer|sidebar|ads?|advert|promo|cookie|"
    r"consent|share|social|related|recommend|breadcrumb|pagination|comments?)"
    r"(?:$|[-_ ])",
    re.IGNORECASE,
)


def prune_html(html: str, min_words: int = 5) -> str:
    """Return HTML with likely-boilerplate subtrees removed.

    Removes:
      - Subtrees whose class/id hints at nav/ads/consent/share.
      - Paragraph-like blocks with fewer than `min_words` words.
      - Subtrees where >80% of the visible text is inside anchors (link farms).
    """
    tree = _parse(html)
    if tree is None:
        return html
    _strip_drop_tags(tree)

    # Pass 1: class/id-based boilerplate removal.
    to_remove: list = []
    for el in tree.iter():
        if not isinstance(el.tag, str):
            continue
        attrs = " ".join(filter(None, (el.get("class"), el.get("id"))))
        if attrs and _BOILERPLATE_CLASS_HINTS.search(attrs):
            to_remove.append(el)
    for el in to_remove:
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)

    # Pass 2: link-density filter on leafy paragraphs.
    for p in tree.xpath(".//p | .//li | .//div"):
        text = "".join(p.itertext()).strip()
        if not text:
            continue
        word_n = len(text.split())
        if word_n < min_words:
            parent = p.getparent()
            if parent is not None:
                parent.remove(p)
                continue
        anchor_text = sum(
            len("".join(a.itertext()).strip()) for a in p.xpath(".//a")
        )
        if len(text) > 0 and anchor_text / max(len(text), 1) > 0.8 and word_n < 30:
            parent = p.getparent()
            if parent is not None:
                parent.remove(p)

    return lxml_html.tostring(tree, encoding="unicode")


# --- BM25 relevance filter ---------------------------------------------------
#
# Ported scoring logic. No rank_bm25 dep — the math is ~30 lines.

_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_STOPWORDS = frozenset(
    "a an the and or but if then else of in on at to for from by with about "
    "as is are was were be been being have has had do does did not no so "
    "this that these those it its they them their".split()
)


def _tokenize(s: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(s) if t.lower() not in _STOPWORDS]


@dataclass
class _Passage:
    text: str
    score: float


def bm25_filter(
    html_or_markdown: str,
    query: str,
    *,
    top_k: int = 20,
    min_words: int = 6,
    min_score: float = 0.0,
) -> str:
    """Return the top-k passages (paragraphs) from the text, BM25-ranked by query.

    Input can be raw HTML (will be converted via `to_markdown`) or already
    plain text / markdown. Output is a newline-joined block of the most
    relevant passages in document order. Empty string if nothing scores
    above `min_score`.
    """
    text = html_or_markdown
    if "<" in text and ">" in text:
        text = to_markdown(text)

    # Split into passages on paragraph boundaries.
    passages = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    passages = [p for p in passages if len(p.split()) >= min_words]
    if not passages:
        return ""

    q_tokens = _tokenize(query)
    if not q_tokens:
        # No query signal — just return prunings in document order.
        return "\n\n".join(passages[:top_k])

    tokenized = [_tokenize(p) for p in passages]
    lengths = [max(len(t), 1) for t in tokenized]
    avgdl = sum(lengths) / len(lengths)
    N = len(tokenized)

    # Document frequency for each query term.
    df: dict[str, int] = {}
    for q in set(q_tokens):
        df[q] = sum(1 for toks in tokenized if q in toks)

    # IDF.
    idf = {q: math.log((N - df[q] + 0.5) / (df[q] + 0.5) + 1) for q in df}

    k1, b = 1.5, 0.75
    scored: list[_Passage] = []
    for i, toks in enumerate(tokenized):
        counts = Counter(toks)
        score = 0.0
        for q in q_tokens:
            if q not in counts:
                continue
            f = counts[q]
            score += idf[q] * (f * (k1 + 1)) / (f + k1 * (1 - b + b * lengths[i] / avgdl))
        if score > min_score:
            scored.append(_Passage(passages[i], score))

    if not scored:
        return ""
    scored.sort(key=lambda p: p.score, reverse=True)
    top = scored[:top_k]
    # Preserve document order for readability.
    order = {p.text: i for i, p in enumerate(scored)}
    top.sort(key=lambda p: passages.index(p.text))
    return "\n\n".join(p.text for p in top)


# --- Structured data extraction ----------------------------------------------

def extract_json_ld(html: str) -> list[dict]:
    """Pull JSON-LD blocks out of the HTML. Returns list of parsed objects."""
    tree = _parse(html)
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


def extract_opengraph(html: str) -> dict[str, str]:
    """Pull OpenGraph `og:*` and Twitter Card `twitter:*` meta into a dict."""
    tree = _parse(html)
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


def extract_meta_title_description(html: str) -> dict[str, str]:
    """Return {title, description, canonical} from the page head."""
    tree = _parse(html)
    if tree is None:
        return {}
    out: dict[str, str] = {}
    titles = tree.xpath(".//title")
    if titles:
        t = _clean_text(titles[0].text)
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


# --- CSS-based structured extraction -----------------------------------------

def extract_by_css(html: str, schema: dict[str, str]) -> dict[str, list[str]]:
    """Given a `{field: css_selector}` schema, return `{field: [matched text, ...]}`.

    Thin port of crawl4ai's JsonCssExtractionStrategy for the agent's
    common case: "pull out the prices / titles / descriptions as a list."
    """
    tree = _parse(html)
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
            text = _clean_text("".join(el.itertext()))
            if text:
                vals.append(text)
        out[field] = vals
    return out
