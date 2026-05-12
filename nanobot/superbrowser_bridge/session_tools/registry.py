"""Tool registration for browser session workers.

`register_session_tools` is the public entry point — orchestrator and tests
call it. Order matches the original monolith so any registration-order side
effects (last-registered-wins / shadowing) are preserved verbatim.
"""

from __future__ import annotations

from .state import BrowserSessionState
from .tools import (
    BrowserAskUserTool,
    BrowserCaptchaScreenshotTool,
    BrowserClickAtTool,
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
    BrowserPickDateTool,
    BrowserPickTimeTool,
    BrowserPlanNextStepsTool,
    BrowserRequestHelpTool,
    BrowserRewindToCheckpointTool,
    BrowserRunScriptTool,
    BrowserUndoLastClickTool,
    BrowserScreenshotTool,
    BrowserScrollTool,
    BrowserScrollUntilTool,
    BrowserScrollWithinTool,
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
        BrowserScrollWithinTool(state),    # in-popup/listbox scroll
        BrowserSelectTool(state),
        BrowserSelectOptionTool(state),
        BrowserPickDateTool(state),
        BrowserPickTimeTool(state),
        BrowserFormPlanTool(state),
        BrowserEvalTool(state),
        BrowserRunScriptTool(state),
        BrowserWaitForTool(state),
        BrowserDragTool(state),
        BrowserGetRectTool(state),         # kept: DOM rect helper
        BrowserDragSelectorsTool(state),   # kept: selector-based drag
        BrowserDragPathTool(state),        # kept: polyline drag
        BrowserSetSliderTool(state),       # kept: slider family for ChaseIRA calc
        BrowserSetSliderAtTool(state),
        BrowserListSliderHandlesTool(state),
        BrowserDragSliderUntilTool(state),
        BrowserImageRegionTool(state),     # kept: image region helper
        BrowserSolvePuzzleTool(state),     # kept: puzzle solver
        BrowserGetMarkdownTool(),          # stateless
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
        BrowserUndoLastClickTool(state),       # surgical in-page misclick undo
        BrowserCloseTool(state),
    ]
    for tool in tools:
        bot._loop.tools.register(tool)
    return state
