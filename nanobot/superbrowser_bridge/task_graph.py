"""Subconscious task graph — multi-step plan that survives across iterations.

Today the vision agent only knows the *overall* `task_instruction` (a
prose string). Each iteration computes a fresh action plan with no memory
of which sub-step is active or what signal would mean it's done. So
bboxes are over-broad, the LLM picks suboptimally, and complex flows
(multi-filter searches, nested checkouts) drift.

This module gives the orchestrator a persistent decomposition: at task
start, the user instruction is broken into an ordered list of subgoals,
each carrying its own `look_for` hints and `expected_signals`. The active
subgoal is threaded through every vision call so bboxes get filtered by
what serves *this* step, not the overall task. After each action a
deterministic updater checks whether any subgoal's signals fired and
advances the pointer.

The module is intentionally pure-Python — no imports from session_tools
or vision_agent core — so it stays unit-testable and avoids circular
imports during worker startup.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Literal, Optional

logger = logging.getLogger(__name__)


# --- Data model -----------------------------------------------------------

SignalKind = Literal[
    "url_contains",
    "url_matches",
    "element_visible",
    "element_text_matches",
    "scroll_at_bottom",
    "dom_mutation",
    "markdown_contains",
    "vision_flag",
]

SubgoalStatus = Literal["pending", "active", "done", "skipped", "blocked"]


@dataclass
class Precondition:
    """What must be visible/true on the current frame BEFORE a mutation
    tool fires for the active subgoal.

    Emitted by the decompose LLM, checked by the validator before every
    click/type. The validator resolves a matching element in the fused
    perception view (vision bbox + DOM fallback) — if it can't, the
    tool is rejected and a coverage-pass re-perception is triggered
    instead of executing a blind click.

    All fields optional to preserve back-compat with older decompositions:
    a Subgoal with no precondition behaves as today.
    """

    element_label: str = ""
    role_hint: Optional[str] = None
    scene_layer: Optional[str] = None
    text_regex: Optional[str] = None
    required: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> Optional["Precondition"]:
        if not isinstance(data, dict):
            return None
        label = str(data.get("element_label") or "").strip()
        # A precondition with no label is effectively absent — collapse
        # to None so downstream checks can cheap-out on `is None`.
        if not label and not data.get("text_regex"):
            return None
        role = data.get("role_hint")
        layer = data.get("scene_layer")
        regex = data.get("text_regex")
        required = bool(data.get("required", True))
        return cls(
            element_label=label[:120],
            role_hint=str(role).strip()[:20] if role else None,
            scene_layer=str(layer).strip()[:20] if layer else None,
            text_regex=str(regex).strip()[:200] if regex else None,
            required=required,
        )


@dataclass
class Signal:
    """Completion or transition signal for a subgoal.

    Evaluated deterministically against the post-action snapshot
    (vision response + DOM element text + URL + step result). Each
    `kind` reads a specific slice of the snapshot:

      url_contains       payload={"text": str}            URL substring (lowercased)
      url_matches        payload={"pattern": regex}       URL regex
      element_visible    payload={"text": str|regex}      bbox/element label match
      element_text_matches payload={"pattern": regex}     interactive element regex
      scroll_at_bottom   payload={}                       scroll telemetry says reached_bottom
      dom_mutation       payload={"selector": css}        not yet wired — placeholder
      markdown_contains  payload={"text": str}            substring in last extracted text
      vision_flag        payload={"name": str, "value": bool}  VisionResponse.flags.<name>

    payload is intentionally untyped (dict) — easier to round-trip
    through JSON than per-kind dataclasses, and the validator at
    evaluation time tolerates missing keys.
    """

    kind: SignalKind
    payload: dict = field(default_factory=dict)


@dataclass
class Subgoal:
    """A single sub-step of the overall task."""

    id: str
    description: str
    look_for: list[str] = field(default_factory=list)
    expected_signals: list[Signal] = field(default_factory=list)
    transitions: list[str] = field(default_factory=list)
    status: SubgoalStatus = "pending"
    started_at: float = 0.0
    completed_at: float = 0.0
    blocker_note: str = ""
    precondition: Optional[Precondition] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["expected_signals"] = [asdict(s) for s in self.expected_signals]
        d["precondition"] = self.precondition.to_dict() if self.precondition else None
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Subgoal":
        signals = [Signal(**s) for s in data.get("expected_signals") or []]
        return cls(
            id=str(data.get("id") or ""),
            description=str(data.get("description") or ""),
            look_for=list(data.get("look_for") or []),
            expected_signals=signals,
            transitions=list(data.get("transitions") or []),
            status=data.get("status") or "pending",  # type: ignore[assignment]
            started_at=float(data.get("started_at") or 0.0),
            completed_at=float(data.get("completed_at") or 0.0),
            blocker_note=str(data.get("blocker_note") or ""),
            precondition=Precondition.from_dict(data.get("precondition")),
        )

    def check_precondition(
        self, fused: Any, *, intent_hint: str = "",
    ) -> "PreconditionCheck":
        """Verify this subgoal's precondition against a FusedPerception.

        Returns a PreconditionCheck describing whether the precondition
        is satisfied and, if not, what corrective action the validator
        should trigger (re-perceive / rewind).

        `fused` is typed Any to avoid a circular import with
        `perception_fusion` — duck-typed on `.iter()` and
        `.resolve_by_label()`.
        """
        pre = self.precondition
        if pre is None or not pre.required:
            return PreconditionCheck(satisfied=True, reason="no_precondition")
        label = (pre.element_label or intent_hint or "").strip()
        if not label and not pre.text_regex:
            return PreconditionCheck(satisfied=True, reason="empty_precondition")

        # Scene-layer gate: when the precondition names a specific scene
        # layer, any match must sit in that layer. "modal_open"/"primary"
        # are the currently-declared buckets — absent field means "any".
        def _layer_ok(element: Any) -> bool:
            if not pre.scene_layer:
                return True
            got = getattr(element, "scene_layer", None) or (
                getattr(getattr(element, "vision_bbox", None), "layer_id", None)
                if element is not None else None
            )
            if not got:
                return True  # unknown layer — don't over-gate
            wanted = pre.scene_layer.lower()
            return wanted in str(got).lower()

        # Try label match first.
        if label:
            elem = fused.resolve_by_label(label) if hasattr(fused, "resolve_by_label") else None
            if elem is not None and _layer_ok(elem):
                return PreconditionCheck(
                    satisfied=True, reason="label_match",
                    matched=elem,
                )
        # Regex fallback — scan fused elements for label_regex match.
        if pre.text_regex:
            try:
                pattern = re.compile(pre.text_regex, re.IGNORECASE)
            except re.error:
                pattern = None
            if pattern is not None:
                for elem in fused.iter() if hasattr(fused, "iter") else []:
                    elabel = (getattr(elem, "label", "") or "").strip()
                    if pattern.search(elabel) and _layer_ok(elem):
                        return PreconditionCheck(
                            satisfied=True, reason="regex_match",
                            matched=elem,
                        )

        # No match — the element the subgoal needs is not on the frame.
        # Coverage pass is the right next step.
        return PreconditionCheck(
            satisfied=False, reason="not_in_frame",
            required_action="re_perceive",
        )


@dataclass
class PreconditionCheck:
    """Return value from Subgoal.check_precondition."""

    satisfied: bool
    reason: str = ""
    matched: Any = None
    required_action: Optional[str] = None  # "re_perceive" | "rewind" | None


@dataclass
class TaskGraph:
    """Ordered subgoal DAG with a single active pointer.

    `subgoals` keeps insertion order (Python dict is ordered since 3.7),
    which matches the order returned by `decompose_task`. `active_id` is
    the current focus; `history` is an append-only log of transitions
    for telemetry / debugging.
    """

    subgoals: dict[str, Subgoal] = field(default_factory=dict)
    active_id: str = ""
    history: list[dict] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.subgoals)

    def current(self) -> Optional[Subgoal]:
        if not self.active_id:
            return None
        return self.subgoals.get(self.active_id)

    def index_of(self, subgoal_id: str) -> int:
        for i, sid in enumerate(self.subgoals.keys()):
            if sid == subgoal_id:
                return i
        return -1

    def advance(self, next_id: Optional[str], reason: str = "") -> None:
        """Mark the current subgoal `done` and pivot to `next_id`.

        If `next_id` is None or unknown, fall back to the next subgoal in
        insertion order. If the active subgoal is already the last one
        and no next is available, leave active_id unchanged but mark it
        done.
        """
        prev_id = self.active_id
        prev = self.current()
        now = time.time()
        if prev:
            prev.status = "done"
            prev.completed_at = now

        if not next_id or next_id not in self.subgoals:
            ids = list(self.subgoals.keys())
            if prev_id and prev_id in ids:
                idx = ids.index(prev_id) + 1
                next_id = ids[idx] if idx < len(ids) else None
            else:
                next_id = ids[0] if ids else None

        if next_id and next_id in self.subgoals:
            self.active_id = next_id
            cur = self.subgoals[next_id]
            cur.status = "active"
            if not cur.started_at:
                cur.started_at = now

        self.history.append({
            "at": now,
            "from": prev_id,
            "to": self.active_id,
            "reason": reason,
        })

    def mark_blocked(self, note: str) -> None:
        cur = self.current()
        if not cur:
            return
        cur.status = "blocked"
        cur.blocker_note = note
        self.history.append({
            "at": time.time(),
            "from": cur.id,
            "to": cur.id,
            "reason": f"blocked: {note}",
        })

    def to_brain_text(self) -> str:
        """Render as a `[PLAN]` block for the brain caption.

        Compact one-line-per-subgoal so the LLM can scan the whole plan
        at a glance. Keep it short: long plans steal context room from
        the actual page state.
        """
        if not self.subgoals:
            return ""
        lines = ["[PLAN]"]
        for sg in self.subgoals.values():
            marker = {
                "pending": "·",
                "active": "▶",
                "done": "✓",
                "skipped": "⤳",
                "blocked": "⊘",
            }.get(sg.status, "·")
            line = f"  {marker} {sg.id}: {sg.description}"
            if sg.status == "blocked" and sg.blocker_note:
                line += f"  (blocked: {sg.blocker_note})"
            lines.append(line)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "subgoals": {k: v.to_dict() for k, v in self.subgoals.items()},
            "active_id": self.active_id,
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaskGraph":
        sg_dict = {
            k: Subgoal.from_dict(v)
            for k, v in (data.get("subgoals") or {}).items()
        }
        return cls(
            subgoals=sg_dict,
            active_id=str(data.get("active_id") or ""),
            history=list(data.get("history") or []),
        )


def trivial_graph(task_instruction: str) -> TaskGraph:
    """Single-node fallback graph used whenever decomposition fails.

    Keeps the rest of the pipeline (vision plumbing, updater_check,
    worker hook) functional even when the LLM is unreachable or the
    task is too short to decompose.
    """
    sg = Subgoal(
        id="g1",
        description=(task_instruction or "complete the task").strip()[:200],
        status="active",
        started_at=time.time(),
    )
    return TaskGraph(subgoals={"g1": sg}, active_id="g1")


# --- LLM decomposition ----------------------------------------------------

_DECOMPOSE_SYSTEM = (
    "You decompose a single browsing task into a short ordered list of "
    "subgoals — the way a person would plan in their head before opening "
    "a website.\n\n"
    "Return JSON shaped EXACTLY like:\n"
    "{\n"
    "  \"subgoals\": [\n"
    "    {\n"
    "      \"id\": \"g1\",\n"
    "      \"description\": \"what this step achieves (1 short sentence)\",\n"
    "      \"look_for\": [\"text or role hints — what UI element to find\"],\n"
    "      \"precondition\": {\n"
    "        \"element_label\": \"Search\",\n"
    "        \"role_hint\": \"button\",\n"
    "        \"scene_layer\": null\n"
    "      },\n"
    "      \"expected_signals\": [\n"
    "        {\"kind\": \"url_contains\",   \"payload\": {\"text\": \"/results\"}},\n"
    "        {\"kind\": \"element_visible\", \"payload\": {\"text\": \"Sort by\"}}\n"
    "      ]\n"
    "    }\n"
    "  ]\n"
    "}\n\n"
    "Rules:\n"
    "  * 2–6 subgoals total. Don't over-decompose trivial tasks.\n"
    "  * Order subgoals by execution sequence: g1 first, then g2, …\n"
    "  * Each subgoal should map to a real UI barrier the user must "
    "    cross — picking a filter, opening a result, submitting a form. "
    "    Don't list internal LLM reasoning steps.\n"
    "  * `look_for` is short text/role hints (5 max), used to bias bbox "
    "    selection. Things like \"Filter by price\", \"input[type=email]\".\n"
    "  * `precondition` is the UI element that MUST be visible before "
    "    this subgoal fires a click/type. `element_label` is the visible "
    "    text or ARIA name to scan for; `role_hint` is button/input/link "
    "    when you know it, else null. The validator rejects any mutation "
    "    whose target doesn't satisfy this — so naming the element the "
    "    user will actually interact with is load-bearing.\n"
    "  * `expected_signals` is what proves the subgoal is done. Pick "
    "    deterministic ones: URL change, an element appearing, a flag "
    "    flipping. Avoid signals that depend on the LLM seeing something.\n"
    "  * Allowed signal kinds: url_contains | url_matches | "
    "    element_visible | element_text_matches | scroll_at_bottom | "
    "    markdown_contains | vision_flag.\n"
    "  * If the task is a one-shot ('open google.com'), return ONE "
    "    subgoal — that's fine. Omit `precondition` when the subgoal "
    "    is navigation-only (no click needed).\n"
    "  * Output ONLY the JSON object. No prose, no markdown fence."
)


def _decompose_user_prompt(task_instruction: str, target_url: Optional[str]) -> str:
    parts = [f"Task: {task_instruction.strip()}"]
    if target_url:
        parts.append(f"Starting URL: {target_url.strip()}")
    parts.append(
        "Decompose into the subgoals a competent user would naturally hold "
        "in mind while completing this task. Return the JSON object only."
    )
    return "\n".join(parts)


def _make_text_client():
    """Build an AsyncOpenAI client pointed at the vision endpoint.

    Reuses VISION_API_KEY / VISION_BASE_URL / VISION_MODEL — the same
    Gemini key used for vision. Text-only call (no image), so the model
    used here can be the same flash-grade model. Returns (client, model)
    or (None, None) if env is missing.
    """
    api_key = (os.environ.get("VISION_API_KEY") or "").strip()
    if not api_key:
        return None, None
    model = (os.environ.get("VISION_MODEL") or "").strip()
    if not model:
        return None, None
    base_url = (
        os.environ.get("VISION_BASE_URL")
        or "https://generativelanguage.googleapis.com/v1beta/openai"
    )
    try:
        from openai import AsyncOpenAI
    except Exception as exc:
        logger.debug("task_graph: openai SDK unavailable (%s)", exc)
        return None, None
    timeout_s = float(os.environ.get("TASK_GRAPH_TIMEOUT_S") or "12.0")
    client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout_s)
    return client, model


async def _decompose_via_llm(
    task_instruction: str,
    target_url: Optional[str],
) -> Optional[list[dict]]:
    """Async path for decomposition — returns parsed subgoals or None."""
    client, model = _make_text_client()
    if client is None:
        return None
    try:
        completion = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _DECOMPOSE_SYSTEM},
                {"role": "user", "content": _decompose_user_prompt(
                    task_instruction, target_url,
                )},
            ],
            max_tokens=1024,
            temperature=0.1,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        logger.info("task_graph decompose failed: %s", exc)
        return None
    raw = (completion.choices[0].message.content or "").strip()
    return _parse_subgoals(raw)


def _parse_subgoals(raw: str) -> Optional[list[dict]]:
    if not raw:
        return None
    # Some models wrap JSON in a fence even when asked not to — strip.
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", raw, re.DOTALL)
    if fence:
        raw = fence.group(1)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    sgs = data.get("subgoals")
    if not isinstance(sgs, list) or not sgs:
        return None
    return [s for s in sgs if isinstance(s, dict)]


def decompose_task(task_instruction: str, target_url: Optional[str] = None) -> TaskGraph:
    """Build a TaskGraph from the user's instruction.

    One-shot LLM call (text-only Gemini). On any failure — no API key,
    network error, malformed JSON — fall back to `trivial_graph` so the
    rest of the pipeline still works. The decomposer is best-effort:
    it improves bbox targeting when present, but its absence must never
    crash the worker.
    """
    text = (task_instruction or "").strip()
    if not text:
        return trivial_graph("complete the task")

    parsed = None
    try:
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            parsed = asyncio.run(_decompose_via_llm(text, target_url))
        else:
            # Already inside an async loop — schedule and wait via
            # run_coroutine_threadsafe is overkill; just degrade to a
            # threadpool run. configure_budget runs synchronously today.
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(asyncio.run, _decompose_via_llm(text, target_url))
                parsed = fut.result(timeout=20.0)
    except Exception as exc:
        logger.info("task_graph decompose orchestration failed: %s", exc)
        parsed = None

    if not parsed:
        return trivial_graph(text)

    return _build_graph_from_parsed(parsed)


def _build_graph_from_parsed(items: list[dict]) -> TaskGraph:
    subgoals: dict[str, Subgoal] = {}
    valid_kinds = {
        "url_contains", "url_matches", "element_visible",
        "element_text_matches", "scroll_at_bottom", "dom_mutation",
        "markdown_contains", "vision_flag",
    }
    for i, raw in enumerate(items):
        sid = str(raw.get("id") or f"g{i+1}").strip() or f"g{i+1}"
        if sid in subgoals:
            sid = f"g{i+1}"
        desc = str(raw.get("description") or "").strip()
        if not desc:
            continue
        look_for_raw = raw.get("look_for") or []
        look_for = [str(x).strip() for x in look_for_raw if str(x).strip()][:5]
        sigs_raw = raw.get("expected_signals") or []
        signals: list[Signal] = []
        for s in sigs_raw:
            if not isinstance(s, dict):
                continue
            kind = str(s.get("kind") or "").strip()
            if kind not in valid_kinds:
                continue
            payload = s.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            signals.append(Signal(kind=kind, payload=payload))  # type: ignore[arg-type]
        subgoals[sid] = Subgoal(
            id=sid,
            description=desc[:200],
            look_for=look_for,
            expected_signals=signals,
            status="pending",
            precondition=Precondition.from_dict(raw.get("precondition")),
        )

    if not subgoals:
        return trivial_graph("complete the task")

    first_id = next(iter(subgoals))
    subgoals[first_id].status = "active"
    subgoals[first_id].started_at = time.time()
    return TaskGraph(subgoals=subgoals, active_id=first_id)


# --- Updater --------------------------------------------------------------

# After this many actions on the same active subgoal with no signal firing,
# mark the subgoal stale and surface a hint to the brain.
DEFAULT_STALE_ACTION_THRESHOLD = 6


def _vision_bbox_labels(vision_resp: Any) -> list[str]:
    """Pull bbox labels off a VisionResponse-like object.

    Tolerates: a real VisionResponse pydantic model, a dict (post
    .model_dump()), or None. Returns a list of lowercased label strings.
    """
    if vision_resp is None:
        return []
    bboxes = getattr(vision_resp, "bboxes", None)
    if bboxes is None and isinstance(vision_resp, dict):
        bboxes = vision_resp.get("bboxes")
    if not bboxes:
        return []
    out: list[str] = []
    for b in bboxes:
        label = getattr(b, "label", None)
        if label is None and isinstance(b, dict):
            label = b.get("label")
        if isinstance(label, str) and label.strip():
            out.append(label.strip().lower())
    return out


def _vision_flag(vision_resp: Any, name: str) -> Optional[bool]:
    """Read VisionResponse.flags.<name>; tolerates dicts."""
    if vision_resp is None or not name:
        return None
    flags = getattr(vision_resp, "flags", None)
    if flags is None and isinstance(vision_resp, dict):
        flags = vision_resp.get("flags")
    if flags is None:
        return None
    val = getattr(flags, name, None)
    if val is None and isinstance(flags, dict):
        val = flags.get(name)
    if isinstance(val, bool):
        return val
    return None


def _matches_text(needle: str, hay: str) -> bool:
    if not needle:
        return False
    return needle.strip().lower() in (hay or "").lower()


def _matches_regex(pattern: str, hay: str) -> bool:
    if not pattern:
        return False
    try:
        return bool(re.search(pattern, hay or "", re.IGNORECASE))
    except re.error:
        return False


def evaluate_signal(
    sig: Signal,
    *,
    vision_resp: Any = None,
    dom_elements_text: str = "",
    url: str = "",
    scroll_telemetry: Optional[dict] = None,
    markdown_text: Optional[str] = None,
) -> bool:
    """True if this signal fires in the current snapshot."""
    payload = sig.payload or {}
    kind = sig.kind

    if kind == "url_contains":
        return _matches_text(str(payload.get("text") or ""), url)
    if kind == "url_matches":
        return _matches_regex(str(payload.get("pattern") or ""), url)

    if kind == "element_visible":
        needle = str(payload.get("text") or "")
        hay_labels = _vision_bbox_labels(vision_resp)
        if any(needle.lower() in lbl for lbl in hay_labels):
            return True
        # Fall through to DOM elements text — vision may have culled the bbox
        # but the element exists in the cheap snapshot.
        return _matches_text(needle, dom_elements_text)

    if kind == "element_text_matches":
        pattern = str(payload.get("pattern") or "")
        if any(_matches_regex(pattern, lbl) for lbl in _vision_bbox_labels(vision_resp)):
            return True
        return _matches_regex(pattern, dom_elements_text)

    if kind == "scroll_at_bottom":
        if not scroll_telemetry:
            return False
        return bool(scroll_telemetry.get("reached_bottom"))

    if kind == "markdown_contains":
        if markdown_text is None:
            return False
        return _matches_text(str(payload.get("text") or ""), markdown_text)

    if kind == "vision_flag":
        name = str(payload.get("name") or "")
        want = bool(payload.get("value", True))
        seen = _vision_flag(vision_resp, name)
        return seen is not None and seen == want

    # dom_mutation is a placeholder for now — needs DOM-mutation observer
    # support from the TS side, not yet wired.
    return False


def updater_check(
    graph: TaskGraph,
    *,
    vision_resp: Any = None,
    dom_elements_text: str = "",
    url: str = "",
    last_action: Optional[dict] = None,
    scroll_telemetry: Optional[dict] = None,
    markdown_text: Optional[str] = None,
    stale_action_threshold: int = DEFAULT_STALE_ACTION_THRESHOLD,
    actions_on_active: int = 0,
) -> tuple[Optional[str], str]:
    """Check whether the active subgoal completed and pick the next one.

    Returns (new_active_id, reason). If `new_active_id == graph.active_id`,
    no transition happened — caller need not re-render the plan. Reason
    is a short human-readable string for the worker_hook guidance line.

    Pure function — does not mutate `graph`. Callers apply the transition
    via `graph.advance(new_active_id, reason)`.

    Strategy:
      1. Evaluate signals for the *active* subgoal first. If any fire,
         advance to the first subgoal in `transitions` (or the next one
         in insertion order if `transitions` is empty).
      2. Otherwise scan downstream subgoals — if a *later* subgoal's
         signals all fire, the user clearly skipped ahead (e.g., a deep
         link), so advance straight to it.
      3. If neither fires AND `actions_on_active >= stale_action_threshold`,
         return reason="stale" so worker_hook can inject a hint without
         actually transitioning.
    """
    active = graph.current()
    if active is None:
        return graph.active_id, ""

    fired_active = [
        sig for sig in active.expected_signals
        if evaluate_signal(
            sig,
            vision_resp=vision_resp,
            dom_elements_text=dom_elements_text,
            url=url,
            scroll_telemetry=scroll_telemetry,
            markdown_text=markdown_text,
        )
    ]
    if fired_active:
        next_id = active.transitions[0] if active.transitions else None
        return next_id, (
            f"signal fired ({fired_active[0].kind}) → completing {active.id}"
        )

    # Scan downstream — the user may have skipped a step.
    ids = list(graph.subgoals.keys())
    try:
        active_idx = ids.index(active.id)
    except ValueError:
        active_idx = -1
    for sid in ids[active_idx + 1:]:
        sg = graph.subgoals[sid]
        if not sg.expected_signals:
            continue
        all_fire = all(
            evaluate_signal(
                sig,
                vision_resp=vision_resp,
                dom_elements_text=dom_elements_text,
                url=url,
                scroll_telemetry=scroll_telemetry,
                markdown_text=markdown_text,
            )
            for sig in sg.expected_signals
        )
        if all_fire:
            return sg.id, f"skipped ahead — all signals for {sg.id} already fired"

    if actions_on_active >= stale_action_threshold:
        return active.id, (
            f"stale: {actions_on_active} actions on {active.id} with no signal fired"
        )

    return active.id, ""


_REPLAN_SYSTEM = (
    "You are replanning the REMAINING steps of a browsing task because "
    "the page changed in an unexpected way. The overall goal does NOT "
    "change — only the path to it. You will be told which subgoals "
    "are already DONE; keep the same language for them. Emit ONLY the "
    "still-pending subgoals as a JSON list, keeping their ids stable "
    "when the intent is the same.\n\n"
    "Return JSON shaped EXACTLY like:\n"
    "{ \"subgoals\": [ { \"id\": \"g3\", \"description\": \"…\", "
    "\"look_for\": [\"…\"], \"expected_signals\": [{\"kind\":"
    "\"url_contains\",\"payload\":{\"text\":\"…\"}}] } ] }\n\n"
    "Rules:\n"
    "  * 1–5 subgoals total in the output — only what's LEFT.\n"
    "  * Prefer keeping the next pending subgoal's id (e.g. g2) unless "
    "    the page changed the plan so much that it no longer applies.\n"
    "  * Signal kinds allowed: url_contains | url_matches | "
    "    element_visible | element_text_matches | scroll_at_bottom | "
    "    markdown_contains | vision_flag.\n"
    "  * Output JSON only."
)


def _replan_user_prompt(
    *, task_instruction: str, current_url: str,
    done_subgoals: list[Subgoal], pending_subgoals: list[Subgoal],
) -> str:
    def _line(sg: Subgoal) -> str:
        return f"    - {sg.id}: {sg.description}"

    parts = [
        f"Overall task (unchanged): {task_instruction.strip()}",
        f"Current URL: {current_url or '(unknown)'}",
        "Done subgoals (do NOT re-emit):",
    ]
    parts.extend(_line(sg) for sg in done_subgoals) if done_subgoals else parts.append("    (none yet)")
    parts.append("Stale pending subgoals (rewrite as needed):")
    parts.extend(_line(sg) for sg in pending_subgoals) if pending_subgoals else parts.append("    (none — plan ran out)")
    parts.append(
        "Emit ONLY the updated pending subgoals — ids stable where intent "
        "matches the stale list. JSON only."
    )
    return "\n".join(parts)


async def rebuild_subgoals(
    graph: TaskGraph,
    *,
    task_instruction: str,
    current_url: str,
    url_changed: bool,
    vision_resp: Any = None,
) -> tuple[TaskGraph, str]:
    """Replace the REMAINING subgoals of `graph` when the page has
    diverged from the original plan.

    "Adjust the path, not the destination" — done subgoals are kept
    verbatim; only pending ones are rewritten by a text-only LLM call.
    Returns `(new_graph, reason)`. On any failure (no API key, network
    error, malformed JSON, refusal) returns `(graph, "")` so the
    worker hook can skip emitting a replan notice without crashing.

    Trigger guard lives in the caller — we don't gate on `url_changed`
    here beyond including it in the prompt context.
    """
    if not task_instruction or not graph.subgoals:
        return graph, ""

    done = [sg for sg in graph.subgoals.values() if sg.status == "done"]
    pending = [sg for sg in graph.subgoals.values() if sg.status != "done"]
    if not pending:
        return graph, ""

    client, model = _make_text_client()
    if client is None:
        return graph, ""

    try:
        completion = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _REPLAN_SYSTEM},
                {"role": "user", "content": _replan_user_prompt(
                    task_instruction=task_instruction,
                    current_url=current_url,
                    done_subgoals=done,
                    pending_subgoals=pending,
                )},
            ],
            max_tokens=1024,
            temperature=0.1,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        logger.info("task_graph rebuild failed: %s", exc)
        return graph, ""

    raw = (completion.choices[0].message.content or "").strip()
    parsed = _parse_subgoals(raw)
    if not parsed:
        return graph, ""

    # Build the new pending subgoals from the parsed payload, then
    # merge with the preserved done subgoals. Keep insertion order:
    # done first, then new pending. The active_id is set to the first
    # pending if present, else the graph's trailing state (which
    # typically means "task complete").
    rebuilt_pending = _build_graph_from_parsed(parsed)
    new_subgoals: dict[str, Subgoal] = {}
    for sg in done:
        new_subgoals[sg.id] = sg
    for sg_id, sg in rebuilt_pending.subgoals.items():
        # Avoid id collisions with done subgoals — LLM may reuse an id.
        final_id = sg_id
        n = 2
        while final_id in new_subgoals:
            final_id = f"{sg_id}_{n}"
            n += 1
        sg.id = final_id
        new_subgoals[final_id] = sg

    active_id = next(
        (sid for sid, sg in new_subgoals.items() if sg.status != "done"),
        graph.active_id,
    )
    new_graph = TaskGraph(
        subgoals=new_subgoals,
        active_id=active_id,
        history=list(graph.history) + [
            {"event": "replan", "at": active_id, "reason": f"url_changed={url_changed}"},
        ],
    )
    reason = f"replanned remaining subgoals ({len(rebuilt_pending.subgoals)} pending)"
    return new_graph, reason


__all__ = [
    "Signal",
    "Subgoal",
    "Precondition",
    "PreconditionCheck",
    "TaskGraph",
    "decompose_task",
    "trivial_graph",
    "evaluate_signal",
    "updater_check",
    "rebuild_subgoals",
    "DEFAULT_STALE_ACTION_THRESHOLD",
]
