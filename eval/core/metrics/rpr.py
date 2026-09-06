"""Redundant Perception Rate (RPR).

RPR = vision passes whose page fingerprint (normalised url, dom_hash,
dom_text_hash) equals the previous pass's / total vision passes. A redundant
pass that the cache served is cheap (``redundant_cached``); a redundant pass
that went to the model is waste (``redundant_uncached``). We also report the
page-churn rate (fraction of consecutive passes with a changed DOM hash),
which is the stratification variable for E5 (stable / mild / dynamic pages).

Source: ``vision_calls.jsonl`` (SUPERBROWSER_TRACE_VISION). Fallback when the
trace is missing: the ``[VISION … cached=…]`` headers in the transcripts (a
proxy: it only sees the sync path).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from eval.core.loaders import load_transcripts, load_vision_calls
from eval.core.records import RunRecord

_HEADER_RE = re.compile(r"\[VISION\s+intent=\S+\s+page_type=\S+\s+cached=(true|false)", re.I)


def norm_url(u: str | None) -> str:
    if not u:
        return ""
    try:
        p = urlsplit(u)
        host = p.netloc.lower()
        host = host[4:] if host.startswith("www.") else host
        return urlunsplit((p.scheme.lower(), host, p.path.rstrip("/") or "/", p.query, ""))
    except Exception:
        return u


def rpr_from_calls(calls: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [c for c in calls if c.get("ok", True)]
    total = len(ok)
    redundant = redundant_cached = redundant_uncached = changed = 0
    prev: tuple[str, str, str] | None = None
    for c in ok:
        key = (norm_url(c.get("url")), c.get("dom_hash") or "", c.get("dom_text_hash") or "")
        if prev is not None:
            if key == prev:
                redundant += 1
                if c.get("cached"):
                    redundant_cached += 1
                else:
                    redundant_uncached += 1
            elif key[1] != prev[1]:
                changed += 1
        prev = key
    n_pairs = max(0, total - 1)
    return {
        "source": "vision_calls.jsonl", "vision_calls": total,
        "cache_hits": sum(1 for c in ok if c.get("cached")),
        "redundant": redundant, "redundant_cached": redundant_cached, "redundant_uncached": redundant_uncached,
        "rpr": (redundant / total) if total else None,
        "rpr_uncached": (redundant_uncached / total) if total else None,
        "page_churn": (changed / n_pairs) if n_pairs else None,
        "by_path": {p: sum(1 for c in ok if c.get("path") == p) for p in sorted({c.get("path") for c in ok if c.get("path")})},
    }


def rpr_from_transcripts(transcripts: list[dict[str, Any]]) -> dict[str, Any]:
    cached = total = 0
    for t in transcripts:
        for m in t.get("messages") or []:
            if m.get("role") != "tool":
                continue
            content = m.get("content")
            text = content if isinstance(content, str) else " ".join(
                (b.get("text") or "") for b in content if isinstance(b, dict)) if isinstance(content, list) else ""
            for hit in _HEADER_RE.findall(text):
                total += 1
                if hit.lower() == "true":
                    cached += 1
    return {"source": "transcript_headers", "vision_calls": total, "cache_hits": cached,
            "rpr": (cached / total) if total else None, "rpr_uncached": None, "page_churn": None,
            "redundant": cached, "redundant_cached": cached, "redundant_uncached": 0}


def compute(run_dir: Path, rec: RunRecord) -> dict[str, Any]:
    calls = load_vision_calls(run_dir)
    if calls:
        return rpr_from_calls(calls)
    return rpr_from_transcripts(load_transcripts(run_dir))
