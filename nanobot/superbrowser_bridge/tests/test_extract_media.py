"""Image scoring + srcset tests for antibot.extract.media.

    source venv/bin/activate && \
        python -m pytest nanobot/superbrowser_bridge/tests/test_extract_media.py
"""

from __future__ import annotations

from superbrowser_bridge.antibot.extract import _html
from superbrowser_bridge.antibot.extract.media import parse_srcset, score_images

_HTML = """
<html><body><article>
<p>An article with a hero image and an icon and a tracking pixel embedded here.</p>
<img src="/hero.jpg" alt="a big hero banner photo" width="800" height="600"
     srcset="/hero-400.jpg 400w, /hero-800.jpg 800w">
<img src="/icon.png" alt="icon" width="16" height="16">
<img src="data:image/png;base64,AAAA" alt="pixel" width="900" height="700">
</article></body></html>
"""


def test_hero_kept_icon_dropped():
    media = score_images(_html.parse(_HTML), base_url="https://x.com/post")
    srcs = [m["src"] for m in media]
    assert any("hero" in s for s in srcs)
    assert not any("icon.png" in s for s in srcs)


def test_srcset_variants_parsed_and_resolved():
    media = score_images(_html.parse(_HTML), base_url="https://x.com/post")
    srcs = [m["src"] for m in media]
    # Absolute-path src resolves against the origin, not the page path.
    assert "https://x.com/hero.jpg" in srcs
    assert "https://x.com/hero-400.jpg" in srcs
    assert "https://x.com/hero-800.jpg" in srcs


def test_data_uri_excluded():
    media = score_images(_html.parse(_HTML), base_url="https://x.com/post")
    assert not any(s.startswith("data:") for s in (m["src"] for m in media))


def test_parse_srcset():
    out = parse_srcset("/a-200.jpg 200w, /a-400.jpg 400w")
    assert out == [{"url": "/a-200.jpg", "width": "200"}, {"url": "/a-400.jpg", "width": "400"}]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
