"""Load the 12 discordant pairs named in the frozen plan.

The original WebJudge success bit printed in the trace file is not returned.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

STIMULUS_REL = "eval/artifacts/ablate10_paper/e12_traces__ablate10__ledger_vs_full_history.md"
_HEADER = re.compile(r"^## ([0-9a-f]{32}) .*$", re.M)
_TASK = re.compile(r"^Task: (.+)$", re.M)
_ARM = re.compile(r"^\*\*(ledger|full_history)\*\*.*$", re.M)
_ANSWER = re.compile(r"^- Final answer: (.*)$", re.M)


def stimulus_sha256(root: Path) -> str:
    return hashlib.sha256((root / STIMULUS_REL).read_bytes()).hexdigest()


def parse_stimuli(root: Path) -> list[dict]:
    text = (root / STIMULUS_REL).read_text()
    headers = list(_HEADER.finditer(text))
    units: list[dict] = []
    for i, header in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        block = text[header.end():end]
        task_id = header.group(1)
        task_m = _TASK.search(block)
        if task_m is None:
            raise ValueError(f"{task_id} has no Task line")
        instruction = task_m.group(1).strip()
        arms = list(_ARM.finditer(block))
        found = {}
        for j, arm in enumerate(arms):
            arm_end = arms[j + 1].start() if j + 1 < len(arms) else len(block)
            chunk = block[arm.end():arm_end]
            ans = _ANSWER.search(chunk)
            if ans is None:
                raise ValueError(f"{task_id} {arm.group(1)} has no final answer")
            found[arm.group(1)] = ans.group(1).strip()
        for arm_name in ("ledger", "full_history"):
            if arm_name not in found:
                raise ValueError(f"{task_id} missing {arm_name}")
            units.append({
                "task_id": task_id,
                "source_arm": arm_name,
                "instruction": instruction,
                "source_report": found[arm_name],
                "unit_id": f"{task_id}:{arm_name}",
            })
    return units
