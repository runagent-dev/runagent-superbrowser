"""WebJudge — Online-Mind2Web's automatic evaluator, ported to Python.

Reference: https://github.com/OSU-NLP-Group/Online-Mind2Web (src/methods),
prompts reproduced verbatim (via the BrowserOS TypeScript port of the same
three-step protocol):

1. Key-point identification from the task text.
2. Key-screenshot identification: every trajectory screenshot is scored 1-5
   for whether it shows evidence of the necessary steps; scores >= 3 are kept
   (at most ``max_images``, the most recent ones).
3. Outcome judgment from task + key points + action history + the kept
   screenshots (+ their reasons) -> "success" | "failure".

The benchmark reports its judge model separately (gpt-4o in the original
release; o4-mini reaches the highest human agreement in the upstream README).
We pin it via ``SUPERBROWSER_EVAL_WEBJUDGE_MODEL`` (default gpt-4o) and record
it in every verdict. Screenshots come from the run directory
(``screenshots/NNN.jpg`` written by the bridge when
SUPERBROWSER_TRACE_SCREENSHOTS=1); the action history comes from the worker
transcripts. Temperature 0 throughout.
"""
from __future__ import annotations

import asyncio
import base64
import re
from pathlib import Path
from typing import Any

from .base import Verdict, action_history, add_usage, resolve_client, usage_of

DEFAULT_MODEL = "gpt-4o"
SCORE_THRESHOLD = 3
MAX_IMAGES = 50

STEP1_KEY_POINTS_SYSTEM = """You are an expert tasked with analyzing a given task to identify the key points explicitly stated in the task description.

**Objective**: Carefully analyze the task description and extract the critical elements explicitly mentioned in the task for achieving its goal.

**Instructions**:
1. Read the task description carefully.
2. Identify and extract **key points** directly stated in the task description.
   - A **key point** is a critical element, condition, or step explicitly mentioned in the task description.
   - Do not infer or add any unstated elements.
   - Words such as "best," "highest," "cheapest," "latest," "most recent," "lowest," "closest," "highest-rated," "largest," and "newest" must go through the sort function(e.g., the key point should be "Filter by highest").

**Respond with**:
- **Key Points**: A numbered list of the explicit key points for completing this task, one per line, without explanations or additional details."""

STEP2_IMAGE_SCORING_SYSTEM = """You are an expert evaluator tasked with determining whether an image contains information about the necessary steps to complete a task.

**Objective**: Analyze the provided image and decide if it shows essential steps or evidence required for completing the task. Use your reasoning to explain your decision before assigning a score.

**Instructions**:
1. Provide a detailed description of the image, including its contents, visible elements, text (if any), and any notable features.

2. Carefully examine the image and evaluate whether it contains necessary steps or evidence crucial to task completion:
- Identify key points that could be relevant to task completion, such as actions, progress indicators, tool usage, applied filters, or step-by-step instructions.
- Does the image show actions, progress indicators, or critical information directly related to completing the task?
- Is this information indispensable for understanding or ensuring task success?
- If the image contains partial but relevant information, consider its usefulness rather than dismissing it outright.

3. Provide your response in the following format:
- **Reasoning**: Explain your thought process and observations. Mention specific elements in the image that indicate necessary steps, evidence, or lack thereof.
- **Score**: Assign a score based on the reasoning, using the following scale:
    - **1**: The image does not contain any necessary steps or relevant information.
    - **2**: The image contains minimal or ambiguous information, unlikely to be essential.
    - **3**: The image includes some relevant steps or hints but lacks clarity or completeness.
    - **4**: The image contains important steps or evidence that are highly relevant but not fully comprehensive.
    - **5**: The image clearly displays necessary steps or evidence crucial for completing the task.

Respond with:
1. **Reasoning**: [Your explanation]
2. **Score**: [1-5]"""

STEP3_OUTCOME_SYSTEM = """You are an expert in evaluating the performance of a web navigation agent. The agent is designed to help a human user navigate a website to complete a task. Given the user's task, the agent's action history, key points for task completion, some potentially important web pages in the agent's trajectory and their reasons, your goal is to determine whether the agent has completed the task and achieved all requirements.

Your response must strictly follow the following evaluation criteria!
*Important Evaluation Criteria*:
1: The filtered results must be displayed correctly. If filters were not properly applied (i.e., missing selection, missing confirmation, or no visible effect in results), the task is not considered successful.
2: You must carefully check whether these snapshots and action history meet these key points. Ensure that specific filter conditions, such as "best," "highest," "cheapest," "latest," "most recent," "lowest," "closest," "highest-rated," "largest," and "newest" are correctly applied using the filter function(e.g., sort function).
3: Certain key points or requirements should be applied by the filter. Otherwise, a search with all requirements as input will be deemed a failure since it cannot guarantee that all results meet the requirements!
4: If the task requires filtering by a specific range of money, years, or the number of beds and bathrooms, the applied filter must exactly match the given requirement. Any deviation results in failure. To ensure the task is successful, the applied filter must precisely match the specified range without being too broad or too narrow.
Examples of Failure Cases:
- If the requirement is less than $50, but the applied filter is less than $25, it is a failure.
- If the requirement is $1500-$2500, but the applied filter is $2000-$2500, it is a failure.
- If the requirement is $25-$200, but the applied filter is $0-$200, it is a failure.
- If the required years are 2004-2012, but the filter applied is 2001-2012, it is a failure.
- If the required years are before 2015, but the applied filter is 2000-2014, it is a failure.
- If the task requires exactly 2 beds, but the filter applied is 2+ beds, it is a failure.
5: Some tasks require a submission action or a display of results to be considered successful.
6: If the retrieved information is invalid or empty(e.g., No match was found), but the agent has correctly performed the required action, it should still be considered successful.
7: If the current page already displays all available items, then applying a filter is not necessary. As long as the agent selects items that meet the requirements (e.g., the cheapest or lowest price), the task is still considered successful.

*IMPORTANT*
Format your response into two lines as shown below:

Thoughts: <your thoughts and reasoning process based on double-checking each key points and the evaluation criteria>
Status: "success" or "failure\""""

_NUM_PREFIX = re.compile(r"^(\d+)")


def list_screenshots(run_dir: Path) -> list[Path]:
    """Ordered trajectory screenshots (``NNN-*.jpg|png``) for a run."""
    d = Path(run_dir) / "screenshots"
    if not d.exists():
        return []
    files = [p for p in d.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png")]

    def key(p: Path) -> tuple[int, str]:
        m = _NUM_PREFIX.match(p.name)
        return (int(m.group(1)) if m else 10**9, p.name)

    return sorted(files, key=key)


def _data_url(path: Path) -> str:
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def parse_key_points(content: str) -> str:
    if "**Key Points**:" in content:
        return content.split("**Key Points**:", 1)[1].strip()
    if "Key Points:" in content:
        return content.split("Key Points:", 1)[1].strip()
    return content.strip()


def parse_score(content: str) -> tuple[int, str]:
    # Accepts "Score: 4", "**Score**: 4", "Score**: 4" and "Score: **4**"
    # (the prompt's own format is "2. **Score**: [1-5]").
    m = re.search(r"Score\**\s*[:：]?\s*\**\s*\[?([1-5])", content, re.I)
    score = int(m.group(1)) if m else 1
    t = re.search(r"\*\*Reasoning\*\*:?\s*([\s\S]*?)(?=\n\n|\*\*Score|$)", content, re.I)
    thought = t.group(1).strip().replace("\n", " ") if t else (content.split("\n", 1)[0] if content else "")
    return score, thought


def parse_status(content: str) -> bool:
    m = re.search(r"Status:\s*\"?(success|failure)\"?", content, re.I)
    return bool(m and m.group(1).lower() == "success")


class WebJudge:
    def __init__(self, client: Any, model: str, *, score_threshold: int = SCORE_THRESHOLD,
                 max_images: int = MAX_IMAGES, concurrency: int = 4):
        self.client = client
        self.model = model
        self.score_threshold = score_threshold
        self.max_images = max_images
        self._sem = asyncio.Semaphore(concurrency)
        self.usage: dict[str, int] = {}

    async def _chat(self, messages: list[dict[str, Any]], *, max_tokens: int) -> str:
        async with self._sem:
            resp = await self.client.chat.completions.create(
                model=self.model, temperature=0, messages=messages, max_tokens=max_tokens)
        self.usage = add_usage(self.usage, usage_of(resp))
        return (resp.choices[0].message.content or "") if resp.choices else ""

    async def identify_key_points(self, task: str) -> str:
        content = await self._chat([
            {"role": "system", "content": STEP1_KEY_POINTS_SYSTEM},
            {"role": "user", "content": f"Task: {task}"},
        ], max_tokens=512)
        return parse_key_points(content)

    async def score_screenshot(self, task: str, key_points: str, path: Path) -> tuple[int, str]:
        content = await self._chat([
            {"role": "system", "content": STEP2_IMAGE_SCORING_SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": (f"**Task**: {task}\n\n**Key Points for Task Completion**: {key_points}\n\n"
                                          "The snapshot of the web page is shown in the image.")},
                {"type": "image_url", "image_url": {"url": _data_url(path), "detail": "high"}},
            ]},
        ], max_tokens=512)
        return parse_score(content)

    async def judge_outcome(self, task: str, key_points: str, actions: list[str],
                            images: list[Path], thoughts: list[str]) -> tuple[bool, str]:
        actions_fmt = "\n".join(f"{i + 1}. {a}" for i, a in enumerate(actions)) or "No actions recorded"
        thoughts_fmt = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(thoughts)) or "No relevant screenshots identified"
        if images:
            text = (f"User Task: {task}\n\nKey Points: {key_points}\n\nAction History:\n{actions_fmt}\n\n"
                    f"The potentially important snapshots of the webpage in the agent's trajectory and their reasons:\n{thoughts_fmt}")
            content: list[dict[str, Any]] = [{"type": "text", "text": text}]
            for p in images:
                content.append({"type": "image_url", "image_url": {"url": _data_url(p), "detail": "high"}})
        else:
            content = [{"type": "text", "text": f"User Task: {task}\n\nKey Points: {key_points}\n\nAction History:\n{actions_fmt}"}]
        out = await self._chat([
            {"role": "system", "content": STEP3_OUTCOME_SYSTEM},
            {"role": "user", "content": content},
        ], max_tokens=1000)
        return parse_status(out), out

    async def evaluate(self, task: str, screenshots: list[Path], actions: list[str]) -> Verdict:
        key_points = await self.identify_key_points(task)
        scored = await asyncio.gather(*(self.score_screenshot(task, key_points, p) for p in screenshots),
                                      return_exceptions=True)
        per_shot: list[dict[str, Any]] = []
        kept: list[tuple[Path, int, str]] = []
        for p, res in zip(screenshots, scored):
            if isinstance(res, Exception):
                per_shot.append({"file": p.name, "score": None, "error": str(res)[:200]})
                continue
            score, thought = res
            per_shot.append({"file": p.name, "score": score, "thought": thought[:400]})
            if score >= self.score_threshold:
                kept.append((p, score, thought))
        if len(kept) > self.max_images:
            kept = kept[-self.max_images:]
        images = [k[0] for k in kept]
        thoughts = [f"Screenshot {k[0].name} (score {k[1]}): {k[2]}" for k in kept]
        ok, reasoning = await self.judge_outcome(task, key_points, actions, images, thoughts)
        return Verdict("webjudge", ok, reasoning[:2000], self.model, details={
            "key_points": key_points,
            "screenshots_evaluated": len(screenshots),
            "screenshots_relevant": len(images),
            "score_threshold": self.score_threshold,
            "per_screenshot": per_shot,
            "n_actions": len(actions),
        }, usage=dict(self.usage))


async def judge(task: Any, run_dir: Path, *, transcripts: list[dict[str, Any]], client: Any = None,
                model: str | None = None) -> Verdict:
    if client is None:
        client, resolved = resolve_client(model_env="SUPERBROWSER_EVAL_WEBJUDGE_MODEL", default_model=DEFAULT_MODEL)
        model = model or resolved
    model = model or DEFAULT_MODEL
    if client is None:
        return Verdict("webjudge", None, "no judge API key available", model)
    shots = list_screenshots(run_dir)
    actions = action_history(transcripts)
    if not shots and not actions:
        return Verdict("webjudge", None, "no screenshots and no actions recorded for this run", model,
                       details={"screenshots_evaluated": 0, "n_actions": 0})
    try:
        return await WebJudge(client, model).evaluate(task.instruction, shots, actions)
    except Exception as exc:  # noqa: BLE001 - never crash a harvest on a judge error
        return Verdict("webjudge", None, f"judge error: {exc}", model)
