"""Shared imports and re-exports for the tool submodules.

Every tool file does ``from ._common import *`` so the tool classes
keep their original (un-namespaced) helper references intact, exactly
as they appeared in the legacy session_tools.py monolith. This avoids
having to rewrite the body of each Tool class.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from datetime import datetime
from typing import Any

import httpx

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    ArraySchema,
    BooleanSchema,
    IntegerSchema,
    NumberSchema,
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)

from ..captcha_solver import _first_actionable, _solve_captcha_iterative
from ..constants import (
    BLOCKED_BROWSER_OPEN_HARD_STOP,
    RESUMPTION_PATH,
    RESUMPTION_TTL_SEC,
    SCREENSHOT_DIR,
    SUPERBROWSER_URL,
    _ATOMIC_FIX_TEXT_JS,
    _CAPTCHA_KEYWORDS,
    _CONTENT_HASH_LEN,
    _CURSOR_TOOL_NAMES,
    _HARD_DOMAINS,
    _SUBMIT_KEYWORDS,
)
from ..crosscheck import _dom_vision_crosscheck, _vision_dom_crosscheck
from ..dom_helpers import (
    _build_network_block_message,
    _fetch_elements,
    _fetch_elements_with_bounds,
    _format_state,
    _rect_iou,
    _resolve_v_by_label,
)
from ..effect import (
    _classify_effect,
    _diff_text,
    _maybe_no_effect_prefix,
    _maybe_script_usage_warning,
)
from ..exceptions import WorkerMustExitError
from ..http_client import _feedback_gate, _request_with_backoff
from ..resumption import (
    clear_resumption_artifact,
    load_resumption_artifact,
    save_resumption_artifact,
)
from ..state import BrowserSessionState
from ..telemetry import _compute_screenshot_budget, _update_scroll_telemetry
from ..vision_sync import (
    _append_fresh_vision,
    _await_vision_prefetch,
    _await_vision_required,
    _force_fresh_vision,
    _is_captcha_intent,
    _last_vision_has_captcha_flag,
    _push_vision_bboxes,
    _push_vision_pending,
    _read_image_dims,
    _schedule_vision_prefetch,
    _vision_alternatives_hint,
)

__all__ = [
    "ArraySchema",
    "BLOCKED_BROWSER_OPEN_HARD_STOP",
    "BooleanSchema",
    "BrowserSessionState",
    "IntegerSchema",
    "NumberSchema",
    "ObjectSchema",
    "RESUMPTION_PATH",
    "RESUMPTION_TTL_SEC",
    "SCREENSHOT_DIR",
    "SUPERBROWSER_URL",
    "StringSchema",
    "Tool",
    "WorkerMustExitError",
    "_ATOMIC_FIX_TEXT_JS",
    "_CAPTCHA_KEYWORDS",
    "_CONTENT_HASH_LEN",
    "_CURSOR_TOOL_NAMES",
    "_HARD_DOMAINS",
    "_SUBMIT_KEYWORDS",
    "_append_fresh_vision",
    "_await_vision_prefetch",
    "_await_vision_required",
    "_build_network_block_message",
    "_classify_effect",
    "_compute_screenshot_budget",
    "_diff_text",
    "_dom_vision_crosscheck",
    "_feedback_gate",
    "_fetch_elements",
    "_fetch_elements_with_bounds",
    "_first_actionable",
    "_force_fresh_vision",
    "_format_state",
    "_is_captcha_intent",
    "_last_vision_has_captcha_flag",
    "_maybe_no_effect_prefix",
    "_maybe_script_usage_warning",
    "_push_vision_bboxes",
    "_push_vision_pending",
    "_read_image_dims",
    "_rect_iou",
    "_request_with_backoff",
    "_resolve_v_by_label",
    "_schedule_vision_prefetch",
    "_solve_captcha_iterative",
    "_update_scroll_telemetry",
    "_vision_alternatives_hint",
    "_vision_dom_crosscheck",
    "Any",
    "asyncio",
    "base64",
    "clear_resumption_artifact",
    "datetime",
    "httpx",
    "json",
    "load_resumption_artifact",
    "os",
    "save_resumption_artifact",
    "time",
    "tool_parameters",
    "tool_parameters_schema",
]
