"""HTML -> clean markdown with numbered citations.

Ported from crawl4ai's DefaultMarkdownGenerator
(markdown_generation_strategy.py): html2text under the hood with
LLM-friendly options, then a citation pass that turns inline links into
``text⟨n⟩`` references plus a trailing ``## References`` block (URLs
deduped). We use the upstream PyPI ``html2text`` package directly rather
than crawl4ai's CustomHTML2Text subclass.
"""

from __future__ import annotations

import re
from typing import Tuple
from urllib.parse import urljoin

import html2text

# Verbatim from crawl4ai markdown_generation_strategy.py:11 — matches
# [text](url "title") and the image variant ![alt](url), tolerating nested
# brackets/parens.
LINK_PATTERN = re.compile(
    r'!?\[((?:[^\[\]]|\[(?:[^\[\]]|\[[^\]]*\])*\])*)\]'
    r'\(((?:[^()\s]|\([^()]*\))*)(?:\s+"([^"]*)")?\)'
)


def fast_urljoin(base: str, url: str) -> str:
    """Fast URL joining for common cases (port of crawl4ai's helper)."""
    if url.startswith(("http://", "https://", "mailto:", "//")):
        return url
    if url.startswith("/"):
        if base.endswith("/"):
            return base[:-1] + url
        return base + url
    return urljoin(base, url)


def _new_converter(base_url: str = "") -> html2text.HTML2Text:
    h = html2text.HTML2Text(baseurl=base_url or "")
    # LLM-friendly defaults (mirror crawl4ai's generate_markdown options).
    h.body_width = 0           # never wrap — wrapping breaks the citation regex
    h.single_line_break = True
    h.mark_code = True
    h.ignore_links = False
    h.ignore_images = False
    h.ignore_emphasis = False
    h.protect_links = False
    h.escape_snob = False
    for attr in ("ignore_mailto_links", "skip_internal_links"):
        try:
            setattr(h, attr, True)
        except Exception:
            pass
    return h


def _to_raw_markdown(html: str, base_url: str = "") -> str:
    if not html:
        return ""
    h = _new_converter(base_url)
    try:
        raw = h.handle(html)
    except Exception:
        return ""
    # mark_code indents fenced blocks by 4 spaces — undo it (crawl4ai's fix).
    return raw.replace("    ```", "```")


def convert_links_to_citations(markdown: str, base_url: str = "") -> Tuple[str, str]:
    """Turn inline ``[text](url)`` links into ``text⟨n⟩`` + a references block.

    Ported from crawl4ai. Deduplicates by resolved URL. ``data:``/``mailto:``/
    ``javascript:`` links are left as plain text (never cited) so base64 blobs
    and mail links don't pollute the references.
    """
    link_map: dict[str, tuple[int, str]] = {}
    url_cache: dict[str, str] = {}
    parts: list[str] = []
    last_end = 0
    counter = 1

    for match in LINK_PATTERN.finditer(markdown):
        parts.append(markdown[last_end:match.start()])
        text, url, title = match.groups()
        text = text or ""
        url = url or ""
        is_img = match.group(0).startswith("!")

        # Skip non-navigational schemes — strip the link, keep the text.
        if url.startswith(("data:", "mailto:", "javascript:", "tel:")):
            parts.append(f"![{text}]" if is_img else text)
            last_end = match.end()
            continue

        if base_url and not url.startswith(("http://", "https://", "mailto:")):
            if url not in url_cache:
                url_cache[url] = fast_urljoin(base_url, url)
            url = url_cache[url]

        if url not in link_map:
            desc = []
            if title:
                desc.append(title)
            if text and text != title:
                desc.append(text)
            link_map[url] = (counter, ": " + " - ".join(desc) if desc else "")
            counter += 1

        num = link_map[url][0]
        parts.append(f"![{text}⟨{num}⟩]" if is_img else f"{text}⟨{num}⟩")
        last_end = match.end()

    parts.append(markdown[last_end:])
    converted_text = "".join(parts)

    if not link_map:
        return converted_text, ""

    references = ["\n\n## References\n\n"]
    references.extend(
        f"⟨{num}⟩ {url}{desc}\n"
        for url, (num, desc) in sorted(link_map.items(), key=lambda x: x[1][0])
    )
    return converted_text, "".join(references)


def html_to_markdown(
    html: str, *, base_url: str = "", citations: bool = True
) -> Tuple[str, str]:
    """Convert HTML to markdown. Returns ``(markdown, references)``.

    With ``citations=True`` links become numbered ``⟨n⟩`` references and the
    second tuple element is the ``## References`` block. With ``citations=False``
    links stay inline ``[text](url)`` and references is ``""``.
    """
    raw = _to_raw_markdown(html, base_url)
    if not citations:
        return raw, ""
    try:
        return convert_links_to_citations(raw, base_url)
    except Exception:
        return raw, ""
