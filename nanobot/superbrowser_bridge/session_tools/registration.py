"""Tool-registration entry point. Mirrors the original
``register_session_tools`` in the legacy session_tools.py byte-for-byte
to preserve the registration order the bot's dispatcher depends on.

Phase 5 — feature-level curation: rarely-used variants are gated
behind ``SUPERBROWSER_FEATURE_LEVEL=full``. The default ``standard``
level hides puzzle/slider/drag/image-region tools from the brain's
choice space (~25% reduction) without removing the code. Workflows
that depend on these tools (Chase IRA calculator, captcha drag) set
``SUPERBROWSER_FEATURE_LEVEL=full`` in their environment.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from nanobot.agent.tools.base import tool_parameters
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema

from .state import BrowserSessionState
from .tools import (
    BrowserAskUserTool,
    BrowserBriefMarkTool,
    BrowserCaptchaScreenshotTool,
    BrowserClickAtTool,
    BrowserClickSelectorTool,
    BrowserClickTool,
    BrowserCloseTool,
    BrowserDetectCaptchaTool,
    BrowserDialogTool,
    BrowserDragPathTool,
    BrowserDragSelectorsTool,
    BrowserDragSliderUntilTool,
    BrowserDragTool,
    BrowserEscalateTool,
    BrowserEvalTool,
    BrowserFindTargetTool,
    BrowserFixTextAtTool,
    BrowserFormBeginTool,
    BrowserFormCommitTool,
    BrowserFormPlanTool,
    BrowserFormStatusTool,
    BrowserGetMarkdownTool,
    BrowserGetRectTool,
    BrowserImageRegionTool,
    BrowserKeysTool,
    BrowserListSliderHandlesTool,
    BrowserNavigateTool,
    BrowserOpenTool,
    BrowserPlanNextStepsTool,
    BrowserRequestHelpTool,
    BrowserRewindToCheckpointTool,
    BrowserRunScriptTool,
    BrowserScreenshotTool,
    BrowserScrollTool,
    BrowserScrollUntilTool,
    BrowserSelectOptionTool,
    BrowserSelectTool,
    BrowserSetSliderAtTool,
    BrowserSetSliderTool,
    BrowserSolveCaptchaTool,
    BrowserSolvePuzzleTool,
    BrowserTypeAtTool,
    BrowserTypeTool,
    BrowserVerifyFactTool,
    BrowserWaitForTool,
)

if TYPE_CHECKING:
    from nanobot import Nanobot  # noqa: F401


# This decorator is ported verbatim from the legacy session_tools.py
# (lines 10349-10352). It is applied to ``register_session_tools`` even
# though the function is not a Tool method — keeping it preserves any
# attribute side effects ``tool_parameters`` records on the function
# object, which downstream introspection in nanobot may rely on.
@tool_parameters(
    tool_parameters_schema(session_id=StringSchema("Session ID"), required=["session_id"])
)

def register_session_tools(bot: "Nanobot", state: BrowserSessionState | None = None) -> BrowserSessionState:
    """Register all browser session tools with a nanobot instance.

    Args:
        bot: The Nanobot instance to register tools on.
        state: Optional shared state. If None, creates a new one.

    Returns:
        The BrowserSessionState used (for external access if needed).
    """
    if state is None:
        state = BrowserSessionState()

    feature_level = (
        os.environ.get("SUPERBROWSER_FEATURE_LEVEL", "standard")
        .strip()
        .lower()
    )
    full = feature_level == "full"

    tools: list = [
        BrowserOpenTool(state),
        BrowserNavigateTool(state),
        BrowserScreenshotTool(state),
        # Click-pathway hierarchy: selector first (zero vision cost),
        # then bbox (vision + snap), then DOM-index (Phase H — for
        # compound rows where vision misses sub-elements).
        BrowserClickSelectorTool(state),
        BrowserClickAtTool(state),
        BrowserClickTool(state),
        BrowserTypeAtTool(state),
        BrowserFixTextAtTool(state),
        BrowserTypeTool(state),
        BrowserKeysTool(state),
        BrowserScrollTool(state),
        BrowserScrollUntilTool(state),     # kept: scroll-until-target helper
        # Phase B1: target finder bridges markdown ∪ vision ∪ scroll —
        # use when label exists in markdown but no V_n labels it.
        BrowserFindTargetTool(state),
        BrowserSelectTool(state),
        BrowserSelectOptionTool(state),
        BrowserFormPlanTool(state),
        BrowserEvalTool(state),
        BrowserRunScriptTool(state),
        BrowserWaitForTool(state),
        BrowserGetRectTool(state),         # kept: DOM rect helper
        BrowserGetMarkdownTool(state),     # stateful: caches body for task_brief reconciliation
        BrowserDialogTool(),               # stateless
        BrowserDetectCaptchaTool(state),
        BrowserCaptchaScreenshotTool(state),
        BrowserSolveCaptchaTool(state),
        BrowserAskUserTool(state),
        BrowserVerifyFactTool(state),
        BrowserRequestHelpTool(state),
        BrowserEscalateTool(state),        # t1 → t3 migration
        BrowserPlanNextStepsTool(state),   # hierarchical planner
        BrowserFormBeginTool(state),       # Phase 2.1: form-fill orchestration
        BrowserFormStatusTool(state),
        BrowserFormCommitTool(state),
        BrowserRewindToCheckpointTool(state),  # kept: session-memory escape hatch
        BrowserBriefMarkTool(state),       # multi-condition checklist marker
        BrowserCloseTool(state),
    ]

    # Phase 5: feature-flagged variants. The brain almost never picks
    # these — they confuse tool selection more than they help. Workflows
    # that genuinely need them (Chase IRA slider calc, drag-puzzle
    # captchas) opt in via SUPERBROWSER_FEATURE_LEVEL=full.
    if full:
        # Insert drag/slider/puzzle variants in the canonical slot
        # (between WaitForTool and GetRectTool) so registration-order
        # parity with the legacy tool dispatcher is preserved.
        wait_idx = next(
            (i for i, t in enumerate(tools) if t.__class__.__name__ == "BrowserWaitForTool"),
            len(tools),
        )
        tools[wait_idx + 1 : wait_idx + 1] = [
            BrowserDragTool(state),
            BrowserDragSelectorsTool(state),   # selector-based drag
            BrowserDragPathTool(state),        # polyline drag
            BrowserSetSliderTool(state),       # slider family for ChaseIRA calc
            BrowserSetSliderAtTool(state),
            BrowserListSliderHandlesTool(state),
            BrowserDragSliderUntilTool(state),
            BrowserImageRegionTool(state),     # image region helper
            BrowserSolvePuzzleTool(state),     # puzzle solver
        ]

    for tool in tools:
        bot._loop.tools.register(tool)
    return state
