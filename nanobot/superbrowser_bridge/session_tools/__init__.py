"""Low-level session-based browser tools for nanobot.

State is encapsulated in BrowserSessionState — not module globals.
This allows multiple Nanobot instances (e.g., orchestrator + browser worker)
to have isolated state in the same process.

This package re-exports the entire public surface of the previous
`session_tools.py` so external imports keep working unchanged. Callers
that reach into `_`-prefixed helpers (the test suite, type_verify,
antibot/captcha/solve_vision) also continue to work.
"""

from __future__ import annotations

# Module-level constants and small public symbols.
from .effects import (
    BLOCKED_BROWSER_OPEN_HARD_STOP,
    WorkerMustExitError,
    _ATOMIC_FIX_TEXT_JS,
    _CAPTCHA_KEYWORDS,
    _CONTENT_HASH_LEN,
    _CURSOR_TOOL_NAMES,
    _HARD_DOMAINS,
    _classify_effect,
    _diff_text,
    _is_captcha_intent,
    _last_vision_has_captcha_flag,
    _maybe_no_effect_prefix,
    _maybe_script_usage_warning,
)
from .feedback import _feedback_gate, _fetch_feedback_state
from .formatting import (
    _build_network_block_message,
    _fetch_elements,
    _format_state,
    _vision_alternatives_hint,
)
from .http_client import (
    SCREENSHOT_DIR,
    SUPERBROWSER_URL,
    _T3Response,
    _auth_headers,
    _is_t3_url,
    _request_with_backoff,
    _t3_dispatch_from_http,
)
from .resumption import (
    RESUMPTION_PATH,
    RESUMPTION_TTL_SEC,
    clear_resumption_artifact,
    load_resumption_artifact,
    save_resumption_artifact,
)
from .telemetry import (
    _extract_recent_failures,
    _update_scroll_telemetry,
)
from .vision_pipeline import (
    _append_fresh_vision,
    _await_vision_prefetch,
    _await_vision_required,
    _push_vision_bboxes,
    _push_vision_pending,
    _read_image_dims,
    _schedule_vision_prefetch,
)
from .captcha_solver import (
    _SUBMIT_KEYWORDS,
    _first_actionable,
    _solve_captcha_iterative,
)

# State + registration entry point.
from .state import BrowserSessionState
from .registry import register_session_tools

# All tool classes.
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
    BrowserListSliderHandlesTool,
    BrowserNavigateTool,
    BrowserOpenTool,
    BrowserPlanNextStepsTool,
    BrowserRequestHelpTool,
    BrowserRewindToCheckpointTool,
    BrowserRunScriptTool,
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
    FieldStatus,
)
