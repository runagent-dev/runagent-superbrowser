"""Query-aware BM25 content filter with priority-tag weighting.

lxml port of crawl4ai's BM25ContentFilter (content_filter_strategy.py:
381-539). Splits the body into ordered text chunks tagged by their block
element, scores each chunk against the query with BM25 (Okapi, k1=1.5,
b=0.75 — the same inline math the prior content.py used), multiplies by a
per-tag priority weight (h1=5, h2=4, ...), keeps chunks above a threshold,
and returns them in document order.

No rank_bm25 / snowballstemmer dependency — the BM25 math is ~20 lines and
we use a tiny, conservative plural stemmer instead.
"""

from __future__ import annotations

import math
import re
from collections import Counter

from . import _html

_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)

_STOPWORDS = frozenset(
    "a an the and or but if then else of in on at to for from by with about "
    "as is are was were be been being have has had do does did not no so "
    "this that these those it its they them their".split()
)

# Block tags get a chunk; these inline tags fold into the parent block's text.
_INLINE_TAGS = frozenset({
    "a", "abbr", "acronym", "b", "bdo", "big", "br", "button", "cite", "code",
    "dfn", "em", "i", "img", "input", "kbd", "label", "map", "object", "q",
    "samp", "script", "select", "small", "span", "strong", "sub", "sup",
    "textarea", "time", "tt", "var",
})

_HEADER_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6", "header"})

# crawl4ai's priority_tags — weight a chunk by the semantic importance of its tag.
_PRIORITY_TAGS = {
    "h1": 5.0, "h2": 4.0, "h3": 3.0, "title": 4.0, "strong": 2.0, "b": 1.5,
    "em": 1.5, "blockquote": 2.0, "code": 2.0, "pre": 1.5, "th": 1.5,
}


def _stem(word: str) -> str:
    """Conservative plural stemmer — strips a trailing regular -s only.

    Deliberately minimal (no -ing/-ed/-ation rules) to avoid collapsing
    unrelated terms. Handles the dominant query/content mismatch:
    prices/price, hotels/hotel, flights/flight.
    """
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _tokenize(s: str, use_stemming: bool) -> list[str]:
    out = (t.lower() for t in _TOKEN_RE.findall(s))
    if use_stemming:
        out = (_stem(t) for t in out)
    return [t for t in out if t and t not in _STOPWORDS]


def extract_page_query(tree, body) -> str:
    """Metadata fallback query: title + h1 + meta keywords/description, else the
    first substantial paragraph. Port of crawl4ai's extract_page_query."""
    parts: list[str] = []
    titles = tree.xpath(".//title")
    if titles and titles[0].text:
        parts.append(titles[0].text)
    h1s = (body if body is not None else tree).xpath(".//h1")
    if h1s:
        parts.append("".join(h1s[0].itertext()))
    temp = ""
    for name in ("keywords", "description"):
        for m in tree.xpath(f'.//meta[@name="{name}"]'):
            c = m.get("content")
            if c:
                parts.append(c)
                temp += c
            break
    if not temp and body is not None:
        for p in body.xpath(".//p"):
            txt = "".join(p.itertext())
            if len(txt) > 150:
                parts.append(txt[:150])
                break
    return " ".join(s for s in parts if s).strip()


def extract_text_chunks(body, min_word_threshold: int | None = None) -> list[tuple]:
    """Return ordered ``(index, text, tag_type, tag_name)`` chunks.

    Each non-inline (block) element yields one chunk whose text is its own
    direct text plus the text of its inline descendants (nested block elements
    become their own chunks). Preserves document order via pre-order DFS.
    """
    chunks: list[tuple] = []
    counter = [0]

    def block_own_text(el) -> str:
        parts: list[str] = []
        if el.text and el.text.strip():
            parts.append(el.text.strip())
        for child in el:
            tag = child.tag
            if isinstance(tag, str) and tag in _INLINE_TAGS:
                t = "".join(child.itertext())
                if t.strip():
                    parts.append(t.strip())
            if child.tail and child.tail.strip():
                parts.append(child.tail.strip())
        return " ".join(" ".join(parts).split())

    def walk(el):
        tag = el.tag
        if not isinstance(tag, str):
            return
        if tag not in _INLINE_TAGS:
            text = block_own_text(el)
            if text:
                tag_type = "header" if tag in _HEADER_TAGS else "content"
                chunks.append((counter[0], text, tag_type, tag))
                counter[0] += 1
        for child in el:
            if isinstance(child.tag, str) and child.tag not in _INLINE_TAGS:
                walk(child)

    if body is not None:
        walk(body)

    if min_word_threshold:
        chunks = [c for c in chunks if len(c[1].split()) >= min_word_threshold]
    return chunks


def filter(
    html_or_tree,
    query: str | None,
    *,
    base_url: str | None = None,
    bm25_threshold: float = 1.0,
    min_words: int = 2,
    top_k: int | None = None,
    use_stemming: bool = True,
) -> str:
    """Return the query-relevant chunks (tag-weighted BM25), newline-joined in
    document order. Falls back to page-metadata query when ``query`` is None."""
    tree = _html.as_tree(html_or_tree)
    if tree is None:
        return ""
    body = _html.find_body(tree)
    q = query or extract_page_query(tree, body)
    if not q or not q.strip():
        return ""

    candidates = extract_text_chunks(body, min_words)
    if not candidates:
        return ""

    corpus = [_tokenize(c[1], use_stemming) for c in candidates]
    q_tokens = _tokenize(q, use_stemming)
    if not q_tokens:
        return ""

    lengths = [max(len(t), 1) for t in corpus]
    avgdl = sum(lengths) / len(lengths)
    n_docs = len(corpus)

    df: dict[str, int] = {}
    for term in set(q_tokens):
        df[term] = sum(1 for toks in corpus if term in toks)
    idf = {term: math.log((n_docs - df[term] + 0.5) / (df[term] + 0.5) + 1) for term in df}

    k1, b = 1.5, 0.75
    scored: list[tuple[int, str, float]] = []  # (doc_index, text, adjusted_score)
    for i, toks in enumerate(corpus):
        counts = Counter(toks)
        score = 0.0
        for term in q_tokens:
            f = counts.get(term, 0)
            if not f:
                continue
            score += idf[term] * (f * (k1 + 1)) / (f + k1 * (1 - b + b * lengths[i] / avgdl))
        if score <= 0:
            continue  # no query term matched — never include
        adjusted = score * _PRIORITY_TAGS.get(candidates[i][3], 1.0)
        if adjusted >= bm25_threshold:
            scored.append((candidates[i][0], candidates[i][1], adjusted))

    if not scored:
        return ""

    # When top_k is set, keep the highest-scoring chunks; then render in
    # document order for readability.
    if top_k is not None and len(scored) > top_k:
        scored = sorted(scored, key=lambda x: x[2], reverse=True)[:top_k]
    scored.sort(key=lambda x: x[0])  # document order

    seen: set[str] = set()
    out: list[str] = []
    for _idx, text, _score in scored:
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
    return "\n\n".join(out)
