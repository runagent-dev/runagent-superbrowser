"""Tool registration for browser session workers.

`register_session_tools` is the public entry point — orchestrator and tests
call it. Order matches the original monolith so any registration-order side
effects (last-registered-wins / shadowing) are preserved verbatim.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .state import BrowserSessionState

if TYPE_CHECKING:
    from superbrowser_bridge.memory import Memory
from .tools import (
    BrowserAskUserTool,
    BrowserCaptchaScreenshotTool,
    BrowserChessMoveTool,
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
    BrowserListElementsTool,
    BrowserListSliderHandlesTool,
    BrowserNavigateTool,
    BrowserOpenTool,
    BrowserPlanNextStepsTool,
    BrowserRequestHelpTool,
    BrowserRewindToCheckpointTool,
    BrowserRunScriptTool,
    BrowserUndoLastClickTool,
    BrowserScreenshotTool,
    BrowserScrollToBboxTool,
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


def register_session_tools(
    bot: "Nanobot",
    state: BrowserSessionState | None = None,
    *,
    memory: "Memory | None" = None,
) -> BrowserSessionState:
    """Register all browser session tools with a nanobot instance.

    Args:
        bot: The Nanobot instance to register tools on.
        state: Optional shared state. If None, creates a new one
            bound to ``memory`` (or a synthesized default Memory).
        memory: Optional Memory instance to bind. Only honored when
            ``state`` is None. delegation.py constructs the worker's
            Memory before BrowserSessionState so the on-disk task
            directory and ledger are stable from the first tool call.

    Returns:
        The BrowserSessionState used (for external access if needed).
    """
    if state is None:
        state = BrowserSessionState(memory=memory)

    tools = [
        BrowserOpenTool(state),
        BrowserNavigateTool(state),
        BrowserScreenshotTool(state),
        BrowserClickTool(state),
        BrowserClickAtTool(state),
        BrowserClickSelectorTool(state),   # CSS-selector click; supports in_iframe
        BrowserTypeAtTool(state),
        BrowserFixTextAtTool(state),
        BrowserTypeTool(state),
        BrowserKeysTool(state),
        BrowserScrollTool(state),
        BrowserScrollToBboxTool(state),    # scroll labelled V_n into view
        BrowserScrollUntilTool(state),     # DEPRECATED: text-based scan (kept for legacy callers)
        BrowserScrollWithinTool(state),    # in-popup/listbox scroll (requires container_selector)
        BrowserSelectTool(state),
        BrowserSelectOptionTool(state),
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
        BrowserChessMoveTool(state),       # pixel-exact chess move (board-rect 8x8 subdivide)
        BrowserListElementsTool(state),    # on-demand element inspection
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
