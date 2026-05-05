"""Tool-registration entry point. Mirrors the original
``register_session_tools`` in the legacy session_tools.py byte-for-byte
to preserve the registration order the bot's dispatcher depends on.
"""

from __future__ import annotations

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

    tools = [
        BrowserOpenTool(state),
        BrowserNavigateTool(state),
        BrowserScreenshotTool(state),
        BrowserClickTool(state),
        BrowserClickAtTool(state),
        BrowserTypeAtTool(state),
        BrowserFixTextAtTool(state),
        BrowserTypeTool(state),
        BrowserKeysTool(state),
        BrowserScrollTool(state),
        BrowserScrollUntilTool(state),     # kept: scroll-until-target helper
        BrowserSelectTool(state),
        BrowserSelectOptionTool(state),
        BrowserFormPlanTool(state),
        BrowserEvalTool(state),
        BrowserRunScriptTool(state),
        BrowserWaitForTool(state),
        BrowserDragTool(state),
        BrowserGetRectTool(state),         # kept: DOM rect helper
        BrowserClickSelectorTool(state),   # kept: DOM-selector fast path
        BrowserDragSelectorsTool(state),   # kept: selector-based drag
        BrowserDragPathTool(state),        # kept: polyline drag
        BrowserSetSliderTool(state),       # kept: slider family for ChaseIRA calc
        BrowserSetSliderAtTool(state),
        BrowserListSliderHandlesTool(state),
        BrowserDragSliderUntilTool(state),
        BrowserImageRegionTool(state),     # kept: image region helper
        BrowserSolvePuzzleTool(state),     # kept: puzzle solver
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
    for tool in tools:
        bot._loop.tools.register(tool)
    return state
