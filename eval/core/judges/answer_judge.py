"""Text-only final-answer judge (secondary evaluator).

Ported unchanged in spirit from the legacy ``eval/oracles.py``: a FIXED judge
model reads the task, an optional rubric and the agent's final answer and
returns success/failure. It cannot see the page, so it over-trusts confident
prose; that is why it is secondary to WebJudge and why E9 validates both.

Env: SUPERBROWSER_EVAL_JUDGE_MODEL (default gpt-5.5, legacy value),
SUPERBROWSER_EVAL_JUDGE_API_KEY / _BASE_URL.
"""
from __future__ import annotations

from typing import Any

from .base import Verdict, aclose_client, add_usage, parse_json_verdict, resolve_client, usage_of

DEFAULT_MODEL = "gpt-5.5"

SYSTEM_PROMPT = (
    "You are a strict evaluator of a web-navigation agent. Given a TASK, an "
    "optional SUCCESS RUBRIC, and the agent's FINAL ANSWER, decide whether the "
    "task was genuinely accomplished. Be skeptical of fabricated or vague "
    "answers: an answer that lacks the concrete data the task asked for is a "
    "FAILURE. Reply with ONLY a JSON object: "
    '{"success": true|false, "rationale": "<one sentence>"}.'
)

FAILURE_MARKERS = ("browser worker failed", "captcha_unsolved", "network_blocked", "worker_no_tool_calls")
# A provider refusal is NOT a task failure. If these are not detected, a sweep
# whose key runs dry mid-way records every remaining run as a real failure and
# the analysis reports a success rate built entirely on billing errors.
API_ERROR_MARKERS = ("exceeded your current quota", "insufficient_quota", "check your plan and billing",
                     "rate limit", "invalid_api_key", "incorrect api key", "authenticationerror",
                     # nanobot's own provider-rejection wording
                     "the ai provider rejected the request", "out of quota", "account is in arrears",
                     "billing status of your api key", "top up",
                     # other providers
                     "quota exceeded", "credit balance is too low", "insufficient credits",
                     "requires more credits", "fewer max_tokens", "payment required",
                     "error code: 402", "http 402", "you exceeded your current quota",
                     "no endpoints found", "provider returned error")
# NB: bare "402" is deliberately NOT a marker — a page result can contain that number.


def looks_like_api_error(final_answer: str | None) -> bool:
    c = (final_answer or "").strip().lower()
    return c.startswith("error:") or any(m in c for m in API_ERROR_MARKERS)


def heuristic_success(final_answer: str | None) -> bool:
    c = (final_answer or "").strip().lower()
    if not c or looks_like_api_error(final_answer):
        return False
    return not any(m in c for m in FAILURE_MARKERS)


def _parse(txt: str) -> tuple[bool, str]:
    obj = parse_json_verdict(txt)
    if obj is not None:
        return bool(obj.get("success")), str(obj.get("rationale", ""))[:300]
    low = (txt or "").lower()
    if "true" in low and "false" not in low:
        return True, (txt or "")[:300]
    return False, (txt or "")[:300] or "unparseable judge output"


async def judge(task: Any, final_answer: str, *, client: Any = None, model: str | None = None) -> Verdict:
    owned = client is None      # only close a client we built ourselves
    if client is None:
        client, resolved = resolve_client(model_env="SUPERBROWSER_EVAL_JUDGE_MODEL", default_model=DEFAULT_MODEL,
                                          prefix="SUPERBROWSER_EVAL_ANSWER_JUDGE")
        model = model or resolved
    if client is None:
        return Verdict("answer_judge", None, "no judge API key available", model)
    if looks_like_api_error(final_answer):
        if owned:
            await aclose_client(client)
        return Verdict("answer_judge", False, "provider/billing error in place of an answer", model,
                       details={"api_error": True})
    rubric = getattr(task, "reference", None) or "(no explicit rubric — use the task's implied success criteria)"
    user = (f"TASK:\n{task.instruction}\n\nSUCCESS RUBRIC:\n{rubric}\n\n"
            f"AGENT FINAL ANSWER:\n{final_answer or '(empty)'}\n")
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]
    last: Exception | None = None
    for kwargs in ({"temperature": 0}, {}):
        try:
            resp = await client.chat.completions.create(model=model, messages=messages, **kwargs)
            txt = (resp.choices[0].message.content or "").strip()
            ok, rationale = _parse(txt)
            if owned:
                await aclose_client(client)
            return Verdict("answer_judge", ok, rationale, model,
                           details={"heuristic_success": heuristic_success(final_answer)},
                           usage=add_usage({}, usage_of(resp)))
        except Exception as exc:  # noqa: BLE001 - retry without temperature, then give up
            last = exc
    if owned:
        await aclose_client(client)
    return Verdict("answer_judge", None, f"judge error: {last}", model)
