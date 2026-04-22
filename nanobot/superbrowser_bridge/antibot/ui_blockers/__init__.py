"""DOM-side detection of non-captcha UI blockers — cookie banners,
newsletter popups, generic modals. Complements vision-side scene graph
by giving the planner a second, independent signal anchored in CSS
selectors and DOM geometry (not pixels).

Public surface:
    from .detect_blockers import BlockerInfo, detect
"""
from .detect_blockers import BlockerInfo, detect

__all__ = ["BlockerInfo", "detect"]
