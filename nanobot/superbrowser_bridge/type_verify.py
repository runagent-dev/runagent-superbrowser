"""Post-type auto-verification + surgical typo correction.

After any successful typing tool (`browser_type`, `browser_type_at`,
`browser_fix_text_at`), this module runs a lightweight check against the
task's intent. If the LLM typed `"dhakka"` when the task says `"dhaka"`,
we detect it, and for small edit distances (<= SURGICAL_MAX_DISTANCE)
apply human-like backspace/retype keystrokes at the cursor position
rather than clearing the whole field.

Design notes:
    - Semantic check is a cheap Gemini flash reflection call; gated by a
      local heuristic so most types never hit the LLM at all.
    - The reflector is instructed to only suggest corrections whose token
      already appears in the task prompt — anti-hallucination floor.
    - Surgical edit runs inside ONE `/evaluate` tick (native setter +
      per-op InputEvent). Same trick `_ATOMIC_FIX_TEXT_JS` uses, so
      React's batched state update never sees a half-applied edit.
    - Large diffs fall back to the existing atomic rewrite path.
    - All behaviour is gated by env vars (VERIFY_ENABLED, VERIFY_AUTOAPPLY)
      so operators can disable autofix and fall back to WARN-only mode
      during initial rollout.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .session_tools import BrowserSessionState


# --- Config ------------------------------------------------------------------

SURGICAL_MAX_DISTANCE = 3
_AUTOAPPLY_CONFIDENCE = 0.85
_WARN_CONFIDENCE = 0.60
_DEFAULT_TIMEOUT_MS = 2500

_GEMINI_OPENAI_COMPAT_BASE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/openai"
)

# Label/name keywords that indicate the field holds secrets / non-word tokens;
# skip the reflector for these — English spelling heuristics do not apply.
_SENSITIVE_LABEL_TOKENS = (
    "password", "passwd", "pwd", "otp", "one-time",
    "code", "pin", "cvv", "cvc", "cc-number", "card number",
    "security code", "verification code",
)

# Input types we never try to spellcheck — shape, not spelling, is what matters.
_SKIP_INPUT_TYPES = frozenset({
    "password", "email", "tel", "url", "number", "date", "time",
    "datetime-local", "month", "week", "search",
})

# LRU cap for (session_id, label, typed_text) → outcome. Avoids re-verifying
# when a tool result triggers a confirm-read that re-invokes the hook.
_RECENT_CACHE_CAP = 8
_recent_cache: "OrderedDict[tuple[str, str, str], float]" = OrderedDict()


# --- Outcome shape -----------------------------------------------------------

@dataclass
class VerifyOutcome:
    """Result of a post-type verify pass.

    `kind` drives caller branching:
      - "skipped": heuristic said not worth checking; caption suffix empty
        unless VERIFY_DEBUG is set.
      - "ok": reflector ran and found no typo.
      - "corrected": typo detected and auto-fix applied; caller should
        update its `synthetic_data.after` / set `auto_corrected=True`.
      - "flagged": typo suspected but confidence below autoapply floor;
        WARNING caption emitted, no rewrite.
      - "error": reflector / evaluate failed; caption marks it unavailable
        but the underlying type is NOT rolled back.
    """
    kind: str
    caption_suffix: str = ""
    corrected_to: str | None = None
    before: str = ""
    after: str = ""
    confidence: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


# --- Skip predicate ----------------------------------------------------------

_EMAIL_RE = re.compile(r"^\S+@\S+\.\S+$")
_URL_RE = re.compile(r"^https?://\S+$")
_NUMERIC_ID_RE = re.compile(r"^[0-9 \-]{8,}$")
_LONG_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{20,}")


def skip_predicate(
    *,
    typed_text: str,
    input_type: str = "",
    label: str = "",
    name: str = "",
    autocomplete: str = "",
) -> tuple[bool, str]:
    """Return (should_skip, reason). Cheap string-only work — no I/O."""
    t = (typed_text or "").strip()
    if not t:
        return True, "empty"
    if len(t) < 3:
        return True, "too_short"

    itype = (input_type or "text").lower()
    if itype in _SKIP_INPUT_TYPES:
        return True, f"input_type={itype}"

    blob = f"{label} {name} {autocomplete}".lower()
    for tok in _SENSITIVE_LABEL_TOKENS:
        if tok in blob:
            return True, "sensitive_label"

    if _EMAIL_RE.match(t):
        return True, "email_like"
    if _URL_RE.match(t):
        return True, "url_like"
    if _NUMERIC_ID_RE.match(t):
        return True, "numeric_id"
    if _LONG_TOKEN_RE.search(t):
        return True, "long_token"

    digit_sym = sum(1 for c in t if not c.isalpha() and not c.isspace())
    if digit_sym / len(t) >= 0.30:
        return True, "programmatic"

    return False, ""


# --- Wagner-Fischer edit planner --------------------------------------------

def plan_surgical_edit(
    before: str, target: str, *, max_distance: int = SURGICAL_MAX_DISTANCE,
) -> tuple[int, list[tuple[str, Any]]] | None:
    """Return (distance, ops) if Levenshtein(before, target) <= max_distance.

    ops is a list of ('keep', n) | ('del', n) | ('ins', substr). Substitutions
    are emitted as del+ins at the same cursor — mirrors a human "backspace
    then retype the right char". Returns None if diff is too large.
    """
    m, n = len(before), len(target)
    if abs(m - n) > max_distance:
        return None

    # Early exit: identical prefix + suffix lets us reduce the problem.
    p = 0
    while p < m and p < n and before[p] == target[p]:
        p += 1
    s = 0
    while (s < m - p) and (s < n - p) and before[m - 1 - s] == target[n - 1 - s]:
        s += 1
    a = before[p:m - s]
    b = target[p:n - s]
    if not a and not b:
        return 0, [("keep", m)]

    # Full DP on the shrunken middle only.
    dp: list[list[int]] = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        dp[i][0] = i
    for j in range(len(b) + 1):
        dp[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            dp[i][j] = min(
                dp[i - 1][j] + 1,      # delete from before
                dp[i][j - 1] + 1,      # insert into before
                dp[i - 1][j - 1] + cost,
            )
    distance = dp[len(a)][len(b)]
    if distance > max_distance:
        return None

    # Backtrack into an edit script on the shrunken middle.
    i, j = len(a), len(b)
    mid_ops: list[tuple[str, Any]] = []
    while i > 0 or j > 0:
        if i > 0 and j > 0 and a[i - 1] == b[j - 1]:
            mid_ops.append(("keep", 1))
            i -= 1; j -= 1
        elif i > 0 and j > 0 and dp[i][j] == dp[i - 1][j - 1] + 1:
            # substitute a[i-1] -> b[j-1]  == del 1 + ins b[j-1]
            mid_ops.append(("del", 1))
            mid_ops.append(("ins", b[j - 1]))
            i -= 1; j -= 1
        elif i > 0 and dp[i][j] == dp[i - 1][j] + 1:
            mid_ops.append(("del", 1))
            i -= 1
        else:
            mid_ops.append(("ins", b[j - 1]))
            j -= 1
    mid_ops.reverse()

    # Merge runs of consecutive 'keep' and 'del' numeric ops.
    merged: list[tuple[str, Any]] = []
    for op in mid_ops:
        if merged and merged[-1][0] == op[0] and op[0] in ("keep", "del"):
            merged[-1] = (op[0], merged[-1][1] + op[1])
        elif merged and merged[-1][0] == "ins" and op[0] == "ins":
            merged[-1] = ("ins", merged[-1][1] + op[1])
        else:
            merged.append(op)

    ops: list[tuple[str, Any]] = []
    if p > 0:
        ops.append(("keep", p))
    ops.extend(merged)
    if s > 0:
        ops.append(("keep", s))

    return distance, ops


# --- Surgical-edit JS (one /evaluate tick) ----------------------------------

_SURGICAL_EDIT_JS = r"""
(() => {
  const x = __X__, y = __Y__, ops = __OPS__;
  const el = document.elementFromPoint(x, y);
  if (!el) return {ok: false, reason: 'no_element'};
  const tag = el.tagName.toLowerCase();
  const isInput = tag === 'input' || tag === 'textarea';
  const isEditable = !!el.isContentEditable;
  if (!isInput && !isEditable) return {ok: false, reason: 'not_input', tag};
  const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype
                                   : HTMLInputElement.prototype;
  const desc = isInput ? Object.getOwnPropertyDescriptor(proto, 'value') : null;
  const setVal = (v) => {
    if (isInput) {
      if (desc && desc.set) desc.set.call(el, v);
      else el.value = v;
    } else {
      el.innerText = v;
    }
  };
  try { el.focus(); } catch (_) {}
  let cursor = 0;
  const before = isInput ? (el.value || '') : (el.innerText || '');
  let cur = before;
  try {
    for (const op of ops) {
      const kind = op[0];
      if (kind === 'keep') {
        cursor += op[1];
      } else if (kind === 'del') {
        const n = op[1];
        cur = cur.slice(0, cursor) + cur.slice(cursor + n);
        setVal(cur);
        el.dispatchEvent(new InputEvent('input', {
          bubbles: true, inputType: 'deleteContentForward', data: null,
        }));
      } else if (kind === 'ins') {
        const s = op[1];
        cur = cur.slice(0, cursor) + s + cur.slice(cursor);
        setVal(cur);
        el.dispatchEvent(new InputEvent('input', {
          bubbles: true, inputType: 'insertText', data: s,
        }));
        cursor += s.length;
      }
      if (isInput && el.setSelectionRange) {
        try { el.setSelectionRange(cursor, cursor); } catch (_) {}
      }
    }
    el.dispatchEvent(new Event('change', {bubbles: true}));
  } catch (e) {
    return {ok: false, reason: 'exception', error: String(e).slice(0, 120), before};
  }
  const after = isInput ? (el.value || '') : (el.innerText || '');
  return {ok: true, before, after, changed: before !== after};
})()
"""


# --- Reflector (LLM semantic spellcheck) ------------------------------------

_SYSTEM_PROMPT = (
    "You compare what a user asked for against what was typed into a web "
    "form field and decide if there is a semantic typo. "
    "Reply JSON only. Schema: "
    '{"is_correct": bool, "suggested_correction": string or null, '
    '"confidence": number between 0 and 1, "reason": string}. '
    "Rules: (1) set is_correct=true whenever the typed text matches any "
    "token of the task prompt (case-insensitive) or is a plausible "
    "spelling in the given field context. "
    "(2) only return a suggested_correction when confidence>=0.85 AND the "
    "proposed correction is a word that ALREADY appears in the task "
    "prompt. Never invent a word that the task did not mention. "
    "(3) keep the answer terse — a single sentence in `reason`."
)


def _build_user_prompt(
    *, typed_text: str, task_instruction: str, label: str, page_url: str,
) -> str:
    # Keep it short; flash model, single shot.
    return (
        f"Task prompt: {task_instruction!r}\n"
        f"Field label / context: {label!r} (page {page_url})\n"
        f"Text typed into the field: {typed_text!r}\n"
        "Decide if the typed text is a semantic typo of a word the user "
        "actually asked for. Reply with JSON only."
    )


_client_cache: dict[str, Any] = {}


def _get_llm_client() -> tuple[Any, str] | None:
    """Return (AsyncOpenAI, model) pair or None if disabled / unconfigured."""
    if os.environ.get("VERIFY_ENABLED", "1").strip() in {"0", "false", "False"}:
        return None
    api_key = (os.environ.get("VERIFY_API_KEY")
               or os.environ.get("VISION_API_KEY")
               or "").strip()
    if not api_key:
        return None
    model = (os.environ.get("VERIFY_MODEL")
             or "gemini-2.0-flash-exp").strip()
    base_url = (os.environ.get("VERIFY_BASE_URL")
                or _GEMINI_OPENAI_COMPAT_BASE_URL).strip()
    timeout_s = _verify_timeout_s()
    cache_key = f"{base_url}|{api_key[:8]}|{timeout_s}"
    cached = _client_cache.get(cache_key)
    if cached is not None:
        return cached, model
    try:
        from openai import AsyncOpenAI  # lazy import — keeps module import cheap
    except Exception:
        return None
    client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout_s)
    _client_cache[cache_key] = client
    return client, model


def _verify_timeout_s() -> float:
    raw = os.environ.get("VERIFY_TIMEOUT_MS") or str(_DEFAULT_TIMEOUT_MS)
    try:
        return max(0.5, float(raw) / 1000.0)
    except ValueError:
        return _DEFAULT_TIMEOUT_MS / 1000.0


async def _reflect_typo(
    *, typed_text: str, task_instruction: str, label: str, page_url: str,
) -> dict[str, Any] | None:
    """Ask the reflector LLM whether `typed_text` is a semantic typo.

    Returns the parsed JSON dict (is_correct/suggested_correction/
    confidence/reason) or None if the call failed / was disabled.
    """
    pair = _get_llm_client()
    if pair is None:
        return None
    client, model = pair
    user_prompt = _build_user_prompt(
        typed_text=typed_text,
        task_instruction=task_instruction,
        label=label,
        page_url=page_url,
    )
    try:
        completion = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=120,
                temperature=0,
                response_format={"type": "json_object"},
            ),
            timeout=_verify_timeout_s(),
        )
    except (asyncio.TimeoutError, Exception) as e:
        print(f"  [verify] reflector failed: {type(e).__name__}: {e!s:.80}")
        return None

    try:
        content = completion.choices[0].message.content or "{}"
        parsed = json.loads(content)
    except Exception as e:
        print(f"  [verify] reflector returned invalid JSON: {e}")
        return None
    return parsed if isinstance(parsed, dict) else None


# --- Cache helpers -----------------------------------------------------------

def _cache_hit(session_id: str, label: str, typed_text: str) -> bool:
    key = (session_id, label, typed_text)
    now = time.time()
    # 60s TTL — stale enough to skip the same field being re-verified during
    # one planning cycle, not so long it masks real re-types minutes later.
    hit = _recent_cache.get(key)
    if hit is not None and (now - hit) < 60.0:
        _recent_cache.move_to_end(key)
        return True
    return False


def _cache_store(session_id: str, label: str, typed_text: str) -> None:
    key = (session_id, label, typed_text)
    _recent_cache[key] = time.time()
    _recent_cache.move_to_end(key)
    while len(_recent_cache) > _RECENT_CACHE_CAP:
        _recent_cache.popitem(last=False)


def _task_has_token(task_instruction: str, token: str) -> bool:
    """Case-insensitive substring check — the anti-hallucination floor."""
    if not token or not task_instruction:
        return False
    return token.lower() in task_instruction.lower()


# --- Public helpers ----------------------------------------------------------

async def _run_evaluate(
    session_id: str, script: str, *, timeout: float = 15.0,
) -> dict[str, Any]:
    """POST to the /evaluate endpoint via the existing backoff wrapper.

    Imported late to avoid a module-level cycle with session_tools.
    """
    from .session_tools import SUPERBROWSER_URL, _request_with_backoff
    r = await _request_with_backoff(
        "POST",
        f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
        json={"script": script},
        timeout=timeout,
    )
    r.raise_for_status()
    body = r.json()
    got = body.get("result") if isinstance(body, dict) else None
    return got if isinstance(got, dict) else {}


def _format_ops(ops: list[tuple[str, Any]]) -> str:
    """Human-readable op summary for the caption."""
    cursor = 0
    chunks: list[str] = []
    for op in ops:
        kind = op[0]
        if kind == "keep":
            cursor += op[1]
        elif kind == "del":
            n = op[1]
            chunks.append(f"{n} backspace{'s' if n > 1 else ''} at pos {cursor}")
        elif kind == "ins":
            chunks.append(f"typed {op[1]!r} at pos {cursor}")
            cursor += len(op[1])
    return "; ".join(chunks) if chunks else "no change"


async def _apply_surgical(
    session_id: str, *, target_x: float, target_y: float,
    before: str, target: str,
) -> tuple[bool, str, list[tuple[str, Any]] | None]:
    """Try to apply a surgical edit. Returns (applied, final_after, ops).
    `applied` is False if the diff was too large for surgical mode."""
    plan = plan_surgical_edit(before, target)
    if plan is None:
        return False, before, None
    distance, ops = plan
    if distance == 0:
        return True, target, ops
    js = (
        _SURGICAL_EDIT_JS
        .replace("__X__", str(float(target_x)))
        .replace("__Y__", str(float(target_y)))
        .replace("__OPS__", json.dumps([list(op) for op in ops]))
    )
    result = await _run_evaluate(session_id, js, timeout=15.0)
    if not result.get("ok"):
        return False, before, ops
    return True, str(result.get("after", target) or ""), ops


async def _apply_atomic(
    session_id: str, *, target_x: float, target_y: float, target: str,
) -> tuple[bool, str]:
    """Fall back to the existing atomic rewrite by re-calling the
    `_ATOMIC_FIX_TEXT_JS` script with the corrected target value."""
    from .session_tools import _ATOMIC_FIX_TEXT_JS
    js = (
        _ATOMIC_FIX_TEXT_JS
        .replace("__TARGET_X__", str(float(target_x)))
        .replace("__TARGET_Y__", str(float(target_y)))
        .replace("__TARGET_TEXT__", json.dumps(target))
    )
    result = await _run_evaluate(session_id, js, timeout=20.0)
    if not result.get("ok"):
        return False, ""
    return True, str(result.get("after", "") or "")


async def verify_and_correct(
    state: "BrowserSessionState",
    session_id: str,
    *,
    target_x: float,
    target_y: float,
    typed_text: str,
    label: str,
    page_url: str,
    field_meta: dict[str, Any] | None = None,
) -> VerifyOutcome:
    """Entry point for coord-addressed typing tools (type_at, fix_text_at).

    Follows the pipeline: re-entrance guard → cache check → skip predicate
    → task-substring shortcut → reflector → apply correction. Every branch
    logs to stdout and `state.record_step` so audit trails survive.
    """
    # Re-entrance guard: the inner atomic/surgical rewrite can trigger the
    # same hook from its own caller. If we see the flag set, do nothing.
    if getattr(state, "_verify_in_progress", False):
        return VerifyOutcome(kind="skipped", caption_suffix="")

    debug = os.environ.get("VERIFY_DEBUG", "").strip() in {"1", "true", "True"}
    autoapply = os.environ.get("VERIFY_AUTOAPPLY", "1").strip() not in {"0", "false", "False"}

    field_meta = field_meta or {}
    skip, reason = skip_predicate(
        typed_text=typed_text,
        input_type=str(field_meta.get("input_type", "") or ""),
        label=str(field_meta.get("label", label) or ""),
        name=str(field_meta.get("name", "") or ""),
        autocomplete=str(field_meta.get("autocomplete", "") or ""),
    )
    if skip:
        suffix = f"\n[verify: skipped ({reason})]" if debug else ""
        return VerifyOutcome(kind="skipped", caption_suffix=suffix)

    task_instr = (getattr(state, "task_instruction", "") or "")
    if task_instr and _task_has_token(task_instr, typed_text):
        # LLM typed something already in the task — it's fine.
        return VerifyOutcome(
            kind="ok",
            caption_suffix="\n[verify: ok]",
        )

    if _cache_hit(session_id, label, typed_text):
        return VerifyOutcome(kind="skipped", caption_suffix="")

    print(
        f"  [verify] checking {label} typed={typed_text!r} "
        f"against task={task_instr[:60]!r}"
    )
    parsed = await _reflect_typo(
        typed_text=typed_text,
        task_instruction=task_instr,
        label=str(field_meta.get("label", label) or label),
        page_url=page_url or "",
    )
    _cache_store(session_id, label, typed_text)
    if parsed is None:
        state.record_step("verify", f"{label} typed={typed_text!r}", "unavailable")
        return VerifyOutcome(
            kind="error",
            caption_suffix="\n[verify: unavailable]",
        )

    is_correct = bool(parsed.get("is_correct", True))
    suggestion = parsed.get("suggested_correction")
    try:
        confidence = float(parsed.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    reason_s = str(parsed.get("reason", "") or "")[:160]

    if is_correct or not suggestion or suggestion == typed_text:
        state.record_step("verify", f"{label} typed={typed_text!r}", f"ok ({reason_s})")
        return VerifyOutcome(
            kind="ok",
            caption_suffix="\n[verify: ok]",
            confidence=confidence,
        )

    suggestion = str(suggestion)
    # Anti-hallucination: the correction MUST exist in the task prompt.
    if task_instr and not _task_has_token(task_instr, suggestion):
        state.record_step(
            "verify",
            f"{label} typed={typed_text!r}",
            f"rejected_correction={suggestion!r}",
        )
        print(
            f"  [verify] rejected correction {suggestion!r} — not in task prompt"
        )
        return VerifyOutcome(
            kind="ok",
            caption_suffix="\n[verify: ok]",
            confidence=confidence,
        )

    if confidence < _WARN_CONFIDENCE:
        state.record_step("verify", f"{label} typed={typed_text!r}", "low_conf_skip")
        return VerifyOutcome(kind="ok", caption_suffix="")

    if confidence < _AUTOAPPLY_CONFIDENCE or not autoapply:
        suffix = (
            f'\n[verify: WARNING possible typo "{typed_text}" - '
            f'suggested "{suggestion}" (conf {confidence:.2f}). '
            f'Inspect before submitting.]'
        )
        state.record_step(
            "verify",
            f"{label} typed={typed_text!r}",
            f"flagged_suggest={suggestion!r} conf={confidence:.2f}",
        )
        print(
            f"  [verify] flagged {typed_text!r} -> {suggestion!r} "
            f"(conf {confidence:.2f}, no autofix)"
        )
        return VerifyOutcome(
            kind="flagged",
            caption_suffix=suffix,
            corrected_to=None,
            confidence=confidence,
        )

    # Apply correction. Guard re-entrance first.
    state._verify_in_progress = True  # type: ignore[attr-defined]
    try:
        applied, final_after, ops = await _apply_surgical(
            session_id, target_x=target_x, target_y=target_y,
            before=typed_text, target=suggestion,
        )
        if applied:
            op_summary = _format_ops(ops or [])
            suffix = (
                f'\n[verify: auto-corrected "{typed_text}" -> "{suggestion}"'
                f' ({op_summary}, conf {confidence:.2f})]'
            )
            state.record_step(
                "verify",
                f"{label} typed={typed_text!r}",
                f"corrected_surgical={suggestion!r} ops={op_summary} conf={confidence:.2f}",
            )
            print(
                f"  [verify] corrected {typed_text!r} -> {suggestion!r} "
                f"at {label} (surgical: {op_summary}, conf {confidence:.2f})"
            )
            return VerifyOutcome(
                kind="corrected",
                caption_suffix=suffix,
                corrected_to=suggestion,
                before=typed_text,
                after=final_after or suggestion,
                confidence=confidence,
            )
        # Surgical path didn't fit / failed → atomic rewrite.
        ok, final_after = await _apply_atomic(
            session_id, target_x=target_x, target_y=target_y, target=suggestion,
        )
        if ok:
            suffix = (
                f'\n[verify: auto-corrected "{typed_text}" -> "{suggestion}"'
                f' (atomic rewrite, conf {confidence:.2f})]'
            )
            state.record_step(
                "verify",
                f"{label} typed={typed_text!r}",
                f"corrected_atomic={suggestion!r} conf={confidence:.2f}",
            )
            print(
                f"  [verify] corrected {typed_text!r} -> {suggestion!r} "
                f"at {label} (atomic, conf {confidence:.2f})"
            )
            return VerifyOutcome(
                kind="corrected",
                caption_suffix=suffix,
                corrected_to=suggestion,
                before=typed_text,
                after=final_after or suggestion,
                confidence=confidence,
            )
        state.record_step(
            "verify",
            f"{label} typed={typed_text!r}",
            f"apply_failed suggest={suggestion!r}",
        )
        return VerifyOutcome(
            kind="error",
            caption_suffix="\n[verify: unavailable (apply failed)]",
            confidence=confidence,
        )
    finally:
        state._verify_in_progress = False  # type: ignore[attr-defined]


async def verify_and_correct_by_index(
    state: "BrowserSessionState",
    session_id: str,
    *,
    dom_index: int,
    typed_text: str,
    page_url: str,
    field_meta: dict[str, Any] | None = None,
) -> VerifyOutcome:
    """Index-addressed variant for `browser_type`.

    Index-mode correction uses the server's `/type` endpoint with
    `clear=true` — no coordinate resolution needed. Less human-like than
    the surgical path but simpler and sufficient since most bot-protection
    happens at the keystroke-dynamics layer, not at the final-value layer.
    """
    if getattr(state, "_verify_in_progress", False):
        return VerifyOutcome(kind="skipped", caption_suffix="")

    debug = os.environ.get("VERIFY_DEBUG", "").strip() in {"1", "true", "True"}
    autoapply = os.environ.get("VERIFY_AUTOAPPLY", "1").strip() not in {"0", "false", "False"}

    label = f"[{dom_index}]"
    field_meta = field_meta or {}
    skip, reason = skip_predicate(
        typed_text=typed_text,
        input_type=str(field_meta.get("input_type", "") or ""),
        label=str(field_meta.get("label", "") or ""),
        name=str(field_meta.get("name", "") or ""),
        autocomplete=str(field_meta.get("autocomplete", "") or ""),
    )
    if skip:
        suffix = f"\n[verify: skipped ({reason})]" if debug else ""
        return VerifyOutcome(kind="skipped", caption_suffix=suffix)

    task_instr = (getattr(state, "task_instruction", "") or "")
    if task_instr and _task_has_token(task_instr, typed_text):
        return VerifyOutcome(kind="ok", caption_suffix="\n[verify: ok]")

    if _cache_hit(session_id, label, typed_text):
        return VerifyOutcome(kind="skipped", caption_suffix="")

    print(
        f"  [verify] checking {label} typed={typed_text!r} "
        f"against task={task_instr[:60]!r}"
    )
    parsed = await _reflect_typo(
        typed_text=typed_text,
        task_instruction=task_instr,
        label=str(field_meta.get("label", "") or label),
        page_url=page_url or "",
    )
    _cache_store(session_id, label, typed_text)
    if parsed is None:
        state.record_step("verify", f"{label} typed={typed_text!r}", "unavailable")
        return VerifyOutcome(
            kind="error",
            caption_suffix="\n[verify: unavailable]",
        )
    is_correct = bool(parsed.get("is_correct", True))
    suggestion = parsed.get("suggested_correction")
    try:
        confidence = float(parsed.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0

    if is_correct or not suggestion or suggestion == typed_text:
        state.record_step("verify", f"{label} typed={typed_text!r}", "ok")
        return VerifyOutcome(
            kind="ok",
            caption_suffix="\n[verify: ok]",
            confidence=confidence,
        )
    suggestion = str(suggestion)
    if task_instr and not _task_has_token(task_instr, suggestion):
        state.record_step(
            "verify", f"{label} typed={typed_text!r}",
            f"rejected_correction={suggestion!r}",
        )
        return VerifyOutcome(kind="ok", caption_suffix="\n[verify: ok]",
                             confidence=confidence)

    if confidence < _WARN_CONFIDENCE:
        return VerifyOutcome(kind="ok", caption_suffix="")

    if confidence < _AUTOAPPLY_CONFIDENCE or not autoapply:
        suffix = (
            f'\n[verify: WARNING possible typo "{typed_text}" - '
            f'suggested "{suggestion}" (conf {confidence:.2f}). '
            f'Inspect before submitting.]'
        )
        state.record_step(
            "verify", f"{label} typed={typed_text!r}",
            f"flagged_suggest={suggestion!r} conf={confidence:.2f}",
        )
        return VerifyOutcome(
            kind="flagged", caption_suffix=suffix,
            confidence=confidence,
        )

    # Apply via server /type endpoint with clear=true. Delegate to a helper
    # in session_tools so we don't reimplement the payload shape.
    state._verify_in_progress = True  # type: ignore[attr-defined]
    try:
        from .session_tools import SUPERBROWSER_URL, _request_with_backoff
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/type",
            json={"index": dom_index, "text": suggestion, "clear": True},
            timeout=30.0,
        )
        if r.status_code >= 400:
            state.record_step(
                "verify", f"{label} typed={typed_text!r}",
                f"apply_failed suggest={suggestion!r} status={r.status_code}",
            )
            return VerifyOutcome(
                kind="error",
                caption_suffix="\n[verify: unavailable (apply failed)]",
                confidence=confidence,
            )
        suffix = (
            f'\n[verify: auto-corrected "{typed_text}" -> "{suggestion}"'
            f' (retyped via index, conf {confidence:.2f})]'
        )
        state.record_step(
            "verify", f"{label} typed={typed_text!r}",
            f"corrected_retype={suggestion!r} conf={confidence:.2f}",
        )
        print(
            f"  [verify] corrected {typed_text!r} -> {suggestion!r} "
            f"at {label} (retype, conf {confidence:.2f})"
        )
        return VerifyOutcome(
            kind="corrected",
            caption_suffix=suffix,
            corrected_to=suggestion,
            before=typed_text,
            after=suggestion,
            confidence=confidence,
        )
    finally:
        state._verify_in_progress = False  # type: ignore[attr-defined]
