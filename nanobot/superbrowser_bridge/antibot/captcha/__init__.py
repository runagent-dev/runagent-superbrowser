"""antibot.captcha — Tier 3 captcha detection and solving.

Ports the essential strategies from `src/browser/captcha/` into Python so
t3 (patchright) sessions have working captcha tooling without round-tripping
to the TS server.
"""

from .detect import CaptchaInfo, detect
from .solve_token import solve_token
from .solve_vision import solve_vision
from .solve_slider import solve_slider
from .widget_screenshot import widget_screenshot

__all__ = [
    "CaptchaInfo",
    "detect",
    "solve_token",
    "solve_vision",
    "solve_slider",
    "widget_screenshot",
]
