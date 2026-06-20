"""Image extraction with relevance scoring.

Port of crawl4ai's LXMLWebScrapingStrategy.process_image + parse_srcset
(content_scraping_strategy.py:42-59, 410-515). Scores each <img> on
dimensions, alt text, page position, format, and responsive variants;
drops icons/buttons/logos (score <= threshold); expands srcset / <picture>
/ data-* variants and skips base64 data: URIs. Relative src is resolved
against base_url (a small addition over crawl4ai, so the agent gets
clickable URLs).
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urljoin

_IMAGE_FORMATS = {"jpg", "jpeg", "png", "webp", "avif", "gif"}
_IMAGE_SCORE_THRESHOLD = 2          # crawl4ai default — drop score <= 2 (icons)
_DESC_MIN_WORDS = 1


def parse_srcset(s: str) -> list[dict]:
    """Verbatim port of crawl4ai.parse_srcset."""
    if not s:
        return []
    variants = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split()
        if len(bits) >= 1:
            url = bits[0]
            width = (
                bits[1].rstrip("w").split(".")[0]
                if len(bits) > 1 and bits[1].endswith("w")
                else None
            )
            variants.append({"url": url, "width": width})
    return variants


def _closest_useful_text(element) -> Optional[str]:
    current = element
    while current is not None:
        try:
            if current.text and len(current.text_content().split()) >= _DESC_MIN_WORDS:
                return current.text_content().strip()
        except Exception:
            pass
        current = current.getparent()
    return None


def _process_image(img, base_url: str, index: int, total: int) -> Optional[list[dict]]:
    style = img.get("style", "") or ""
    alt = img.get("alt", "") or ""
    src = img.get("src", "") or ""
    data_src = img.get("data-src", "") or ""
    srcset = img.get("srcset", "") or ""
    data_srcset = img.get("data-srcset", "") or ""

    if "display:none" in style.replace(" ", ""):
        return None

    parent = img.getparent()
    if parent is not None and parent.tag in ("button", "input"):
        return None
    parent_classes = (parent.get("class", "") if parent is not None else "").split()
    if any("button" in c or "icon" in c or "logo" in c for c in parent_classes):
        return None
    if (src and any(c in src for c in ("button", "icon", "logo"))) or (
        alt and any(c in alt for c in ("button", "icon", "logo"))
    ):
        return None

    # Score.
    score = 0
    width = img.get("width")
    if width and width.isdigit():
        score += 1 if int(width) > 150 else 0
    height = img.get("height")
    if height and height.isdigit():
        score += 1 if int(height) > 150 else 0
    if alt:
        score += 1
    if total > 0 and index / total < 0.5:
        score += 1

    detected_format = None
    for candidate in (src, data_src, srcset, data_srcset):
        if candidate:
            matches = [fmt for fmt in _IMAGE_FORMATS if fmt in candidate.lower()]
            if matches:
                detected_format = matches[0]
                score += 1
                break

    if srcset or data_srcset:
        score += 1
    picture = img.xpath("./ancestor::picture[1]")
    if picture:
        score += 1

    if score <= _IMAGE_SCORE_THRESHOLD:
        return None

    unique_urls: set[str] = set()
    variants: list[dict] = []
    base_info = {
        "alt": alt,
        "desc": _closest_useful_text(img),
        "score": score,
        "type": "image",
        "group_id": index,
        "format": detected_format,
    }

    def add_variant(raw_src: str, vwidth: Optional[str] = None):
        if not raw_src or raw_src.startswith("data:"):
            return
        # urljoin (not fast_urljoin) — base_url is a full page URL with a path,
        # so absolute-path srcs like "/hero.jpg" must resolve against the origin.
        resolved = urljoin(base_url, raw_src) if base_url else raw_src
        if resolved in unique_urls:
            return
        unique_urls.add(resolved)
        variant = {**base_info, "src": resolved}
        if vwidth:
            variant["width"] = vwidth
        variants.append(variant)

    add_variant(src)
    add_variant(data_src)
    for attr in (srcset, data_srcset):
        if attr:
            for source in parse_srcset(attr):
                add_variant(source["url"], source["width"])
    if picture:
        for source in picture[0].xpath(".//source[@srcset]"):
            ss = source.get("srcset")
            if ss:
                for data in parse_srcset(ss):
                    add_variant(data["url"], data["width"])
    for attr, value in img.attrib.items():
        if attr.startswith("data-") and ("src" in attr or "srcset" in attr) and "http" in value:
            add_variant(value)

    return variants or None


def score_images(tree, base_url: str | None = None) -> list[dict]:
    """Walk all <img> in the tree and return scored, deduped image variants."""
    if tree is None:
        return []
    imgs = tree.xpath(".//img")
    total = len(imgs)
    out: list[dict] = []
    for i, img in enumerate(imgs):
        variants = _process_image(img, base_url or "", i, total)
        if variants:
            out.extend(variants)
    out.sort(key=lambda v: v.get("score", 0), reverse=True)
    return out
