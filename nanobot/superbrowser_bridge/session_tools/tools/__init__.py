"""Tool classes for the session-tools package.

Each cluster lives in its own submodule. ``TOOL_CLASSES`` lists every
Tool class in the exact registration order used by
``register_session_tools`` in the legacy session_tools.py — keep this
order stable, the bot's tool dispatcher is sensitive to it.
"""

from __future__ import annotations

from .ask import BrowserAskUserTool, BrowserRequestHelpTool
from .captcha import BrowserDetectCaptchaTool, BrowserSolveCaptchaTool
from .cursor import (
    BrowserClickAtTool,
    BrowserClickSelectorTool,
    BrowserClickTool,
    BrowserDragTool,
    BrowserFixTextAtTool,
    BrowserKeysTool,
    BrowserTypeAtTool,
    BrowserTypeTool,
)
from .dom_extract import (
    BrowserBriefMarkTool,
    BrowserGetMarkdownTool,
    BrowserGetRectTool,
    BrowserPlanNextStepsTool,
    BrowserVerifyFactTool,
)
from .eval import BrowserEvalTool, BrowserRunScriptTool
from .find import BrowserFindTargetTool
from .forms import (
    BrowserDialogTool,
    BrowserFormBeginTool,
    BrowserFormCommitTool,
    BrowserFormPlanTool,
    BrowserFormStatusTool,
    BrowserSelectOptionTool,
    BrowserSelectTool,
)
from .lifecycle import (
    BrowserCloseTool,
    BrowserEscalateTool,
    BrowserOpenTool,
    BrowserRewindToCheckpointTool,
)
from .navigation import (
    BrowserNavigateTool,
    BrowserScrollTool,
    BrowserScrollUntilTool,
    BrowserWaitForTool,
)
from .puzzle import BrowserSolvePuzzleTool
from .screenshots import (
    BrowserCaptchaScreenshotTool,
    BrowserImageRegionTool,
    BrowserScreenshotTool,
)
from .sliders import (
    BrowserDragPathTool,
    BrowserDragSelectorsTool,
    BrowserDragSliderUntilTool,
    BrowserListSliderHandlesTool,
    BrowserSetSliderAtTool,
    BrowserSetSliderTool,
)

__all__ = [
    "BrowserAskUserTool",
    "BrowserBriefMarkTool",
    "BrowserCaptchaScreenshotTool",
    "BrowserClickAtTool",
    "BrowserClickSelectorTool",
    "BrowserClickTool",
    "BrowserCloseTool",
    "BrowserDetectCaptchaTool",
    "BrowserDialogTool",
    "BrowserDragPathTool",
    "BrowserDragSelectorsTool",
    "BrowserDragSliderUntilTool",
    "BrowserDragTool",
    "BrowserEscalateTool",
    "BrowserEvalTool",
    "BrowserFindTargetTool",
    "BrowserFixTextAtTool",
    "BrowserFormBeginTool",
    "BrowserFormCommitTool",
    "BrowserFormPlanTool",
    "BrowserFormStatusTool",
    "BrowserGetMarkdownTool",
    "BrowserGetRectTool",
    "BrowserImageRegionTool",
    "BrowserKeysTool",
    "BrowserListSliderHandlesTool",
    "BrowserNavigateTool",
    "BrowserOpenTool",
    "BrowserPlanNextStepsTool",
    "BrowserRequestHelpTool",
    "BrowserRewindToCheckpointTool",
    "BrowserRunScriptTool",
    "BrowserScreenshotTool",
    "BrowserScrollTool",
    "BrowserScrollUntilTool",
    "BrowserSelectOptionTool",
    "BrowserSelectTool",
    "BrowserSetSliderAtTool",
    "BrowserSetSliderTool",
    "BrowserSolveCaptchaTool",
    "BrowserSolvePuzzleTool",
    "BrowserTypeAtTool",
    "BrowserTypeTool",
    "BrowserVerifyFactTool",
    "BrowserWaitForTool",
]
