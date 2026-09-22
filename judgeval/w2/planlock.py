"""Checks that execution is still on the tagged plan."""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

TAG = "w2-analysis-plan-2026-09-22"
PLAN_REL = "judgeval/ANALYSIS_PLAN_W2.md"

_FENCE = re.compile(r"^([A-Z_]+):\n\n```\n(.*?)```", re.M | re.S)


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True)


def tagged_plan(root: Path) -> str:
    return subprocess.check_output(["git", "show", f"{TAG}:{PLAN_REL}"], cwd=root).decode()


def verify_plan_frozen(root: Path) -> str:
    """Return the plan text. Refuse to continue if the working tree or the tag message drifted."""
    blob = tagged_plan(root).encode()
    work = (root / PLAN_REL).read_bytes()
    if blob != work:
        raise SystemExit("working-tree analysis plan differs from the tagged blob; judge calls refused")
    digest = hashlib.sha256(blob).hexdigest()
    message = _git(root, "for-each-ref", f"refs/tags/{TAG}", "--format=%(contents)")
    if digest not in message:
        raise SystemExit("tag message does not record the plan sha256; judge calls refused")
    commit = _git(root, "rev-parse", f"{TAG}^{{}}").strip()
    head_plan = subprocess.check_output(["git", "rev-parse", f"{TAG}^{{}}"], cwd=root, text=True).strip()
    if commit != head_plan:
        raise SystemExit("tag does not resolve")
    return blob.decode()


def instructions(plan_text: str) -> dict[str, str]:
    found = {name: body.strip("\n") for name, body in _FENCE.findall(plan_text)}
    needed = ["ASSERT", "DISCLOSE", "VERBOSE_CONF", "FORMAT_PROSE", "FORMAT_BULLETS"]
    missing = [n for n in needed if n not in found]
    if missing:
        raise SystemExit(f"plan is missing rewriter instructions: {missing}")
    return {n: found[n] for n in needed}


def plan_task_ids(plan_text: str) -> list[str]:
    ids = re.findall(r"^\d+\. `([0-9a-f]{32})`", plan_text, re.M)
    if len(ids) != 12:
        raise SystemExit(f"plan task list has {len(ids)} ids, expected 12")
    return ids


def plan_stimulus_sha(plan_text: str) -> str:
    m = re.search(r"sha256 `([0-9a-f]{64})`", plan_text)
    if not m:
        raise SystemExit("plan has no stimulus sha256")
    return m.group(1)


def plan_format_ids(plan_text: str) -> list[str]:
    # The format subset is the three backticked ids in the paragraph that introduces it.
    block = plan_text.split("Format pair, six units only", 1)[1].split("FORMAT_PROSE", 1)[0]
    ids = re.findall(r"`([0-9a-f]{32})`", block)
    if len(ids) != 3:
        raise SystemExit(f"plan format subset has {len(ids)} ids, expected 3")
    return ids
