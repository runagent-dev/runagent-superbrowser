"""
Low-level session-based browser tools for nanobot.

State is encapsulated in BrowserSessionState — not module globals.
This allows multiple Nanobot instances (e.g., orchestrator + browser worker)
to have isolated state in the same process.
"""

from __future__ import annotations

import asyncio
import json
import time
import os
import base64
from datetime import datetime
from typing import Any

import httpx
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    BooleanSchema,
    IntegerSchema,
    NumberSchema,
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)

SUPERBROWSER_URL = "http://localhost:3100"
SCREENSHOT_DIR = os.environ.get("SUPERBROWSER_SCREENSHOT_DIR", "/tmp/superbrowser/screenshots")


async def _request_with_backoff(
    method: str,
    url: str,
    *,
    json: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = 30.0,
    max_retries: int = 3,
) -> httpx.Response:
    """POST/GET with jittered backoff on transient errors.

    Retries on 429 (rate-limited) and 503 (service overloaded) with
    delays roughly [1s, 2s, 4s] + small jitter. Honors the server's
    Retry-After header when present.

    This exists because a single run of nanobot fires 200-500 tool calls
    against our TS server — without backoff, a brief burst hitting the
    per-IP rate limiter would surface as a hard 429 that the LLM mis-
    classifies as a permanent outage and refuses to retry.
    """
    import random
    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(max_retries + 1):
            try:
                if method.upper() == "GET":
                    r = await client.get(url, params=params)
                else:
                    r = await client.post(url, json=json)
            except (httpx.ConnectError, httpx.ReadTimeout) as e:
                last_exc = e
                if attempt == max_retries:
                    raise
                delay = (2 ** attempt) + random.uniform(0, 0.5)
                print(f"  [net retry {attempt + 1}/{max_retries}] {type(e).__name__}: waiting {delay:.1f}s")
                await asyncio.sleep(delay)
                continue

            # Retryable status codes. Honor Retry-After if present.
            if r.status_code in (429, 503):
                if attempt == max_retries:
                    return r  # caller sees the 429 after all retries
                retry_after = r.headers.get("Retry-After")
                try:
                    retry_after_s = float(retry_after) if retry_after else None
                except ValueError:
                    retry_after_s = None
                delay = retry_after_s if retry_after_s is not None else (2 ** attempt) + random.uniform(0, 0.5)
                # Cap at 10s — Retry-After from a confused server could otherwise block the run.
                delay = min(10.0, delay)
                print(f"  [429 retry {attempt + 1}/{max_retries}] waiting {delay:.1f}s")
                await asyncio.sleep(delay)
                continue

            return r
    # Unreachable: loop either returns or raises.
    if last_exc:
        raise last_exc
    raise RuntimeError("request retry loop exited without return")


async def _fetch_feedback_state() -> dict[str, Any]:
    """Read the TS-side FeedbackBus snapshot over HTTP.

    Non-fatal on any failure — returns {} so callers fall through to the
    normal dispatch path (caller stays the same when the signal is down).
    """
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            r = await client.get(f"{SUPERBROWSER_URL}/feedback")
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict):
                    return data
    except Exception:
        pass
    return {}


async def _feedback_gate(tool_name: str) -> str | None:
    """Return a deferred-result string when another subsystem owns the
    browser right now (active captcha solve). None means `proceed`.

    Used at the top of mutating tools (click/type/scroll/navigate) to
    keep nanobot from racing the captcha solver — if the gate fires,
    nanobot gets an observation saying "captcha active, retry after 2s"
    and yields instead of firing a click that lands on a solved-then-
    reloaded page.
    """
    state = await _fetch_feedback_state()
    if state.get("captchaActive"):
        strategy = state.get("captchaStrategy") or "unknown"
        msg = (
            f"[feedback] {tool_name} deferred: captcha solve in progress "
            f"(strategy={strategy}). Retry after ~2000ms; do not issue "
            f"more actions until you see the captcha_done signal."
        )
        print(f"  {msg}")
        return msg
    return None

# After this many guard-refused browser_open calls in a single worker run, we
# stop being polite and abort the worker. The guard's text message is clearly
# not getting through to the LLM at this point and continuing would just
# drain the iteration budget on a no-op loop.
BLOCKED_BROWSER_OPEN_HARD_STOP = 3


class WorkerMustExitError(RuntimeError):
    """Raised from a tool when the worker must terminate immediately.

    Bubbles up through nanobot's tool runner. Carries a reason string the
    orchestrator can surface to the user so the failure mode is observable
    (vs. a silent iteration drain).
    """


_CAPTCHA_KEYWORDS = (
    "captcha", "recaptcha", "hcaptcha", "turnstile", "cloudflare",
    "verify you are human", "prove you are not a robot", "slider puzzle",
    "click all images", "select all", "drag the", "i'm not a robot",
)
_HARD_DOMAINS = (
    "apartments.com", "zillow.com", "ticketmaster.com", "nytimes.com",
    "linkedin.com", "instagram.com", "facebook.com",
)
# Hash length used to dedupe screenshots when page content changes on same URL.
_CONTENT_HASH_LEN = 500


def _compute_screenshot_budget(
    task_instruction: str = "",
    target_url: str = "",
    is_research: bool = False,
) -> int:
    """Task-complexity-aware screenshot budget.

    Base=6. +4 for research tasks, +10 for captcha-suspect tasks, +8 for
    known-hard domains. Capped at 30 to prevent runaway cost.
    """
    budget = 6
    lower_task = (task_instruction or "").lower()
    lower_url = (target_url or "").lower()
    if is_research:
        budget += 4
    if any(kw in lower_task for kw in _CAPTCHA_KEYWORDS):
        budget += 10
    if any(dom in lower_url for dom in _HARD_DOMAINS):
        budget += 8
    return min(budget, 30)


class BrowserSessionState:
    """Per-instance state for browser session tools.

    Each Nanobot instance that registers browser tools gets its own state.
    This prevents multi-agent setups from sharing globals.
    """

    # Default budget when no task context is supplied. Use
    # configure_budget() to switch to complexity-aware allocation.
    DEFAULT_SCREENSHOT_BUDGET = 6
    CAPTCHA_MODE_ITERATIONS = 15
    MAX_CLICK_AT = 3

    def __init__(self):
        self.max_screenshots = self.DEFAULT_SCREENSHOT_BUDGET
        self.screenshot_budget = self.max_screenshots
        self.vision_calls = 0
        self.text_calls = 0
        self.start_time = 0.0
        self.sessions_opened = 0
        self.activity_log: list[str] = []
        # Per-session (reset on each browser_open)
        self.step_counter = 0
        self.click_at_count = 0
        self.action_count = 0
        self.actions_since_screenshot = 0

        # Checkpoint & URL tracking
        self.task_id: str = ""
        self.checkpoints: list[dict] = []
        self.current_url: str = ""
        self.best_checkpoint_url: str = ""
        self.url_visit_counts: dict[str, int] = {}
        self.regression_count: int = 0
        # Dedupe key: (normalized_url, hash_of_content) — so a same URL with
        # changed content (e.g., after clicking "Load more") still allows a new
        # screenshot. Populated in mark_screenshot_taken().
        self.screenshotted_keys: set[tuple[str, str]] = set()
        self.last_screenshot_url: str = ""
        self.last_page_content_hash: str = ""
        self.step_history: list[dict] = []
        # Track consecutive click-type tool calls for loop detection
        self.consecutive_click_calls: int = 0
        # Active session ID (set by browser_open)
        self.session_id: str = ""
        # How many times has BrowserOpenTool had to refuse a redundant call?
        # The guard returns a stern message on the first few; if the LLM keeps
        # ignoring it past BLOCKED_BROWSER_OPEN_HARD_STOP, we raise to abort
        # the worker rather than silently drain its iteration budget.
        self.blocked_browser_open_count: int = 0

        # Captcha-mode: when a captcha is detected, relax the "no actions since
        # last screenshot" rule for CAPTCHA_MODE_ITERATIONS iterations. The
        # per-round counter `captcha_solve_round` is included in the dedup key
        # so each solve attempt gets its own screenshot allowance — preventing
        # the blanket bypass that used to let the worker screenshot the same
        # unchanged captcha 15 times.
        self.captcha_mode: bool = False
        self.captcha_mode_remaining: int = 0
        self.captcha_solve_round: int = 0

        # Per-index fingerprint cache. Populated on each /state fetch; read
        # by click/type tools to send `expected_fingerprint` along with the
        # request. Lets the TS side reject clicks that would land on a
        # different element than the LLM originally targeted (stale index).
        self.element_fingerprints: dict[int, str] = {}
        self.captcha_screenshots_used: int = 0
        # Hard cap on screenshots allowed within captcha mode (across rounds).
        # Kicks in only in captcha mode; the normal budget still caps overall.
        self.captcha_mode_screenshot_cap: int = 8

        # Network-layer block: set by browser_open/browser_navigate when the
        # target returns 4xx/5xx. Distinguishes "site blocks automated clients
        # before any page loads" from "page loaded but interaction failed" —
        # completely different failure classes needing different remediations
        # (IP/TLS/proxy vs. selector/timing/captcha).
        self.network_blocked: bool = False
        self.last_network_status: int | None = None

        # Human-in-the-loop handoff: when True, the TS server registers a
        # HumanInputManager for this session AND the captcha orchestrator
        # will fall back to human handoff after auto-strategies exhaust.
        # Orchestrator sets this before register_session_tools(). Default
        # False to preserve no-op behavior for workers that don't opt in.
        self.human_handoff_enabled: bool = False
        # Per-session budget — relayed to the TS server which enforces it.
        # 1 by default, overridable via SUPERBROWSER_MAX_HUMAN_HANDOFFS.
        self.human_handoff_budget: int = 1

        # Domain pinning: when set, BrowserNavigateTool rejects URLs
        # outside this domain (+ subdomains) and a small safe-list
        # (google.com, etc.). Prevents the worker LLM from hallucinating
        # to alternative sites when the target blocks it.
        self.pinned_domain: str = ""

        # Vision preprocessor bookkeeping. Populated inside
        # build_tool_result_blocks so tools that don't pass an intent
        # explicitly inherit the last one used — useful when the brain
        # fires a chained sequence (navigate → click → verify) with the
        # same underlying intent.
        self._last_intent: str = ""
        self._last_dom_hash: str = ""
        self._last_vision_summary: str = ""

    # --- budget configuration ---------------------------------------------

    def configure_budget(
        self,
        task_instruction: str = "",
        target_url: str = "",
        is_research: bool = False,
    ) -> int:
        """Set screenshot budget based on task complexity. Returns new budget."""
        self.max_screenshots = _compute_screenshot_budget(
            task_instruction=task_instruction,
            target_url=target_url,
            is_research=is_research,
        )
        self.screenshot_budget = self.max_screenshots
        return self.max_screenshots

    def enter_captcha_mode(self) -> None:
        """Relax screenshot limits for the next N iterations.

        Called when browser_detect_captcha returns a captcha. Captcha
        solving requires multiple screenshots per round (before drag,
        after drag, verify result) — normal budget would starve it.
        Resets per-round counters so a re-entry doesn't inherit stale
        dedup state from a previous challenge.
        """
        self.captcha_mode = True
        self.captcha_mode_remaining = self.CAPTCHA_MODE_ITERATIONS
        self.captcha_solve_round = 0
        self.captcha_screenshots_used = 0

    def tick_captcha_mode(self) -> None:
        """Decrement captcha_mode counter. Call once per agent iteration."""
        if not self.captcha_mode:
            return
        self.captcha_mode_remaining -= 1
        if self.captcha_mode_remaining <= 0:
            self.captcha_mode = False
            self.captcha_mode_remaining = 0

    def reset_per_session(self):
        """Reset per-session counters. Budget is NOT reset."""
        self.step_counter = 0
        self.click_at_count = 0
        self.action_count = 0
        self.actions_since_screenshot = 0

    def init_if_needed(self):
        if self.start_time == 0.0:
            self.start_time = time.time()

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Normalize URL for comparison (strip trailing slash, fragment)."""
        if not url:
            return ""
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(url)
        path = parsed.path.rstrip("/") or "/"
        return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, parsed.query, ""))

    def record_url(self, url: str) -> None:
        """Track a URL visit. Updates current_url and visit counts."""
        if not url:
            return
        norm = self._normalize_url(url)
        self.current_url = url
        self.url_visit_counts[norm] = self.url_visit_counts.get(norm, 0) + 1

    def record_checkpoint(self, url: str, title: str, action: str) -> None:
        """Record a progress checkpoint (successful meaningful step)."""
        self.checkpoints.append({
            "url": url, "title": title, "action": action,
            "time": datetime.now().strftime("%H:%M:%S"),
        })
        if url and url != self.best_checkpoint_url:
            self.best_checkpoint_url = url

    def is_regression(self, url: str) -> bool:
        """Check if navigating to url is going backward."""
        if not url or not self.best_checkpoint_url:
            return False
        norm = self._normalize_url(url)
        best_norm = self._normalize_url(self.best_checkpoint_url)
        # Regression = revisiting an earlier URL when we've been deeper
        if norm == best_norm:
            return False
        return self.url_visit_counts.get(norm, 0) > 0

    def should_allow_screenshot(
        self,
        url: str,
        content_hash: str = "",
    ) -> tuple[bool, str]:
        """Check if a screenshot should be allowed. Returns (allowed, reason).

        Captcha mode no longer blanket-bypasses dedup. Instead:
          - actions_since_screenshot check is relaxed (a captcha round might
            genuinely need multiple vision calls between tool actions)
          - captcha_solve_round is folded into the dedup key so each solve
            attempt gets its own allowance
          - a hard cap (captcha_mode_screenshot_cap) prevents runaway burn
            even if vision keeps failing
        """
        if self.screenshot_budget <= 0:
            return False, "[Screenshot budget exhausted] Use browser_get_markdown or browser_eval instead."
        if self.captcha_mode:
            if self.captcha_screenshots_used >= self.captcha_mode_screenshot_cap:
                return False, (
                    f"[Captcha-mode screenshot cap hit ({self.captcha_mode_screenshot_cap}). "
                    "The vision-based solve isn't converging. Call browser_ask_user for human help, "
                    "or browser_request_help to hand off to a fresh tactic.]"
                )
            norm = self._normalize_url(url)
            key = (norm, f"cap-round-{self.captcha_solve_round}:{content_hash or ''}")
            if norm and key in self.screenshotted_keys:
                return False, (
                    "[Captcha screenshot already taken for this solve round with no change. "
                    "Call browser_solve_captcha or browser_click a tile — don't re-screenshot the same state.]"
                )
            return True, ""
        if self.actions_since_screenshot == 0:
            return False, "[No actions since last screenshot — reuse previous. Use browser_get_markdown to re-read content.]"
        norm = self._normalize_url(url)
        # Dedupe on (url, content_hash) — if content changed since last shot
        # at this URL, allow a fresh screenshot.
        key = (norm, content_hash or "")
        if norm and key in self.screenshotted_keys:
            return False, "[Screenshot already exists for this URL + content. Use browser_get_markdown or browser_eval to read page state instead.]"
        return True, ""

    def mark_screenshot_taken(self, url: str, content_hash: str = "") -> None:
        """Record that a screenshot was taken for (url, content_hash).

        In captcha mode the key includes the current solve round so each
        solve attempt gets its own dedup allowance.
        """
        norm = self._normalize_url(url)
        if not norm:
            return
        if self.captcha_mode:
            self.captcha_screenshots_used += 1
            self.screenshotted_keys.add(
                (norm, f"cap-round-{self.captcha_solve_round}:{content_hash or ''}")
            )
        else:
            self.screenshotted_keys.add((norm, content_hash or ""))
        self.last_screenshot_url = norm
        self.last_page_content_hash = content_hash or ""

    @staticmethod
    def hash_page_content(text: str, scroll_y: int | None = None) -> str:
        """Structural fingerprint of a page for screenshot dedup.

        Replaces the old "SHA1 of first 500 chars" scheme, which was both
        insensitive to real changes (below-fold content, lazy loads) and
        over-sensitive to benign re-renders (React class-hash churn).

        Input is the `clickableElementsToString()` payload returned by the
        TS server — one line per interactive element, format roughly:
          `[0]<button aria-label="Sign in">Sign in</button>`

        Fingerprint inputs:
          - count of interactive elements (line count)
          - tag-name histogram (button/input/a/…)
          - top-N aria-label / placeholder / name values, normalized
          - scroll-Y bucketed to 100px when supplied

        All inputs are concatenated into a deterministic canonical string,
        then SHA1-hashed and truncated. Bucketing scroll keeps tiny scroll
        jitters from invalidating dedup while still distinguishing real
        scroll positions.
        """
        if not text:
            return ""
        import hashlib
        import re

        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        count = len(lines)

        # Tag histogram — parse `<tag` at the start of each element snippet.
        tag_counts: dict[str, int] = {}
        attr_pat = re.compile(r'(?:aria-label|placeholder|name)="([^"]+)"')
        attr_samples: list[str] = []
        for ln in lines[:40]:  # cap at 40 to keep bounded
            m = re.search(r"<([a-zA-Z][a-zA-Z0-9]*)", ln)
            if m:
                tag = m.group(1).lower()
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
            # Grab first meaningful attribute to anchor identity.
            a = attr_pat.search(ln)
            if a:
                val = a.group(1).strip().lower()
                # Normalize whitespace + drop digits (React IDs, counters).
                val = re.sub(r"\s+", " ", val)
                val = re.sub(r"\d+", "#", val)
                attr_samples.append(val[:40])

        hist = ",".join(f"{t}:{n}" for t, n in sorted(tag_counts.items()))
        # Use the first 20 normalized attributes — enough to differentiate
        # pages, few enough that a single added menu item doesn't flip the hash.
        attrs = "|".join(attr_samples[:20])
        scroll_bucket = ""
        if scroll_y is not None:
            scroll_bucket = f"s{int(scroll_y) // 100}"

        canonical = f"n={count}|h={hist}|a={attrs}|{scroll_bucket}"
        return hashlib.sha1(canonical.encode("utf-8", errors="ignore")).hexdigest()[:12]

    def record_step(self, tool_name: str, args_summary: str, result_summary: str) -> None:
        """Record a step in the structured step history."""
        self.step_history.append({
            "tool": tool_name,
            "args": args_summary,
            "result": result_summary[:200],
            "url": self.current_url,
            "time": datetime.now().strftime("%H:%M:%S"),
        })

    def get_last_checkpoint(self) -> dict | None:
        """Return the most recent checkpoint."""
        return self.checkpoints[-1] if self.checkpoints else None

    def export_step_history(self) -> str:
        """Export structured step history and checkpoint to disk.

        Writes TWO formats:
          - step_history.md  — human-readable markdown log
          - step_history.json — structured data the orchestrator parses
            to build domain-keyed captcha learnings and to inject prior
            context into subsequent tasks.
        """
        lines = ["## Step History"]
        for i, step in enumerate(self.step_history, 1):
            lines.append(f"{i}. [{step['time']}] {step['tool']}({step['args']}) → {step['result']}")
            if step.get("url"):
                lines.append(f"   URL: {step['url']}")

        if self.checkpoints:
            lines.append("\n## Checkpoints (progress markers)")
            for cp in self.checkpoints:
                lines.append(f"- [{cp['time']}] {cp['action']} → {cp['url']}")

        lines.append(f"\n## Best checkpoint URL: {self.best_checkpoint_url or 'none'}")
        lines.append(f"## Regressions detected: {self.regression_count}")

        content = "\n".join(lines)

        # Write to task-specific directory
        task_dir = f"/tmp/superbrowser/{self.task_id}" if self.task_id else "/tmp/superbrowser"
        os.makedirs(task_dir, exist_ok=True)
        step_path = os.path.join(task_dir, "step_history.md")
        with open(step_path, "w") as f:
            f.write(content)
        print(f"  [step history saved: {step_path}]")

        # Structured JSON export for orchestrator consumption.
        import json as _json_export
        structured = {
            "task_id": self.task_id,
            "sessions_opened": self.sessions_opened,
            "current_url": self.current_url,
            "best_checkpoint_url": self.best_checkpoint_url,
            "regression_count": self.regression_count,
            "checkpoints": self.checkpoints,
            "vision_calls": self.vision_calls,
            "text_calls": self.text_calls,
            "max_screenshots": self.max_screenshots,
            "screenshots_used": self.max_screenshots - self.screenshot_budget,
            "steps": self.step_history,
            "activity_log": self.activity_log,
        }
        json_path = os.path.join(task_dir, "step_history.json")
        try:
            with open(json_path, "w") as f:
                _json_export.dump(structured, f, indent=2, default=str)
            print(f"  [structured history saved: {json_path}]")
        except Exception as exc:  # pragma: no cover - best-effort persistence
            print(f"  [structured history save failed: {exc}]")

        # Save checkpoint as JSON for re-delegation
        if self.best_checkpoint_url:
            import json as _json
            checkpoint_data = {
                "url": self.best_checkpoint_url,
                "title": self.checkpoints[-1].get("title", "") if self.checkpoints else "",
                "regressions": self.regression_count,
            }
            cp_path = os.path.join(task_dir, "checkpoint.json")
            with open(cp_path, "w") as f:
                _json.dump(checkpoint_data, f)
            print(f"  [checkpoint saved: {cp_path}]")

        return content

    def log_activity(self, action: str, result: str = "ok"):
        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {action}"
        if result != "ok":
            entry += f" → {result}"
        self.activity_log.append(entry)
        if len(self.activity_log) > 30:
            self.activity_log.pop(0)

    def get_activity_summary(self) -> str:
        if not self.activity_log:
            return ""
        lines = "\n".join(self.activity_log[-15:])
        return (
            f"\n--- Previous activity (DO NOT repeat failed approaches) ---\n"
            f"{lines}\n"
            f"--- Screenshots remaining: {self.screenshot_budget}/{self.max_screenshots} | Sessions opened: {self.sessions_opened} ---"
        )

    def print_summary(self):
        elapsed = time.time() - self.start_time if self.start_time else 0
        used = self.max_screenshots - self.screenshot_budget
        print(f"\n  [Session Summary]")
        print(f"  Duration: {elapsed:.1f}s | Sessions: {self.sessions_opened}")
        print(f"  Vision calls: {self.vision_calls} | Text calls: {self.text_calls} | Screenshots: {used}/{self.max_screenshots}")
        est = self.vision_calls * 0.03 + self.text_calls * 0.002
        print(f"  Estimated cost: ~${est:.3f}")

    def export_activity_log(self) -> str:
        """Export structured activity log to disk for the orchestrator to read."""
        elapsed = time.time() - self.start_time if self.start_time else 0
        used = self.max_screenshots - self.screenshot_budget

        lines = [
            f"## Browser Worker Activity",
            f"Duration: {elapsed:.1f}s | Screenshots: {used}/{self.max_screenshots} | Tool calls: {self.vision_calls + self.text_calls}",
            "",
            "### Actions",
        ]
        lines.extend(self.activity_log)
        content = "\n".join(lines)

        # Write to disk so orchestrator/subagent can read it
        activity_path = "/tmp/superbrowser/last_activity.md"
        os.makedirs(os.path.dirname(activity_path), exist_ok=True)
        with open(activity_path, "w") as f:
            f.write(content)
        print(f"  [activity log saved: {activity_path}]")
        return content

    def save_screenshot(self, b64: str, label: str = "") -> str:
        self.step_counter += 1
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
        fn = f"{self.step_counter:03d}-{label}.jpg" if label else f"{self.step_counter:03d}.jpg"
        path = os.path.join(SCREENSHOT_DIR, fn)
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))
        print(f"  [screenshot saved: {path}]")
        return path

    async def build_tool_result_blocks(
        self,
        b64: str,
        caption: str,
        *,
        intent: str | None = None,
        url: str | None = None,
        elements: str | None = None,
        elements_with_bounds: list[dict] | None = None,
        device_pixel_ratio: float = 1.0,
    ) -> list[dict] | str:
        """Async dispatch between the vision-preprocessor path and the
        legacy image-blocks path.

        When `VISION_ENABLED=1` the screenshot is sent to the dedicated
        vision agent (cheap model) and the brain only sees its textual
        summary + bboxes + flags. Otherwise we fall through to the
        legacy `build_image_blocks` that embeds the JPEG directly.

        `intent` and `url` are optional hints used solely by the vision
        path. `elements` is the DOM element listing — hashed as a cache
        key so a re-screenshot on the same URL with identical DOM hits
        the cache.
        """
        # Lazy import: keeps the vision package optional at import time
        # so a broken VISION_API_KEY doesn't blow up sessions that never
        # enable the feature.
        try:
            from vision_agent import (
                dom_hash_of,
                get_vision_agent,
                vision_agent_enabled,
            )
        except ImportError:
            vision_agent_enabled = lambda: False  # type: ignore[assignment]
            get_vision_agent = None  # type: ignore[assignment]
            dom_hash_of = None  # type: ignore[assignment]

        if vision_agent_enabled() and get_vision_agent is not None:
            dh = dom_hash_of(elements) if dom_hash_of else ""
            if dh:
                self._last_dom_hash = dh
            if intent:
                self._last_intent = intent
            effective_intent = intent or self._last_intent or "observe page"
            effective_url = url or self.current_url
            try:
                agent = get_vision_agent()
                resp = await agent.analyze(
                    screenshot_b64=b64,
                    intent=effective_intent,
                    session_id=self.session_id,
                    url=effective_url,
                    dom_hash=dh or self._last_dom_hash,
                    previous_summary=self._last_vision_summary or None,
                )
                self._last_vision_summary = resp.summary
                self.vision_calls += 1
                self.actions_since_screenshot = 0
                label = (caption or "").split("\n")[0][:30].replace(" ", "-")
                # Still save the raw screenshot locally for debugging —
                # doesn't leave the box, doesn't reach the brain.
                self.save_screenshot(b64, label)
                text = f"{caption}\n\n{resp.as_brain_text()}" if caption else resp.as_brain_text()
                return [{"type": "text", "text": text}]
            except Exception as exc:
                # Never let a vision-layer failure break a tool result —
                # fall through to the legacy image path.
                print(f"  [vision-agent: falling back to image blocks — {exc}]")

        return self.build_image_blocks(
            b64,
            caption,
            elements_with_bounds=elements_with_bounds,
            device_pixel_ratio=device_pixel_ratio,
        )

    def build_image_blocks(
        self,
        b64: str,
        caption: str,
        elements_with_bounds: list[dict] | None = None,
        device_pixel_ratio: float = 1.0,
    ) -> list[dict]:
        """Build a vision-message-ready payload (text + image).

        If `elements_with_bounds` is provided, paint dashed bbox overlays +
        index labels on the screenshot so the LLM can ground on [index]
        instead of guessing pixel coordinates. Silently falls back to the
        raw screenshot if PIL is unavailable or overlay fails.
        """
        self.vision_calls += 1
        self.actions_since_screenshot = 0
        label = caption.split("\n")[0][:30].replace(" ", "-").replace("/", "_")

        final_b64 = b64
        if elements_with_bounds:
            try:
                from superbrowser_bridge.highlights import build_highlighted_screenshot
                final_b64 = build_highlighted_screenshot(
                    b64, elements_with_bounds, device_pixel_ratio,
                )
            except Exception:
                final_b64 = b64

        # Clamp the final payload to ≤ 2MB / ≤1568px side. Runs AFTER the
        # highlight overlay pass so overlay bloat can't tip a 1.8MB raw
        # screenshot over Gemini's 1.5MB-ish reject threshold.
        try:
            from superbrowser_bridge.image_safety import sanitize_image_b64
            final_b64 = sanitize_image_b64(final_b64)
        except Exception as e:
            print(f"  [image-safety: sanitize failed, sending raw: {e}]")

        self.save_screenshot(final_b64, label)
        return [
            {"type": "text", "text": caption},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{final_b64}"}},
        ]

    def build_text_only(self, data: dict, prefix: str = "") -> str:
        self.action_count += 1
        self.text_calls += 1
        self.actions_since_screenshot += 1
        parts = [prefix]
        if data.get("url"):
            parts.append(f"Page: {data['url']}")
        if data.get("title"):
            parts.append(f"Title: {data['title']}")
        result = " | ".join(p for p in parts if p)
        # Auto-include interactive elements so agent knows what's on page
        # (BrowserOS pattern: every action returns updated element snapshot)
        if data.get("elements"):
            result += f"\n\nInteractive elements:\n{data['elements']}"
        if data.get("consoleErrors"):
            result += f"\nConsole errors: {data['consoleErrors']}"
        if data.get("pendingDialogs"):
            result += f"\nPending dialogs: {data['pendingDialogs']}"
        if self.action_count >= 5:
            result += "\n\n[HINT: Use browser_run_script to batch remaining steps into ONE script.]"
        return result


async def _fetch_elements(session_id: str, state: "BrowserSessionState | None" = None) -> str:
    """Fetch current interactive elements without vision (cheap, no screenshot).

    This is the key BrowserOS pattern: every action gets a fresh element snapshot
    so the agent always knows what's on the page without wasting a screenshot.

    If `state` is passed, we ALSO update `state.element_fingerprints` with
    the fresh per-index fingerprint map. Click/type tools then send the
    cached fingerprint as `expected_fingerprint` so the TS side can reject
    stale-index clicks (DOM shifted between state-fetch and click).
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{SUPERBROWSER_URL}/session/{session_id}/state",
                params={"vision": "false"},
            )
            r.raise_for_status()
            data = r.json()
        if state is not None:
            fps = data.get("fingerprints") or {}
            if isinstance(fps, dict):
                # JSON keys come back as strings; coerce to int for direct index lookup.
                state.element_fingerprints = {int(k): v for k, v in fps.items() if isinstance(v, str)}
        return data.get("elements", "")
    except Exception:
        return ""


def _build_network_block_message(status_code: int, url: str) -> str:
    """Structured message when a page returns 4xx/5xx — tells the worker to
    stop immediately rather than trying interactions on a blocked shell.

    This is distinct from CAPTCHA: CAPTCHA returns 200 + a challenge page.
    A 403/429/503 means the bot-detection edge refused to serve content at
    all, so no amount of clicking will help. The right move is to exit the
    worker via done(success=False) so the orchestrator can route to the
    search worker or escalate (proxy, TLS fingerprinting, etc.).
    """
    reason_hint = {
        401: "Authentication required — this page needs a logged-in session.",
        403: "Forbidden — site's bot detection refused at the network layer. No page interaction will help.",
        404: "Page not found at this URL.",
        429: "Rate-limited — the site throttled our requests. Different IP may help.",
        451: "Blocked for legal reasons (geographic restriction likely).",
        503: "Service unavailable — could be bot detection (Cloudflare/Akamai) or real outage.",
    }.get(status_code, "Server returned an error status — page content is not usable.")
    return (
        f"\n\n[NETWORK_BLOCKED status={status_code} url={url}]\n"
        f"{reason_hint}\n"
        f"ACTION: do NOT attempt further interactions. Call "
        f"done(success=False, final_answer='NETWORK_BLOCKED: HTTP {status_code} at {url}') "
        f"so the orchestrator can escalate (try a different approach, search worker, or request proxy)."
    )


def _format_state(data: dict, state: "BrowserSessionState | None" = None) -> str:
    parts: list[str] = []
    # Leading structured marker that survives tool-result truncation. Even
    # when maxToolResultChars slices the trailing base64 image apart, these
    # first ~120 characters stay intact, so the worker's LLM can always see
    # "the tool succeeded; a session is open" and won't fire a redundant
    # browser_open.
    session_id = data.get("sessionId") or (state.session_id if state else "")
    url = data.get("url") or ""
    title = (data.get("title") or "").replace('"', "'")[:80]
    step = state.step_counter if state else 0
    if session_id or url:
        parts.append(
            f'[SESSION_STATE session_id={session_id or "?"} '
            f'url={url or "?"} title="{title}" step={step}]'
        )
    if data.get("url"):
        parts.append(f"URL: {data['url']}")
    if data.get("title"):
        parts.append(f"Title: {data['title']}")
    if data.get("scrollInfo"):
        si = data["scrollInfo"]
        parts.append(f"Scroll: {si.get('scrollY', 0)}/{si.get('scrollHeight', 0)} (viewport: {si.get('viewportHeight', 0)})")
    if data.get("elements"):
        parts.append(f"\nInteractive elements:\n{data['elements']}")
    if data.get("consoleErrors"):
        parts.append(f"\nConsole errors: {data['consoleErrors']}")
    if data.get("pendingDialogs"):
        parts.append(f"\nPending dialogs: {data['pendingDialogs']}")
    return "\n".join(parts)


# ── Tool classes — each holds a reference to shared BrowserSessionState ──

@tool_parameters(
    tool_parameters_schema(
        url=StringSchema("URL to open (optional)", nullable=True),
        region=StringSchema("Region code for geo-restricted sites (e.g., 'bd', 'in')", nullable=True),
        proxy=StringSchema("Direct proxy URL (e.g., 'socks5://proxy:1080')", nullable=True),
        intent=StringSchema(
            "Optional hint describing what you want from the vision agent "
            "(e.g. 'check if login is required', 'find search box'). "
            "Only used when VISION_ENABLED=1.",
            nullable=True,
        ),
        required=[],
    )
)
class BrowserOpenTool(Tool):
    name = "browser_open"
    description = (
        "Open a new browser session. Returns a screenshot and interactive elements. "
        "For geo-restricted sites, pass region='bd' (Bangladesh), 'in' (India), etc."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, url: str | None = None, region: str | None = None, proxy: str | None = None, intent: str | None = None, **kw: Any) -> Any:
        self.s.init_if_needed()

        # --- Idempotency guard ------------------------------------------
        # Two paths reach this tool with a live session already:
        #   1. The worker's LLM is in an amnesia loop (truncated/stripped
        #      screenshots → can't tell browser_open already ran) and is
        #      firing it again for the same URL.
        #   2. The orchestrator pre-seeded self.s.session_id from a
        #      resumption artifact (orchestrator_tools.py resumption path)
        #      and the worker's LLM ignored the "DO NOT call browser_open"
        #      instruction in the prompt.
        # In both cases creating a second real session is the bug — it
        # overwrites session_id with a throwaway and discards any progress.
        # Return a plain-string message (no image blocks, so truncation
        # can't mangle it) pointing the LLM at the right next tool.
        if self.s.session_id:
            self.s.blocked_browser_open_count += 1
            if self.s.blocked_browser_open_count >= BLOCKED_BROWSER_OPEN_HARD_STOP:
                raise WorkerMustExitError(
                    f"browser_open called {self.s.blocked_browser_open_count} "
                    f"times after the idempotency guard refused it. The LLM "
                    f"is in a tight loop ignoring the guard message. "
                    f"Aborting worker to prevent iteration drain. "
                    f"session_id={self.s.session_id}"
                )
            same_url = (
                not url
                or self.s._normalize_url(url) == self.s._normalize_url(self.s.current_url)
            )
            print(
                f"\n>> browser_open BLOCKED (session already active: "
                f"{self.s.session_id}) — refusal #{self.s.blocked_browser_open_count}"
            )
            if same_url:
                return (
                    f"[SESSION_ALREADY_OPEN session_id={self.s.session_id} "
                    f"url={self.s.current_url}]\n"
                    f"A browser session is already active on this URL. "
                    f"DO NOT call browser_open again — it would discard your "
                    f"current page.\n"
                    f"Use one of these instead:\n"
                    f"  - browser_screenshot(session_id=\"{self.s.session_id}\") "
                    f"to see the current view\n"
                    f"  - browser_get_markdown(session_id=\"{self.s.session_id}\") "
                    f"to read the page text\n"
                    f"  - browser_click / browser_type to interact\n"
                    f"  - browser_navigate(session_id=\"{self.s.session_id}\", "
                    f"url=\"...\") to switch URLs on the same session"
                )
            return (
                f"[WRONG_TOOL session_id={self.s.session_id} current_url={self.s.current_url}]\n"
                f"You asked to open a different URL ({url}) but a session is "
                f"already active. Use browser_navigate on the existing session — "
                f"do NOT call browser_open, which would create a throwaway "
                f"second session and discard your current page.\n"
                f"  browser_navigate(session_id=\"{self.s.session_id}\", url=\"{url}\")"
            )

        self.s.reset_per_session()
        self.s.sessions_opened += 1

        print(f"\n>> browser_open(url={url}, region={region}) [session #{self.s.sessions_opened}, screenshots left: {self.s.screenshot_budget}]")

        payload: dict[str, Any] = {}
        if url:
            payload["url"] = url
        if region:
            payload["region"] = region
        if proxy:
            payload["proxy"] = proxy
        # Opt in to human handoff on the TS side. The server instantiates a
        # HumanInputManager for this session and the captcha orchestrator
        # can fall back to a human handoff after auto-solve exhausts.
        if self.s.human_handoff_enabled:
            payload["enableHumanHandoff"] = True
            payload["humanHandoffBudget"] = self.s.human_handoff_budget

        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/create",
            json=payload,
            timeout=45.0,
        )
        if r.status_code == 429:
            # Retries exhausted. Give the LLM a CLEAR hint that this is
            # transient — otherwise it hallucinates a permanent outage
            # (observed 2026-04-14 log: "the service is completely
            # blocking session creation") and gives up entirely.
            return (
                "[transient_rate_limit] Browser session service is busy "
                "(HTTP 429 after retries). This is a temporary rate limit, "
                "NOT a permanent outage. Wait ~30 seconds and call "
                "browser_open again. Do not switch to a different strategy."
            )
        r.raise_for_status()
        data = r.json()

        actual_url = data.get("url", url or "")
        self.s.session_id = data.get("sessionId", "")
        self.s.log_activity(f"browser_open({url or 'blank'})", f"session={data.get('sessionId', '?')}")
        self.s.record_url(actual_url)
        self.s.record_checkpoint(actual_url, data.get("title", ""), f"browser_open({url or 'blank'})")
        self.s.record_step("browser_open", url or "blank", f"session={data.get('sessionId', '?')}")
        self.s.consecutive_click_calls = 0

        # If human handoff is enabled, print the view URL to stdout so the
        # user can pre-open it in their browser. The view page polls the
        # /human-input endpoint and will show a banner the instant the
        # agent needs help, so having it open beforehand eliminates the
        # race where the agent blocks for 5 min before the user notices.
        if self.s.human_handoff_enabled and self.s.session_id:
            public_host = os.environ.get(
                "SUPERBROWSER_PUBLIC_HOST", SUPERBROWSER_URL.rstrip("/"),
            )
            view_url = f"{public_host}/session/{self.s.session_id}/view"
            print(
                f"\n>> [HUMAN HANDOFF ENABLED] Open this URL in your browser "
                f"and keep it open:\n>>   {view_url}\n>> "
                f"If the agent needs help, you'll see a banner there."
            )

        caption = _format_state(data, self.s)
        caption = f"Session: {data['sessionId']}\n{caption}"

        # Network-layer block detection (4xx/5xx). Fast-fails before the worker
        # wastes iterations on an unresponsive page. 404 is treated as fatal
        # here (wrong URL) but not a network block per se.
        status_code = data.get("statusCode")
        if isinstance(status_code, int):
            self.s.last_network_status = status_code
            if status_code >= 400 and status_code != 404:
                self.s.network_blocked = True
                caption += _build_network_block_message(status_code, actual_url)
                self.s.record_step("browser_open", url or "blank", f"NETWORK_BLOCKED status={status_code}")
                return caption
            elif status_code == 404:
                caption += _build_network_block_message(404, actual_url)
                return caption

        # Surface captcha detection from the server
        if data.get("captchaDetected"):
            ct = data["captchaDetected"]["type"]
            caption += (
                f"\n\n[CAPTCHA DETECTED: {ct}] "
                f"Call browser_solve_captcha(session_id='{data['sessionId']}', method='auto') to solve it."
            )

        # Show previous activity so agent knows what was already tried
        if self.s.sessions_opened > 1:
            activity = self.s.get_activity_summary()
            if activity:
                caption += activity

        if data.get("screenshot") and self.s.screenshot_budget > 0:
            self.s.screenshot_budget -= 1
            if actual_url:
                self.s.mark_screenshot_taken(
                    actual_url,
                    self.s.hash_page_content(data.get("elements", "") or data.get("title", "")),
                )
            return await self.s.build_tool_result_blocks(
                data["screenshot"],
                caption,
                intent=intent or "observe opened page",
                url=actual_url,
                elements=data.get("elements"),
            )
        return caption


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID from browser_open"),
        url=StringSchema("URL to navigate to"),
        intent=StringSchema(
            "Optional hint for the vision agent (e.g. 'verify navigation "
            "succeeded', 'find sign-up button'). Only used when "
            "VISION_ENABLED=1.",
            nullable=True,
        ),
        required=["session_id", "url"],
    )
)
class BrowserNavigateTool(Tool):
    name = "browser_navigate"
    description = "Navigate to a URL in an open browser session."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, url: str, intent: str | None = None, **kw: Any) -> Any:
        print(f"\n>> browser_navigate({url})")
        gate = await _feedback_gate("browser_navigate")
        if gate:
            return gate

        # --- Domain-pinning guard -----------------------------------------
        # When pinned_domain is set, only allow navigation to the target
        # domain (+ subdomains) and a small safe-list. Prevents the worker
        # LLM from visiting alternative sites when the target blocks it.
        if self.s.pinned_domain:
            from urllib.parse import urlparse as _urlparse
            _SAFE_DOMAINS = ("google.com", "googleapis.com", "gstatic.com", "google.co")
            try:
                _target_host = (_urlparse(url).hostname or "").lower().replace("www.", "")
            except Exception:
                _target_host = ""
            _pinned = self.s.pinned_domain
            _is_pinned = _target_host == _pinned or _target_host.endswith("." + _pinned)
            _is_safe = any(
                _target_host == sd or _target_host.endswith("." + sd)
                for sd in _SAFE_DOMAINS
            )
            if _target_host and not _is_pinned and not _is_safe:
                self.s.record_step("browser_navigate", url, f"BLOCKED: outside pinned domain {_pinned}")
                print(f"   [DOMAIN_PINNED] blocked navigation to {_target_host} (pinned={_pinned})")
                return (
                    f"[DOMAIN_PINNED] Navigation to {url} is BLOCKED. "
                    f"You MUST stay on {_pinned} (and its subdomains). "
                    f"Do NOT visit other sites to find the answer. "
                    f"If {_pinned} is blocking you, call browser_solve_captcha or "
                    f"browser_ask_user, or report failure via done(success=False)."
                )

        self.s.actions_since_screenshot += 1
        self.s.consecutive_click_calls = 0

        # Detect regression before navigating
        regression = self.s.is_regression(url)
        if regression:
            self.s.regression_count += 1

        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(f"{SUPERBROWSER_URL}/session/{session_id}/navigate", json={"url": url})
            r.raise_for_status()
            data = r.json()

        actual_url = data.get("url", url)
        self.s.log_activity(f"navigate({url})", f"title={data.get('title', '?')}")
        self.s.record_url(actual_url)

        caption = _format_state(data, self.s)

        # Network-layer block detection — same logic as browser_open. Exit
        # early so the worker doesn't try to interact with a 403/429 shell.
        status_code = data.get("statusCode")
        if isinstance(status_code, int):
            self.s.last_network_status = status_code
            if status_code >= 400 and status_code != 404:
                self.s.network_blocked = True
                caption += _build_network_block_message(status_code, actual_url)
                self.s.record_step("browser_navigate", url, f"NETWORK_BLOCKED status={status_code}")
                return caption
            elif status_code == 404:
                caption += _build_network_block_message(404, actual_url)
                self.s.record_step("browser_navigate", url, f"HTTP 404 at {actual_url}")
                return caption

        self.s.record_step("browser_navigate", url, f"title={data.get('title', '?')}")

        if regression:
            caption += "\n[WARNING: You already visited this URL. Fix your approach on the CURRENT page instead of going backward. Do NOT restart from the beginning.]"

        # Surface captcha detection from the server
        if data.get("captchaDetected"):
            ct = data["captchaDetected"]["type"]
            caption += (
                f"\n\n[CAPTCHA DETECTED: {ct}] "
                f"Call browser_solve_captcha(session_id='{session_id}', method='auto') to solve it."
            )

        if data.get("screenshot") and self.s.screenshot_budget > 0:
            self.s.screenshot_budget -= 1
            if actual_url:
                self.s.mark_screenshot_taken(
                    actual_url,
                    self.s.hash_page_content(data.get("elements", "") or data.get("title", "")),
                )
            return await self.s.build_tool_result_blocks(
                data["screenshot"],
                caption,
                intent=intent or "verify navigation succeeded",
                url=actual_url,
                elements=data.get("elements"),
            )
        return caption


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        intent=StringSchema(
            "Optional hint for the vision agent. Only used when "
            "VISION_ENABLED=1.",
            nullable=True,
        ),
        required=["session_id"],
    )
)
class BrowserScreenshotTool(Tool):
    name = "browser_screenshot"
    description = "Take a screenshot. COSTS MONEY. Use browser_get_markdown or browser_eval to verify instead."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, intent: str | None = None, **kw: Any) -> Any:
        # Peek current page content so dedup keys on (url, content_hash)
        # — a reload or DOM change produces a different hash and unblocks.
        peek_hash = ""
        try:
            peek_elements = await _fetch_elements(session_id, self.s)
            peek_hash = BrowserSessionState.hash_page_content(peek_elements)
        except Exception:
            pass

        allowed, reason = self.s.should_allow_screenshot(self.s.current_url, peek_hash)
        if not allowed:
            self.s.log_activity("screenshot(BLOCKED)", reason[:60])
            return reason

        self.s.screenshot_budget -= 1
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{SUPERBROWSER_URL}/session/{session_id}/state",
                # bounds=true returns selectorEntries (with x/y/width/height) +
                # devicePixelRatio so we can draw bbox overlays before the
                # screenshot goes to the vision LLM.
                params={"vision": "true", "bounds": "true"},
            )
            r.raise_for_status()
            data = r.json()

        actual_url = data.get("url", self.s.current_url)
        if actual_url:
            self.s.mark_screenshot_taken(
                actual_url,
                self.s.hash_page_content(data.get("elements", "")),
            )
        self.s.log_activity(f"screenshot({actual_url[:50] if actual_url else '?'})")
        self.s.record_step("browser_screenshot", "", f"url={actual_url[:60] if actual_url else '?'}")
        caption = _format_state(data, self.s)
        caption += f"\n[Screenshots remaining: {self.s.screenshot_budget}]"
        if data.get("screenshot"):
            entries = data.get("selectorEntries") or []
            dpr = float(data.get("devicePixelRatio") or 1.0)
            # Rename tagName → tag for the overlay (both naming schemes work
            # but tag is the overlay's canonical key).
            overlay_elements = [
                {
                    "index": e.get("index"),
                    "tag": e.get("tagName") or e.get("tag"),
                    "role": e.get("role") or (e.get("attributes") or {}).get("role"),
                    "bounds": e.get("bounds"),
                }
                for e in entries
                if e.get("bounds") and e.get("index") is not None
            ]
            return await self.s.build_tool_result_blocks(
                data["screenshot"],
                caption,
                intent=intent or "observe page",
                url=actual_url,
                elements=data.get("elements"),
                elements_with_bounds=overlay_elements,
                device_pixel_ratio=dpr,
            )
        return caption


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        index=IntegerSchema(description="Element index"),
        button=StringSchema("Mouse button: left, right, middle", nullable=True),
        required=["session_id", "index"],
    )
)
class BrowserClickTool(Tool):
    name = "browser_click"
    description = "Click an interactive element by its [index] number."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, index: int, button: str | None = None, **kw: Any) -> Any:
        print(f"\n>> browser_click([{index}])")
        gate = await _feedback_gate("browser_click")
        if gate:
            return gate
        self.s.consecutive_click_calls += 1
        payload: dict[str, Any] = {"index": index}
        if button:
            payload["button"] = button
        # Send the fingerprint the LLM was targeting. If the DOM shifted,
        # the TS side returns 409 + stale_index with a suggested new index.
        cached_fp = self.s.element_fingerprints.get(index)
        if cached_fp:
            payload["expected_fingerprint"] = cached_fp

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(f"{SUPERBROWSER_URL}/session/{session_id}/click", json=payload)
                # 409 = stale-index guard fired. Surface the suggested
                # index (if any) so the LLM retargets instead of blindly
                # retrying or falling back to click_at coords.
                if r.status_code == 409:
                    info = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                    stale_msg = info.get("error", "Stale index")
                    suggested = info.get("suggested_index")
                    current = info.get("current_element", "")
                    hint = f" Try [{suggested}]." if suggested is not None else " Re-read elements list and pick again."
                    result = f"[stale_index] {stale_msg} Current [{index}] is {current}.{hint}"
                    self.s.log_activity(f"click([{index}])(STALE)", f"suggested={suggested}")
                    await _fetch_elements(session_id, self.s)
                    return result
                # 400 = structured TS-side failure (element not found,
                # not visible, disabled, etc.). Parse it and return an
                # actionable message to the LLM — crucially, DO NOT fall
                # back to JS click for these: JS click against a missing
                # element also fails, and the raw Python exception string
                # that comes out ('Client error 400 Bad Request for URL')
                # is un-actionable enough that Gemini will sometimes
                # return an empty response on the next turn (observed
                # 2026-04-14). Structured text here prevents that.
                if r.status_code == 400:
                    info = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                    reason = info.get("reason", "unknown")
                    err = info.get("error", f"click [{index}] failed")
                    alternatives = info.get("alternatives") or []
                    await _fetch_elements(session_id, self.s)
                    self.s.log_activity(f"click([{index}])({reason})", err[:60])
                    alt_lines = "\n".join(f"  - {a}" for a in alternatives[:3]) if alternatives else ""
                    fresh_hint = "\nElements have been re-read above — pick a current [index]."
                    return (
                        f"[click_failed:{reason}] {err}"
                        + (f"\nAlternatives:\n{alt_lines}" if alt_lines else "")
                        + fresh_hint
                    )
                r.raise_for_status()
                data = r.json()
        except httpx.HTTPStatusError as e:
            # Opaque 4xx/5xx (not 400/409). Usually network-layer.
            self.s.log_activity(f"click([{index}])(HTTP{e.response.status_code})", str(e)[:60])
            return (
                f"[click_failed:http_{e.response.status_code}] {e.response.text[:200] if e.response.text else str(e)[:200]}"
            )
        except Exception as e:
            # True transport error (connection refused, timeout). JS
            # fallback doesn't help here either — the server is down.
            self.s.log_activity(f"click([{index}])(TRANSPORT)", str(e)[:60])
            return f"[click_failed:transport] {str(e)[:200]} — browser service unreachable. Retry in a few seconds."

        actual_url = data.get("url", self.s.current_url)
        if actual_url:
            self.s.record_url(actual_url)
        self.s.log_activity(f"click([{index}])", f"url={actual_url[:50] if actual_url else '?'}")
        self.s.record_step("browser_click", f"index={index}", f"url={actual_url[:60] if actual_url else '?'}")
        return self.s.build_text_only(data, f"Clicked [{index}]")


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        x=NumberSchema(description="X coordinate"),
        y=NumberSchema(description="Y coordinate"),
        required=["session_id", "x", "y"],
    )
)
class BrowserClickAtTool(Tool):
    name = "browser_click_at"
    description = "Click at x,y coordinates. LAST RESORT — prefer browser_run_script."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, session_id: str, x: float, y: float, **kw: Any) -> Any:
        self.s.click_at_count += 1
        self.s.consecutive_click_calls += 1
        if self.s.click_at_count > self.s.MAX_CLICK_AT:
            return f"[BLOCKED] browser_click_at used {self.s.click_at_count} times. Use browser_run_script instead."
        print(f"\n>> browser_click_at({x}, {y})")
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(f"{SUPERBROWSER_URL}/session/{session_id}/click", json={"x": x, "y": y})
            # 409 = reward-band reject. Historical data says this zone
            # doesn't respond to clicks on this host; surface the hint
            # so the LLM re-reads elements instead of trying another
            # nearby coord.
            if r.status_code == 409:
                info = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                err = info.get("error") or "click_at rejected: low-reward zone"
                self.s.log_activity(f"click_at({x},{y})(BAND_REJECT)", f"band={info.get('band')}")
                return f"[low_reward_band] {err}"
            r.raise_for_status()
            data = r.json()
        actual_url = data.get("url", self.s.current_url)
        if actual_url:
            self.s.record_url(actual_url)
        self.s.record_step("browser_click_at", f"x={x},y={y}", f"url={actual_url[:60] if actual_url else '?'}")
        return self.s.build_text_only(data, f"Clicked at ({x}, {y})")


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        index=IntegerSchema(description="Element index"),
        text=StringSchema("Text to type"),
        clear=BooleanSchema(description="Clear field first (default: true)", default=True),
        required=["session_id", "index", "text"],
    )
)
class BrowserTypeTool(Tool):
    name = "browser_type"
    description = "Type text into an input field by its [index] number."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, index: int, text: str, clear: bool = True, **kw: Any) -> Any:
        print(f'\n>> browser_type([{index}], "{text}")')
        gate = await _feedback_gate("browser_type")
        if gate:
            return gate
        self.s.consecutive_click_calls += 1  # type is also step-by-step
        payload: dict[str, Any] = {"index": index, "text": text, "clear": clear}
        cached_fp = self.s.element_fingerprints.get(index)
        if cached_fp:
            payload["expected_fingerprint"] = cached_fp
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(f"{SUPERBROWSER_URL}/session/{session_id}/type", json=payload)
            if r.status_code == 409:
                info = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                suggested = info.get("suggested_index")
                current = info.get("current_element", "")
                hint = f" Try [{suggested}]." if suggested is not None else " Re-read elements list and pick again."
                await _fetch_elements(session_id, self.s)
                return f"[stale_index] Element [{index}] is now {current}.{hint}"
            # Same structured-400 handling as BrowserClickTool — avoid
            # surfacing raw 'Client error 400' which empties Gemini's
            # next turn.
            if r.status_code == 400:
                info = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                reason = info.get("reason", "unknown")
                err = info.get("error", f"type [{index}] failed")
                alternatives = info.get("alternatives") or []
                await _fetch_elements(session_id, self.s)
                self.s.log_activity(f"type([{index}])({reason})", err[:60])
                alt_lines = "\n".join(f"  - {a}" for a in alternatives[:3]) if alternatives else ""
                return (
                    f"[type_failed:{reason}] {err}"
                    + (f"\nAlternatives:\n{alt_lines}" if alt_lines else "")
                    + "\nElements have been re-read above — pick a current [index]."
                )
            r.raise_for_status()
            data = r.json()
        self.s.record_step("browser_type", f'index={index}, text="{text[:30]}"', "ok")
        return self.s.build_text_only(data, f'Typed "{text}" into [{index}]')


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        keys=StringSchema("Keys to send (e.g. Enter, ArrowDown, Tab)"),
        required=["session_id", "keys"],
    )
)
class BrowserKeysTool(Tool):
    name = "browser_keys"
    description = "Send keyboard keys or shortcuts."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, keys: str, **kw: Any) -> Any:
        print(f"\n>> browser_keys({keys})")
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(f"{SUPERBROWSER_URL}/session/{session_id}/keys", json={"keys": keys})
            r.raise_for_status()
            data = r.json()
        # Fetch updated elements after key press (e.g., Enter may submit form)
        if not data.get("elements"):
            elements = await _fetch_elements(session_id, self.s)
            if elements:
                data["elements"] = elements
        return self.s.build_text_only(data, f"Sent keys: {keys}")


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        direction=StringSchema("Scroll direction: up or down", nullable=True),
        percent=NumberSchema(description="Scroll to exact percentage 0-100", nullable=True),
        required=["session_id"],
    )
)
class BrowserScrollTool(Tool):
    name = "browser_scroll"
    description = "Scroll the page up or down, or to a specific percentage."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, direction: str | None = None, percent: float | None = None, **kw: Any) -> Any:
        print(f"\n>> browser_scroll({direction or f'{percent}%'})")
        gate = await _feedback_gate("browser_scroll")
        if gate:
            return gate
        payload: dict[str, Any] = {}
        if percent is not None:
            payload["percent"] = percent
        else:
            payload["direction"] = direction or "down"
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(f"{SUPERBROWSER_URL}/session/{session_id}/scroll", json=payload)
            r.raise_for_status()
            data = r.json()
        # Fetch updated elements after scroll (new elements may be visible)
        if not data.get("elements"):
            elements = await _fetch_elements(session_id, self.s)
            if elements:
                data["elements"] = elements
        action = f"Scrolled to {percent}%" if percent is not None else f"Scrolled {direction or 'down'}"
        return self.s.build_text_only(data, action)


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        index=IntegerSchema(description="Element index of the select/dropdown"),
        value=StringSchema("Option value or visible text to select"),
        required=["session_id", "index", "value"],
    )
)
class BrowserSelectTool(Tool):
    name = "browser_select"
    description = "Select an option in a dropdown by value."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, index: int, value: str, **kw: Any) -> Any:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(f"{SUPERBROWSER_URL}/session/{session_id}/select", json={"index": index, "value": value})
            r.raise_for_status()
            data = r.json()
        # Fetch updated elements after selection (may trigger form changes)
        if not data.get("elements"):
            elements = await _fetch_elements(session_id, self.s)
            if elements:
                data["elements"] = elements
        return self.s.build_text_only(data, f'Selected "{value}" in [{index}]')


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        script=StringSchema("JavaScript code to execute in the page"),
        required=["session_id", "script"],
    )
)
class BrowserEvalTool(Tool):
    name = "browser_eval"
    description = "Execute JavaScript in the browser page. FREE — no screenshot cost."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, session_id: str, script: str, **kw: Any) -> str:
        self.s.actions_since_screenshot += 1
        self.s.consecutive_click_calls = 0  # eval resets click loop tracking
        print(f"\n>> browser_eval({script[:60]}...)")
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(f"{SUPERBROWSER_URL}/session/{session_id}/evaluate", json={"script": script})
            r.raise_for_status()
            data = r.json()
        result = data.get("result")
        result_str = json.dumps(result, indent=2, ensure_ascii=False)[:5000] if isinstance(result, (dict, list)) else str(result)[:5000]
        self.s.log_activity(f"eval({script[:40]}...)", result_str[:60])
        self.s.record_step("browser_eval", script[:60], result_str[:100])
        return result_str


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        script=StringSchema(
            "Puppeteer script body. Variables: page (Puppeteer Page), context, helpers (sleep, screenshot, log)."
        ),
        context=ObjectSchema(description="Optional context data", nullable=True),
        timeout=IntegerSchema(description="Script timeout in ms (default: 60000)", nullable=True),
        required=["session_id", "script"],
    )
)
class BrowserRunScriptTool(Tool):
    name = "browser_run_script"
    description = (
        "Execute a Puppeteer script with full page API access. "
        "Use for complex multi-step automation."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, script: str, context: dict | None = None, timeout: int | None = None, **kw: Any) -> str:
        print(f"\n>> browser_run_script({script[:80]}...)")
        self.s.consecutive_click_calls = 0  # script execution resets click loop tracking
        payload: dict[str, Any] = {"code": script}
        if context:
            payload["context"] = context
        if timeout:
            payload["timeout"] = timeout

        client_timeout = max(120.0, (timeout or 60000) / 1000 + 10)
        async with httpx.AsyncClient(timeout=client_timeout) as client:
            r = await client.post(f"{SUPERBROWSER_URL}/session/{session_id}/script", json=payload)
            r.raise_for_status()
            data = r.json()

        self.s.actions_since_screenshot += 1

        if not data.get("success"):
            error = data.get("error", "Unknown error")
            self.s.log_activity("run_script(FAILED)", error[:100])
            self.s.record_step("browser_run_script", script[:60], f"FAILED: {error[:100]}")
            # Fetch current elements so agent can see what's on the page and fix the script
            elements = await _fetch_elements(session_id, self.s)
            tip = "\n[TIP: Fix the script and retry in this SAME session. Do NOT navigate back to the start.]"
            if elements:
                tip += f"\n\nCurrent interactive elements:\n{elements}"
            return f"Script error: {error}{tip}"

        parts = []
        result = data.get("result")
        if result is not None:
            if isinstance(result, (dict, list)):
                parts.append(f"Result: {json.dumps(result, indent=2, ensure_ascii=False)[:5000]}")
            else:
                parts.append(f"Result: {str(result)[:5000]}")

        logs = data.get("logs", [])
        if logs:
            parts.append("Logs:\n" + "\n".join(logs[:20]))

        duration = data.get("duration", 0)
        parts.append(f"Duration: {duration}ms")
        self.s.log_activity(f"run_script(ok, {duration}ms)", str(result)[:60] if result else "void")
        self.s.record_step("browser_run_script", script[:60], str(result)[:100] if result else "void")
        self.s.record_checkpoint(self.s.current_url, "", f"run_script(ok, {duration}ms)")

        # Auto-include updated elements so agent sees current page state
        elements = await _fetch_elements(session_id, self.s)
        if elements:
            parts.append(f"\nInteractive elements:\n{elements}")

        return "\n".join(parts)


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        text=StringSchema("Text to wait for on the page", nullable=True),
        selector=StringSchema("CSS selector to wait for", nullable=True),
        timeout=IntegerSchema(description="Max wait time in seconds (default: 10)", nullable=True),
        required=["session_id"],
    )
)
class BrowserWaitForTool(Tool):
    name = "browser_wait_for"
    description = (
        "Wait for text or a CSS selector to appear on the page. "
        "Much better than blind helpers.sleep() — polls efficiently until the condition is met. "
        "Provide either 'text' or 'selector' (not both). FREE — no screenshot cost."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        text: str | None = None,
        selector: str | None = None,
        timeout: int | None = None,
        **kw: Any,
    ) -> str:
        if not text and not selector:
            return "Error: provide either 'text' or 'selector' parameter."

        timeout_s = timeout or 10
        label = f'text="{text}"' if text else f'selector="{selector}"'
        print(f"\n>> browser_wait_for({label}, timeout={timeout_s}s)")

        if text:
            script = f"""
                const deadline = Date.now() + {timeout_s * 1000};
                while (Date.now() < deadline) {{
                    if (document.body.innerText.includes({json.dumps(text)})) {{
                        return {{found: true, title: document.title, url: location.href}};
                    }}
                    await new Promise(r => setTimeout(r, 500));
                }}
                return {{found: false, title: document.title, url: location.href, bodyPreview: document.body.innerText.substring(0, 200)}};
            """
        else:
            script = f"""
                const deadline = Date.now() + {timeout_s * 1000};
                while (Date.now() < deadline) {{
                    if (document.querySelector({json.dumps(selector)})) {{
                        return {{found: true, title: document.title, url: location.href}};
                    }}
                    await new Promise(r => setTimeout(r, 500));
                }}
                return {{found: false, title: document.title, url: location.href, bodyPreview: document.body.innerText.substring(0, 200)}};
            """

        client_timeout = max(30.0, timeout_s + 10)
        async with httpx.AsyncClient(timeout=client_timeout) as client:
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/script",
                json={"code": script, "timeout": timeout_s * 1000 + 5000},
            )
            r.raise_for_status()
            data = r.json()

        if not data.get("success"):
            self.s.log_activity(f"wait_for({label})", f"script error: {data.get('error', '?')[:60]}")
            return f"Wait failed (script error): {data.get('error', 'unknown')}"

        result = data.get("result", {})
        if result.get("found"):
            self.s.log_activity(f"wait_for({label})", "found")
            # Fetch updated elements
            elements = await _fetch_elements(session_id, self.s)
            response = f"Found! Page: {result.get('url', '?')} | Title: {result.get('title', '?')}"
            if elements:
                response += f"\n\nInteractive elements:\n{elements}"
            return response
        else:
            self.s.log_activity(f"wait_for({label})", f"timeout after {timeout_s}s")
            return (
                f"Not found after {timeout_s}s. "
                f"Page: {result.get('url', '?')} | Title: {result.get('title', '?')}\n"
                f"Page preview: {result.get('bodyPreview', 'N/A')}"
            )


@tool_parameters(
    tool_parameters_schema(session_id=StringSchema("Session ID"), required=["session_id"])
)
class BrowserGetMarkdownTool(Tool):
    name = "browser_get_markdown"
    description = "Extract page content as markdown. FREE — no screenshot cost."

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, **kw: Any) -> str:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"{SUPERBROWSER_URL}/session/{session_id}/markdown")
            r.raise_for_status()
            data = r.json()
        return data.get("content", "No content extracted")[:10000]


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        accept=BooleanSchema(description="Accept (true) or dismiss (false)"),
        text=StringSchema("Text for prompt dialogs", nullable=True),
        required=["session_id", "accept"],
    )
)
class BrowserDialogTool(Tool):
    name = "browser_dialog"
    description = "Accept or dismiss a pending JavaScript dialog."

    async def execute(self, session_id: str, accept: bool, text: str | None = None, **kw: Any) -> str:
        payload: dict[str, Any] = {"accept": accept}
        if text:
            payload["text"] = text
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(f"{SUPERBROWSER_URL}/session/{session_id}/dialog", json=payload)
            r.raise_for_status()
        return f"Dialog {'accepted' if accept else 'dismissed'}"


@tool_parameters(
    tool_parameters_schema(session_id=StringSchema("Session ID"), required=["session_id"])
)
class BrowserCloseTool(Tool):
    name = "browser_close"
    description = "Close the browser session and free resources. Always close when done."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, session_id: str, **kw: Any) -> str:
        print(f"\n>> browser_close({session_id})")
        self.s.log_activity(f"close({session_id})")
        self.s.print_summary()
        self.s.export_activity_log()
        self.s.export_step_history()
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.delete(f"{SUPERBROWSER_URL}/session/{session_id}")
            r.raise_for_status()
        used = self.s.max_screenshots - self.s.screenshot_budget
        return f"Session closed. Vision: {self.s.vision_calls}, Text: {self.s.text_calls}, Screenshots: {used}/{self.s.max_screenshots}, Regressions: {self.s.regression_count}"


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        startX=NumberSchema("Start X coordinate"),
        startY=NumberSchema("Start Y coordinate"),
        endX=NumberSchema("End X coordinate"),
        endY=NumberSchema("End Y coordinate"),
        steps=IntegerSchema("Number of intermediate steps (default 25, higher = smoother)", nullable=True),
        required=["session_id", "startX", "startY", "endX", "endY"],
    )
)
class BrowserDragTool(Tool):
    name = "browser_drag"
    description = "Drag from (startX, startY) to (endX, endY). Useful for slider CAPTCHAs and drag-to-verify puzzles."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, session_id: str, startX: float, startY: float, endX: float, endY: float, steps: int | None = None, **kw: Any) -> str:
        print(f"\n>> browser_drag(({startX},{startY}) -> ({endX},{endY}))")
        self.s.actions_since_screenshot += 1
        self.s.consecutive_click_calls = 0

        payload: dict[str, Any] = {
            "startX": startX, "startY": startY,
            "endX": endX, "endY": endY,
        }
        if steps is not None:
            payload["steps"] = steps

        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(f"{SUPERBROWSER_URL}/session/{session_id}/drag", json=payload)
            r.raise_for_status()
            data = r.json()

        self.s.record_step("browser_drag", f"({startX},{startY})->({endX},{endY})", data.get("url", ""))
        caption = f"Dragged from ({startX},{startY}) to ({endX},{endY})"
        if data.get("elements"):
            caption += f"\n{data['elements']}"
        return caption


@tool_parameters(
    tool_parameters_schema(session_id=StringSchema("Session ID"), required=["session_id"])
)
class BrowserDetectCaptchaTool(Tool):
    name = "browser_detect_captcha"
    description = "Check if the page has a captcha."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, **kw: Any) -> str:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{SUPERBROWSER_URL}/session/{session_id}/captcha/detect")
            r.raise_for_status()
            data = r.json()
        captcha = data.get("captcha")
        if not captcha:
            return "No captcha detected."
        # Detecting a captcha triggers captcha_mode: screenshot dedup +
        # "no-actions-since-last-shot" rules are relaxed so the solver can
        # take repeated before/after screenshots.
        self.s.enter_captcha_mode()
        # Surface the live-view URL the moment a captcha is detected, not
        # only when handoff fires. Gives the LLM (and any UI piping tool
        # output to a human) a link to offer immediately.
        view_url = data.get("viewUrl") or (
            f"{os.environ['SUPERBROWSER_PUBLIC_HOST'].rstrip('/')}"
            f"/session/{session_id}/view"
            if os.environ.get("SUPERBROWSER_PUBLIC_HOST")
            else None
        )
        lines = [
            f"Captcha detected: type={captcha['type']}, "
            f"siteKey={captcha.get('siteKey', 'N/A')} "
            f"(captcha_mode active for next {self.s.CAPTCHA_MODE_ITERATIONS} iterations)",
        ]
        if view_url:
            lines.append(
                f"Live view for human handoff: {view_url} "
                f"(open this URL if you decide to hand off to the user)"
            )
        return "\n".join(lines)


@tool_parameters(
    tool_parameters_schema(session_id=StringSchema("Session ID"), required=["session_id"])
)
class BrowserCaptchaScreenshotTool(Tool):
    name = "browser_captcha_screenshot"
    description = "Take a close-up screenshot of the captcha area."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, **kw: Any) -> Any:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{SUPERBROWSER_URL}/session/{session_id}/captcha/screenshot")
            if r.status_code == 404:
                return "No captcha area found."
            r.raise_for_status()
        b64 = base64.b64encode(r.content).decode()
        return await self.s.build_tool_result_blocks(
            b64,
            "Captcha area — analyze to solve",
            intent="solve captcha — locate widget + tiles + handles",
            url=self.s.current_url,
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        method=StringSchema("Solving method: 'auto', 'token', 'ai_vision', 'grid'", nullable=True),
        provider=StringSchema("Captcha solver: '2captcha' or 'anticaptcha'", nullable=True),
        api_key=StringSchema("API key for solver service", nullable=True),
        required=["session_id"],
    )
)
class BrowserSolveCaptchaTool(Tool):
    name = "browser_solve_captcha"
    description = "Solve a detected captcha automatically."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, session_id: str, method: str | None = None, provider: str | None = None, api_key: str | None = None, **kw: Any) -> str:
        # Vision-based solve path. Replaces the three deleted TS vision
        # strategies (recaptcha-ai-grid, slider-drag, generic-vision) with
        # a single call to our dedicated Python vision agent. When the
        # brain (or fast-to-human policy) picks method='vision' we run the
        # full solve loop here and return a structured result without ever
        # hitting the server's captcha/solve endpoint.
        if method == "vision":
            return await self._solve_via_vision(session_id)

        payload: dict[str, Any] = {}
        if method:
            payload["method"] = method
        if provider:
            payload["provider"] = provider
        if api_key:
            payload["apiKey"] = api_key
        # Advance the solve round BEFORE the call so the next screenshot
        # (which the LLM will take to inspect the result) gets a fresh dedup
        # allowance distinct from the pre-solve shots.
        self.s.captcha_solve_round += 1
        async with httpx.AsyncClient(timeout=180.0) as client:
            r = await client.post(f"{SUPERBROWSER_URL}/session/{session_id}/captcha/solve", json=payload)
            r.raise_for_status()
            data = r.json()

        # Build a structured result the orchestrator can parse — keeps the
        # method + subMethod + vendor + trace so per-domain captcha learnings
        # can be written automatically. The LLM sees JSON; a human-readable
        # summary is injected at the top for quick scanning.
        summary: str
        if data.get("solved"):
            summary = (
                f"Captcha SOLVED via {data.get('method', '?')}"
                f"/{data.get('subMethod', '?')} in {data.get('durationMs', 0)}ms "
                f"({data.get('attempts', 1)} attempt(s))"
            )
        else:
            summary = f"Captcha NOT solved: {data.get('error', 'all methods failed')}"

        # Record the structured method info so orchestrator can learn from it.
        structured = {
            "solved": bool(data.get("solved")),
            "captchaType": data.get("captchaType") or data.get("captcha", {}).get("type"),
            "vendorDetected": data.get("vendorDetected"),
            "method": data.get("method"),
            "subMethod": data.get("subMethod"),
            "attempts": data.get("attempts"),
            "totalRounds": data.get("totalRounds"),
            "durationMs": data.get("durationMs"),
            "siteKey": data.get("siteKey"),
            "iframeUrl": data.get("iframeUrl"),
            "error": data.get("error"),
        }
        # Drop None values so the JSON stays compact.
        structured = {k: v for k, v in structured.items() if v is not None}

        self.s.record_step(
            "browser_solve_captcha",
            method or "auto",
            f"{summary} | {json.dumps(structured, default=str)[:300]}",
        )

        # The solve freed us from captcha — end captcha_mode so the normal
        # budget rules resume.
        if data.get("solved"):
            self.s.captcha_mode = False
            self.s.captcha_mode_remaining = 0
            return f"{summary}\n\nResult JSON:\n{json.dumps(structured, indent=2, default=str)}"

        # --- Auto-escalation to human handoff (deterministic) -------------
        # When auto-solve fails AND human handoff is enabled, immediately
        # POST to the human-input endpoint instead of returning to the LLM
        # and hoping it calls browser_ask_user. This removes the LLM
        # decision loop from the critical captcha→human path.
        if self.s.human_handoff_enabled and self.s.human_handoff_budget > 0:
            print(f"   [auto-escalation] captcha auto-solve failed, requesting human handoff")
            self.s.human_handoff_budget -= 1
            try:
                handoff_timeout = int(os.environ.get("SUPERBROWSER_HANDOFF_TIMEOUT_MS", "180000")) / 1000
                async with httpx.AsyncClient(timeout=handoff_timeout + 10) as hclient:
                    hr = await hclient.post(
                        f"{SUPERBROWSER_URL}/session/{session_id}/human-input/ask",
                        json={
                            "type": "captcha",
                            "message": (
                                "Auto-solve failed for captcha. Please open the live view URL "
                                "and click through the challenge — the agent will detect when "
                                "it clears and resume automatically."
                            ),
                        },
                    )
                    if hr.status_code == 200:
                        hdata = hr.json()
                        if hdata.get("cancelled") or hdata.get("timeout"):
                            human_result = "Human handoff timed out or was cancelled."
                        else:
                            human_result = "Human solved the captcha. Resuming task."
                            self.s.captcha_mode = False
                            self.s.captcha_mode_remaining = 0
                    else:
                        human_result = f"Human handoff request failed (HTTP {hr.status_code})."
            except Exception as exc:
                human_result = f"Human handoff request error: {exc}"

            self.s.record_step(
                "browser_solve_captcha",
                "auto_escalation",
                human_result,
            )
            return (
                f"{summary}\n\n"
                f"[AUTO-ESCALATION] {human_result}\n\n"
                f"Result JSON:\n{json.dumps(structured, indent=2, default=str)}"
            )

        return f"{summary}\n\nResult JSON:\n{json.dumps(structured, indent=2, default=str)}"

    async def _solve_via_vision(self, session_id: str) -> str:
        """Python-side captcha solver driven entirely by the vision agent.

        Flow:
            1. Grab a screenshot + DOM state.
            2. Ask the vision agent for captcha widget / tiles / handles.
            3. Click each captcha_tile. For slider_handle, drag to the
               right edge of the widget.
            4. Re-screenshot and re-check flags.captcha_present. If still
               present after one pass, report NOT solved (fast-to-human
               policy will hand off).
        """
        try:
            from vision_agent import get_vision_agent, vision_agent_enabled
        except ImportError:
            return (
                "Captcha NOT solved: vision_agent package not importable. "
                "Install / configure the vision agent or use method='auto'."
            )
        if not vision_agent_enabled():
            return (
                "Captcha NOT solved: method='vision' requires VISION_ENABLED=1. "
                "Set the env flag and a VISION_API_KEY, or use method='auto'."
            )

        self.s.captcha_solve_round += 1

        # Fetch screenshot + elements.
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{SUPERBROWSER_URL}/session/{session_id}/state",
                params={"vision": "true"},
            )
            r.raise_for_status()
            state = r.json()

        b64 = state.get("screenshot")
        if not b64:
            return "Captcha NOT solved: could not fetch screenshot from server."

        agent = get_vision_agent()
        try:
            resp = await agent.analyze(
                screenshot_b64=b64,
                intent="solve captcha — identify widget, tiles, and handles",
                session_id=session_id,
                url=state.get("url", self.s.current_url),
                dom_hash="",  # force cache miss for this solve attempt
            )
        except Exception as exc:
            return f"Captcha NOT solved: vision call failed ({exc})."

        if not resp.flags.captcha_present and not resp.flags.captcha_widget_bbox:
            # Vision didn't see a captcha — maybe already cleared.
            self.s.captcha_mode = False
            self.s.captcha_mode_remaining = 0
            return (
                "Captcha SOLVED via vision/no_widget_visible — the vision "
                "agent reports no captcha present on the page. If you "
                "disagree, take a fresh screenshot and retry."
            )

        tiles = [b for b in resp.bboxes if b.role == "captcha_tile"]
        handles = [b for b in resp.bboxes if b.role == "slider_handle"]
        widget = resp.flags.captcha_widget_bbox

        actions: list[str] = []

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Click each tile the vision agent flagged.
            for tile in tiles:
                cx, cy = tile.center()
                try:
                    await client.post(
                        f"{SUPERBROWSER_URL}/session/{session_id}/click",
                        json={"x": cx, "y": cy},
                    )
                    actions.append(f"click tile {tile.label!r} @({cx},{cy})")
                    await asyncio.sleep(0.25)
                except Exception as exc:
                    actions.append(f"click tile failed: {exc}")

            # Drag any slider handles to the right edge of their widget
            # (or the handle bbox + 250px as a fallback).
            for handle in handles:
                sx, sy = handle.center()
                if widget is not None:
                    ex = widget.x + max(widget.w - 12, 1)
                    ey = sy
                else:
                    ex, ey = sx + 250, sy
                try:
                    await client.post(
                        f"{SUPERBROWSER_URL}/session/{session_id}/drag",
                        json={"startX": sx, "startY": sy, "endX": ex, "endY": ey, "steps": 30},
                    )
                    actions.append(f"drag slider {handle.label!r} {sx},{sy} → {ex},{ey}")
                    await asyncio.sleep(0.5)
                except Exception as exc:
                    actions.append(f"drag slider failed: {exc}")

            # If we didn't find either, try clicking the widget center —
            # reCAPTCHA "I'm not a robot" checkbox falls through here.
            if not tiles and not handles and widget is not None:
                cx, cy = widget.center()
                try:
                    await client.post(
                        f"{SUPERBROWSER_URL}/session/{session_id}/click",
                        json={"x": cx, "y": cy},
                    )
                    actions.append(f"click widget center @({cx},{cy})")
                    await asyncio.sleep(1.0)
                except Exception as exc:
                    actions.append(f"click widget failed: {exc}")

            # Verify.
            try:
                vr = await client.get(
                    f"{SUPERBROWSER_URL}/session/{session_id}/state",
                    params={"vision": "true"},
                )
                vr.raise_for_status()
                verify_state = vr.json()
            except Exception as exc:
                verify_state = {"screenshot": None, "error": str(exc)}

        verify_b64 = verify_state.get("screenshot")
        solved = False
        if verify_b64:
            try:
                vresp = await agent.analyze(
                    screenshot_b64=verify_b64,
                    intent="verify captcha cleared",
                    session_id=session_id,
                    url=verify_state.get("url", self.s.current_url),
                    dom_hash="",
                )
                solved = not vresp.flags.captcha_present
            except Exception:
                solved = False

        structured = {
            "solved": solved,
            "method": "vision",
            "subMethod": "python_vision_agent",
            "attempts": 1,
            "tiles_clicked": len(tiles),
            "handles_dragged": len(handles),
            "actions": actions,
            "provider": resp.provider,
            "model": resp.model,
        }
        self.s.record_step(
            "browser_solve_captcha",
            "vision",
            f"solved={solved} | {json.dumps(structured, default=str)[:300]}",
        )

        if solved:
            self.s.captcha_mode = False
            self.s.captcha_mode_remaining = 0
            return (
                f"Captcha SOLVED via vision/python_vision_agent "
                f"({len(tiles)} tile(s), {len(handles)} handle(s))"
                f"\n\nResult JSON:\n{json.dumps(structured, indent=2, default=str)}"
            )
        return (
            f"Captcha NOT solved after one vision-driven attempt. "
            f"Per fast-to-human policy, call browser_ask_user next."
            f"\n\nResult JSON:\n{json.dumps(structured, indent=2, default=str)}"
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        question=StringSchema("What to ask the user"),
        input_type=StringSchema("Type: credentials, captcha, confirmation, otp, text, choice", nullable=True),
        required=["session_id", "question"],
    )
)
class BrowserAskUserTool(Tool):
    name = "browser_ask_user"
    description = (
        "Ask the user a question and BLOCK until they respond "
        "(up to 5 minutes). Use for credentials, OTP, confirmation, or "
        "when you need a human decision. The user replies via the remote "
        "view UI at /session/<id>/view or any HTTP client. Returns the "
        "user's reply as a string; on timeout returns a sentinel message "
        "you can react to."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        question: str,
        input_type: str | None = None,
        **kw: Any,
    ) -> Any:
        # Map nanobot-side hint to the TS server's HumanInputType. Default
        # 'text' is the safest — it accepts free-form replies and the UI's
        # "Done" button also works against it.
        valid_types = {
            "credentials", "captcha", "confirmation", "otp", "card", "text", "choice",
        }
        ht = (input_type or "text").lower()
        if ht not in valid_types:
            ht = "text"

        # Capture a screenshot to include in the request payload so any UI
        # listener (not just the live-view poller) can show what page the
        # agent is stuck on. Best-effort.
        screenshot_b64 = None
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                sr = await client.get(
                    f"{SUPERBROWSER_URL}/session/{session_id}/state",
                    params={"vision": "true"},
                )
                sr.raise_for_status()
                sdata = sr.json()
            screenshot_b64 = sdata.get("screenshot") or None
        except Exception:
            screenshot_b64 = None

        # View URL for the user — the concrete surface where they interact.
        public_host = os.environ.get(
            "SUPERBROWSER_PUBLIC_HOST", SUPERBROWSER_URL.rstrip("/"),
        )
        view_url = f"{public_host}/session/{session_id}/view"
        message = (
            f"{question}\n\n"
            f"To respond: open {view_url} in your browser. "
            f"Either interact with the page (for captchas) or click the "
            f"'Done' button when finished."
        )

        # Five-minute timeout matches HumanInputManager's default; the TS
        # server holds the HTTP connection open until the user replies or
        # the timer fires, so client-side we just wait.
        timeout_ms = 5 * 60 * 1000
        self.s.record_step(
            "browser_ask_user",
            f"type={ht}",
            f"view_url={view_url}",
        )
        try:
            async with httpx.AsyncClient(timeout=timeout_ms / 1000 + 10) as client:
                r = await client.post(
                    f"{SUPERBROWSER_URL}/session/{session_id}/human-input/ask",
                    json={
                        "type": ht,
                        "message": message,
                        "screenshot": screenshot_b64,
                        "timeout": timeout_ms,
                    },
                )
                r.raise_for_status()
                data = r.json()
        except Exception as exc:
            return (
                f"[browser_ask_user error: {exc}. "
                f"User was not asked. Continue without their input "
                f"or call again.]"
            )

        if data.get("timedOut"):
            return (
                f"[User did not respond within {timeout_ms // 60000} minutes. "
                f"Proceed without their input or call done(success=False).]"
            )

        response = data.get("response") or {}
        if response.get("cancelled"):
            return "[User cancelled the request. Proceed accordingly.]"

        payload = response.get("data") or {}
        if not payload:
            return "[User responded but provided no data.]"

        # Flatten the reply dict into a short readable string for the model.
        parts = [f"{k}: {v}" for k, v in payload.items()]
        return f"[User replied] {' | '.join(parts)}"


# ── Resumption-handoff helpers ───────────────────────────────────────────
# When a worker exits (stuck, captcha-blocked, or after browser_request_help),
# we save enough tactical state that the NEXT worker can resume on the same
# live Puppeteer session with knowledge of what already failed — instead of
# spawning a fresh session from the home page.
#
# File: /tmp/superbrowser/resumption.json
# Expiry: 5 minutes (RESUMPTION_TTL_SEC). Past that, the Puppeteer session
# has likely been GC'd server-side so liveness is doubtful regardless.

RESUMPTION_PATH = "/tmp/superbrowser/resumption.json"
RESUMPTION_TTL_SEC = 300


def _extract_recent_failures(step_history: list[dict], limit: int = 5) -> list[dict]:
    """Pull the most recent tool steps that look like failures.

    With Priority 1 in place, click/type results include phrases like
    '(element_covered):' or '(stale_selector):' when the structured reason
    is set. We match on those plus generic error markers.
    """
    out: list[dict] = []
    markers = ("FAILED", "failed (", "error:", "Script error", "ERROR:", "NOT solved")
    for step in reversed(step_history):
        result = str(step.get("result") or "")
        if any(m in result for m in markers):
            out.append({
                "tool": step.get("tool", ""),
                "args": str(step.get("args", ""))[:160],
                "result_excerpt": result[:220],
                "url": step.get("url", ""),
                "time": step.get("time", ""),
            })
        if len(out) >= limit:
            break
    return list(reversed(out))


def save_resumption_artifact(
    state: "BrowserSessionState",
    domain: str,
    help_reason: str = "",
    help_failed_tactics: str = "",
) -> bool:
    """Write a resumption hint so the next delegation can pick up where we left off.

    Returns True if the artifact was written. Never raises.
    """
    try:
        if not state.session_id or not state.current_url:
            return False
        payload = {
            "session_id": state.session_id,
            "current_url": state.current_url,
            "best_checkpoint_url": state.best_checkpoint_url,
            "domain": domain,
            "task_id": state.task_id,
            "recent_failures": _extract_recent_failures(state.step_history),
            "help_reason": help_reason or "",
            "help_failed_tactics": help_failed_tactics or "",
            "written_at": time.time(),
        }
        os.makedirs(os.path.dirname(RESUMPTION_PATH), exist_ok=True)
        with open(RESUMPTION_PATH, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"  [resumption artifact saved: session={state.session_id} url={state.current_url}]")
        return True
    except OSError as exc:
        print(f"  [resumption save failed: {exc}]")
        return False


async def load_resumption_artifact(domain: str) -> dict | None:
    """Read and validate a resumption artifact for the given domain.

    Returns None if the artifact is missing, expired, from a different
    domain, or the referenced Puppeteer session is no longer alive
    on the TS server.
    """
    if not os.path.exists(RESUMPTION_PATH):
        return None
    try:
        with open(RESUMPTION_PATH) as f:
            payload = json.load(f)
    except (ValueError, OSError):
        return None

    age = time.time() - float(payload.get("written_at", 0) or 0)
    if age > RESUMPTION_TTL_SEC:
        try:
            os.remove(RESUMPTION_PATH)
        except OSError:
            pass
        return None
    if payload.get("domain") != domain:
        return None

    sid = payload.get("session_id")
    if not sid:
        return None

    # Cheap liveness probe — hit the TS server's session state endpoint.
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                f"{SUPERBROWSER_URL}/session/{sid}/state",
                params={"vision": "false"},
            )
        if r.status_code != 200:
            try:
                os.remove(RESUMPTION_PATH)
            except OSError:
                pass
            return None
    except Exception:
        return None

    return payload


def clear_resumption_artifact() -> None:
    """Remove the resumption artifact (call when a new session successfully supersedes it)."""
    if os.path.exists(RESUMPTION_PATH):
        try:
            os.remove(RESUMPTION_PATH)
        except OSError:
            pass


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        claim=StringSchema(
            "The exact factual claim you're about to report. Include the value, unit, "
            "and what it refers to. E.g. 'Total price for 2 nights at Agoda Grand Sylhet "
            "May 3-5 is BDT 14,500' or '5-star hotel count in Sylhet on GoZayaan is 3'."
        ),
        required=["session_id", "claim"],
    )
)
class BrowserVerifyFactTool(Tool):
    """Visual sanity check before reporting an extracted value.

    Takes a fresh screenshot and frames a narrow verification question for
    the next model turn. The LLM must look at the actual page and say whether
    it supports the claim. Catches the common failure mode where an extraction
    script returned null/wrong-element and the model filled in a plausible
    number downstream.

    Intentionally bypasses normal dedup — verification screenshots are a
    deliberate, infrequent request and must see the current state.
    """

    name = "browser_verify_fact"
    description = (
        "Visually verify a factual claim against the current page before reporting it. "
        "Call this with the EXACT value you're about to return. Then look at the "
        "returned screenshot and answer honestly: does the page actually show this? "
        "If not, do NOT report the original value — go back and fix your extraction."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, session_id: str, claim: str, **kw: Any) -> Any:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{SUPERBROWSER_URL}/session/{session_id}/state",
                params={"vision": "true"},
            )
            r.raise_for_status()
            data = r.json()

        self.s.record_step("browser_verify_fact", claim[:80], "screenshot taken for verification")

        caption = (
            f"[VERIFY CLAIM]\n"
            f"Claim under review: {claim}\n\n"
            f"Look at the screenshot below carefully. In your NEXT reply, respond ONLY "
            f"with a JSON object of the form:\n"
            f'  {{"supported": <bool>, "observed_value": "<what you actually see on the page, '
            f"verbatim, or null if absent>\", "
            f'"reason": "<one sentence explaining what you saw>"}}\n\n'
            f"Rules:\n"
            f"- supported=true only if the claim matches what's visible on the page exactly "
            f"(values, units, context). A crossed-out price is NOT the current price.\n"
            f"- If the page shows a DIFFERENT value than the claim, set supported=false "
            f"and put the real value in observed_value.\n"
            f"- If the page doesn't show enough to tell, set supported=false with a "
            f"'cannot verify' reason — do NOT rubber-stamp.\n"
            f"- After this verify, if supported=false, FIX your extraction and retry. "
            f"If supported=true, report the claim as your final answer."
        )

        if data.get("screenshot"):
            # Don't let verify-fact screenshots eat the captcha cap (they're
            # not for captcha) nor trigger normal dedup (verification must
            # see the live page state). Route through the same async
            # vision-preprocessor hook every other screenshot tool uses, so
            # the brain never sees the raw image when VISION_ENABLED=1.
            self.s.vision_calls += 1
            return await self.s.build_tool_result_blocks(
                data["screenshot"],
                caption,
                intent="verify fact against page",
                url=data.get("url", self.s.current_url),
                elements=data.get("elements"),
            )
        # No screenshot available — still return the caption so the caller
        # can at least reason about the textual state.
        return caption + "\n\n[No screenshot available — verify against browser_get_markdown output.]"


@tool_parameters(
    tool_parameters_schema(
        reason=StringSchema(
            "Why you're stuck. Be specific: 'element_covered by cookie banner I can't dismiss', "
            "'captcha solve failed 3 times', 'selector index keeps shifting'."
        ),
        failed_tactics=StringSchema(
            "Comma-separated list of tactics you already tried. E.g., "
            "'click [5] twice, scroll-and-retry, switch to XPath selector'."
        ),
        required=["reason", "failed_tactics"],
    )
)
class BrowserRequestHelpTool(Tool):
    """Escape hatch: worker signals 'I'm stuck' with structured context.

    Writes a resumption artifact so the orchestrator can spin up a new
    worker that RESUMES on the same live Puppeteer session with a
    different tactic — instead of starting from scratch.

    The worker should call `done(success=False, final_answer=...)` on the
    next turn after calling this tool.
    """

    name = "browser_request_help"
    description = (
        "Call this when you're stuck and a fresh tactic is needed. "
        "Writes structured state so the orchestrator can delegate a "
        "SUCCESSOR worker that resumes on the SAME live browser session "
        "with knowledge of what failed. "
        "After calling this tool, call done(success=False) with a short "
        "explanation — do NOT keep trying the same tactics."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, reason: str, failed_tactics: str, **kw: Any) -> str:
        # Lazy-import to avoid circular imports with the orchestrator module.
        from superbrowser_bridge.routing import _domain_from_url
        domain = _domain_from_url(self.s.current_url) if self.s.current_url else ""
        saved = save_resumption_artifact(
            self.s, domain,
            help_reason=reason,
            help_failed_tactics=failed_tactics,
        )
        self.s.record_step(
            "browser_request_help",
            reason[:80],
            f"saved={saved} session={self.s.session_id}",
        )
        hint = (
            "[HELP REQUESTED] Resumption state saved. "
            "Now call done(success=False, final_answer='Need different tactic: ...') "
            "with a ≤30-word summary. "
            "The orchestrator will delegate a fresh worker that resumes on this "
            "same browser session with your failed tactics excluded."
        ) if saved else (
            "[HELP REQUEST NOT SAVED] session_id or current_url is empty — "
            "resumption artifact could not be written. Proceed with done(success=False) "
            "and explain the blocker in final_answer."
        )
        return hint


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
        BrowserTypeTool(state),
        BrowserKeysTool(state),
        BrowserScrollTool(state),
        BrowserSelectTool(state),
        BrowserEvalTool(state),
        BrowserRunScriptTool(state),
        BrowserWaitForTool(state),
        BrowserDragTool(state),
        BrowserGetMarkdownTool(),        # stateless
        BrowserDialogTool(),             # stateless
        BrowserDetectCaptchaTool(state),
        BrowserCaptchaScreenshotTool(state),
        BrowserSolveCaptchaTool(state),
        BrowserAskUserTool(state),
        BrowserVerifyFactTool(state),
        BrowserRequestHelpTool(state),
        BrowserCloseTool(state),
    ]
    for tool in tools:
        bot._loop.tools.register(tool)
    return state
