"""Shared judge plumbing: verdict type, client resolution, run-dir readers."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eval._bootstrap import DEFAULT_CONFIG_PATH


@dataclass
class Verdict:
    judge: str
    success: bool | None            # None => evaluator unavailable / undecided
    rationale: str = ""
    model: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)   # judge tokens (cost accounting)

    def to_dict(self) -> dict[str, Any]:
        return {"judge": self.judge, "success": self.success, "rationale": self.rationale,
                "model": self.model, "details": self.details, "usage": self.usage}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Verdict":
        return cls(judge=d.get("judge", "?"), success=d.get("success"), rationale=d.get("rationale", ""),
                   model=d.get("model"), details=dict(d.get("details") or {}), usage=dict(d.get("usage") or {}))


# ------------------------------------------------------------------ clients
def resolve_client(*, model_env: str, default_model: str,
                   prefix: str | None = None) -> tuple[Any, str]:
    """(AsyncOpenAI | None, model).

    ``prefix`` scopes the credentials to one judge, so the screenshot judge and
    the answer judge can live on different providers (e.g. WebJudge on OpenAI,
    the answer judge on a Gemini OpenAI-compatible endpoint). Key precedence:
    ``<prefix>_API_KEY`` > ``SUPERBROWSER_EVAL_JUDGE_API_KEY`` > ``OPENAI_API_KEY``
    > ``~/.nanobot/config.json`` providers (openai, then openrouter); the base URL
    follows the key it belongs to, so a scoped key is never sent to the shared
    endpoint and vice versa. A judge must never be the candidate model — pin it
    explicitly."""
    from openai import AsyncOpenAI

    model = os.environ.get(model_env, default_model)
    api_key = base_url = None
    if prefix:  # scoped pair wins, and stays a pair
        api_key = os.environ.get(f"{prefix}_API_KEY") or None
        if api_key:
            base_url = os.environ.get(f"{prefix}_BASE_URL")
    if not api_key:
        api_key = os.environ.get("SUPERBROWSER_EVAL_JUDGE_API_KEY") or None
        if api_key:
            base_url = os.environ.get("SUPERBROWSER_EVAL_JUDGE_BASE_URL")
    if not api_key:
        api_key = os.environ.get("OPENAI_API_KEY") or None
        # a shared base URL is only meaningful for the shared key
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
    kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    return AsyncOpenAI(**kwargs), model


def usage_of(resp: Any) -> dict[str, int]:
    u = getattr(resp, "usage", None)
    if u is None:
        return {}
    return {"input_tokens": int(getattr(u, "prompt_tokens", 0) or 0),
            "output_tokens": int(getattr(u, "completion_tokens", 0) or 0)}


def add_usage(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, 0) + int(v)
    out["calls"] = out.get("calls", 0) + 1
    return out


# ------------------------------------------------------------ run-dir readers
def read_final_answer(run_dir: Path) -> str:
    p = Path(run_dir) / "result.txt"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def load_transcripts(run_dir: Path) -> list[dict[str, Any]]:
    """Worker transcripts (the delegation-tap dumps), in filename order."""
    out: list[dict[str, Any]] = []
    for p in sorted((Path(run_dir) / "workers").glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join((b.get("text") or "") if isinstance(b, dict) else str(b) for b in content)
    return "" if content is None else str(content)


def action_history(transcripts: list[dict[str, Any]], *, max_arg_chars: int = 300) -> list[str]:
    """``name(json-args)`` per executed tool call, across workers, in order.
    Mirrors what Online-Mind2Web feeds WebJudge as the action history."""
    actions: list[str] = []
    for t in transcripts:
        for m in t.get("messages") or []:
            if m.get("role") != "assistant":
                continue
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                name = fn.get("name") or tc.get("name") or "tool"
                args = fn.get("arguments")
                if not isinstance(args, str):
                    try:
                        args = json.dumps(args, ensure_ascii=False)
                    except Exception:
                        args = str(args)
                if len(args) > max_arg_chars:
                    args = args[:max_arg_chars] + "…"
                actions.append(f"{name}({args})")
    return actions


def final_url(transcripts: list[dict[str, Any]]) -> str | None:
    for t in reversed(transcripts):
        url = (t.get("meta") or {}).get("current_url")
        if url:
            return str(url)
    return None


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_json_verdict(txt: str) -> dict[str, Any] | None:
    m = _JSON_RE.search(txt or "")
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None
