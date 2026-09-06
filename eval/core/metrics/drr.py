"""Dead-End Revisit Rate (DRR) and repetition diagnostics.

An action signature is ``(normalised url, tool, normalised target)`` where the
target is the vision label / selector / typed text taken from the step
arguments (V_n indices rotate between screenshots, so the LABEL is the stable
identity). A signature is a dead end once a step with it failed (step
``success`` is False or its result carries a failure tag). A *revisit* is a
later step with the same signature.

    DRR = revisits / distinct dead-end signatures     (0 when there were none)

Also reported: ``repeated_actions`` (a step identical to the previous step),
``url_revisits`` / ``regressions`` (url_visit events), and the click-guard
refusals (dead_click_blocked / same_element_blocked events, falling back to
transcript tags).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from eval.core.loaders import load_events, load_steps
from eval.core.metrics.rpr import norm_url
from eval.core.records import RunRecord

_FAIL_RE = re.compile(r"\[(click_at_failed|click_silent|no_effect:|dead_click_blocked|same_element_blocked|"
                      r"click_loop_detected|stale_index|VERIFY_MISS|ERROR|NETWORK_BLOCKED|CF_INTERSTITIAL)|"
                      r"\bFAILED\b|HTTP\s+(401|403|404|429|451|503)\b", re.I)
_LABEL_RE = re.compile(r'"(?:expected_label|target_label|label|text|css|selector|url|keys|value|option)"\s*:\s*"([^"]{1,120})"')
_VLABEL_RE = re.compile(r'^V\d+\|"?([^"]*)"?')


def normalise_target(tool: str, args: Any) -> str:
    """Stable identity of what the step targeted."""
    if args is None:
        return ""
    if isinstance(args, (dict, list)):
        text = json.dumps(args, sort_keys=True, ensure_ascii=False)
    else:
        text = str(args)
    m = _VLABEL_RE.match(text.strip())
    if m and m.group(1):
        return m.group(1).strip().lower()[:80]
    labels = _LABEL_RE.findall(text)
    if labels:
        return " | ".join(l.strip().lower() for l in labels)[:120]
    return text.strip().lower()[:120]


def step_failed(step: dict[str, Any]) -> bool:
    if step.get("success") is False:
        return True
    return bool(_FAIL_RE.search(str(step.get("result") or "")))


def drr_from_steps(steps: list[dict[str, Any]]) -> dict[str, Any]:
    dead: dict[tuple[str, str, str], int] = {}   # signature -> step index of first failure
    revisits = 0
    revisit_examples: list[dict[str, Any]] = []
    repeated = 0
    prev_sig: tuple[str, str, str] | None = None
    for i, s in enumerate(steps):
        tool = str(s.get("tool") or "")
        sig = (norm_url(s.get("url")), tool, normalise_target(tool, s.get("args")))
        if tool in ("browser_screenshot", "browser_get_markdown", "browser_list_elements", "browser_wait_for"):
            prev_sig = sig
            continue  # read-only observations are not dead ends
        if sig in dead:
            revisits += 1
            if len(revisit_examples) < 10:
                revisit_examples.append({"step": i, "first_failure_step": dead[sig], "tool": tool, "target": sig[2], "url": sig[0]})
        if step_failed(s) and sig not in dead:
            dead[sig] = i
        if prev_sig == sig:
            repeated += 1
        prev_sig = sig
    n_dead = len(dead)
    return {"dead_end_signatures": n_dead, "revisits": revisits, "drr": (revisits / n_dead) if n_dead else 0.0,
            "repeated_actions": repeated, "steps": len(steps), "revisit_examples": revisit_examples}


def compute(run_dir: Path, rec: RunRecord) -> dict[str, Any]:
    steps = load_steps(run_dir)
    out = drr_from_steps(steps)
    events = load_events(run_dir, include_orchestrator=False)
    visits = [e for e in events if e.get("type") == "url_visit"]
    out["url_revisits"] = sum(1 for e in visits if int(e.get("visits") or 0) > 1)
    out["regressions"] = sum(1 for e in visits if e.get("regression"))
    guard = {t: sum(1 for e in events if e.get("type") == t) for t in ("dead_click_blocked", "same_element_blocked")}
    if not any(guard.values()):
        tags = (rec.counts or {}).get("tags") or {}
        guard = {t: int(tags.get(t) or 0) for t in guard}
    out.update(guard)
    out["source"] = "events+steps" if visits else "steps+tags"
    return out
