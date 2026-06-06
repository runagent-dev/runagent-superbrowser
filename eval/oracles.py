"""Success oracles for the eval.

Primary: a FIXED LLM judge (never a candidate worker model) scores the
orchestrator's final answer against the task rubric. Configure via:
  SUPERBROWSER_EVAL_JUDGE_MODEL     (default "gpt-5.5")
  SUPERBROWSER_EVAL_JUDGE_API_KEY   (else OPENAI_API_KEY, else config.json)
  SUPERBROWSER_EVAL_JUDGE_BASE_URL  (else OpenAI default / OpenRouter)

The judge MUST be held fixed across all candidate runs — pin it explicitly so a
candidate never grades itself. Its id is recorded in every judge.json.

Secondary: a cheap heuristic (non-empty, no failure markers) mirroring
delegation.py's own success classification — recorded alongside the judge so
runs are still scored if no judge key is available.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from ._bootstrap import DEFAULT_CONFIG_PATH

_FAILURE_MARKERS = (
    "browser worker failed",
    "captcha_unsolved",
    "network_blocked",
    "worker_no_tool_calls",
)

# Provider/billing/auth errors — the run produced no real task data, not a failure
# of the model's web-navigation. Detected separately so they don't pollute metrics.
_API_ERROR_MARKERS = (
    "exceeded your current quota",
    "insufficient_quota",
    "check your plan and billing",
    "rate limit",
    "invalid_api_key",
    "incorrect api key",
    "authenticationerror",
)


def looks_like_api_error(final_answer: str | None) -> bool:
    """True if the content is an LLM provider error (quota/rate-limit/auth) rather
    than a task answer — i.e. the run never actually executed the task."""
    c = (final_answer or "").strip().lower()
    return c.startswith("error:") or any(m in c for m in _API_ERROR_MARKERS)


def heuristic_success(final_answer: str | None) -> bool:
    """Non-empty, not a task-failure marker, and not a provider/billing error."""
    c = (final_answer or "").strip().lower()
    if not c or looks_like_api_error(final_answer):
        return False
    return not any(m in c for m in _FAILURE_MARKERS)


def _resolve_judge_client():
    """Return (AsyncOpenAI | None, model_id). None client => no key available."""
    from openai import AsyncOpenAI

    model = os.environ.get("SUPERBROWSER_EVAL_JUDGE_MODEL", "gpt-5.5")
    api_key = os.environ.get("SUPERBROWSER_EVAL_JUDGE_API_KEY") or os.environ.get(
        "OPENAI_API_KEY"
    )
    base_url = os.environ.get("SUPERBROWSER_EVAL_JUDGE_BASE_URL")
    if not api_key:
        try:
            data = json.loads(Path(DEFAULT_CONFIG_PATH).read_text())
            providers = data.get("providers", {})
            if providers.get("openai", {}).get("apiKey"):
                api_key = providers["openai"]["apiKey"]
                base_url = base_url or providers["openai"].get("apiBase")
            elif providers.get("openrouter", {}).get("apiKey"):
                api_key = providers["openrouter"]["apiKey"]
                base_url = base_url or "https://openrouter.ai/api/v1"
        except Exception:
            pass
    if not api_key:
        return None, model
    kwargs = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    return AsyncOpenAI(**kwargs), model


_JUDGE_SYS = (
    "You are a strict evaluator of a web-navigation agent. Given a TASK, an "
    "optional SUCCESS RUBRIC, and the agent's FINAL ANSWER, decide whether the "
    "task was genuinely accomplished. Be skeptical of fabricated or vague "
    "answers: an answer that lacks the concrete data the task asked for is a "
    "FAILURE. Reply with ONLY a JSON object: "
    '{"success": true|false, "rationale": "<one sentence>"}.'
)


def _parse_verdict(txt: str) -> dict:
    m = re.search(r"\{.*\}", txt or "", re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            return {
                "success": bool(obj.get("success")),
                "rationale": str(obj.get("rationale", ""))[:300],
            }
        except Exception:
            pass
    low = (txt or "").lower()
    if "true" in low and "false" not in low:
        return {"success": True, "rationale": (txt or "")[:300]}
    return {"success": False, "rationale": (txt or "")[:300] or "unparseable judge output"}


async def judge(task, final_answer: str, *, extra_context: str = "") -> dict:
    """LLM-judge the final answer against the task rubric. Returns
    {success: bool|None, rationale, judge_model}. success=None => judge unavailable."""
    client, model = _resolve_judge_client()
    if client is None:
        return {
            "success": None,
            "rationale": "no judge API key available",
            "judge_model": model,
        }
    rubric = getattr(task, "reference", None) or (
        "(no explicit rubric — use the task's implied success criteria)"
    )
    user = (
        f"TASK:\n{task.instruction}\n\nSUCCESS RUBRIC:\n{rubric}\n\n"
        f"AGENT FINAL ANSWER:\n{final_answer or '(empty)'}\n"
    )
    if extra_context:
        user += f"\nADDITIONAL CONTEXT:\n{extra_context[:2000]}\n"
    messages = [
        {"role": "system", "content": _JUDGE_SYS},
        {"role": "user", "content": user},
    ]
    # Try with temperature=0 (reproducible); retry without if the model rejects it.
    for kwargs in ({"temperature": 0}, {}):
        try:
            resp = await client.chat.completions.create(
                model=model, messages=messages, **kwargs
            )
            txt = (resp.choices[0].message.content or "").strip()
            verdict = _parse_verdict(txt)
            verdict["judge_model"] = model
            return verdict
        except Exception as exc:
            last = exc
            continue
    return {"success": None, "rationale": f"judge error: {last}", "judge_model": model}
