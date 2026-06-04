"""Model registry for the §7.4 evaluation — EDIT THE NUMBERS.

Maps the model id you put in ``~/.nanobot/config.json`` (``agents.defaults.model``)
to:
  - a short display name,
  - lab origin ("US" or "CN") — the axis of the paper's claim,
  - ``bench_composite``: a normalized 0-100 mean of a FIXED public reasoning
    benchmark set (e.g. MMLU-Pro / GPQA / LiveCodeBench / AIME-class), the x-axis
    of the dissociation scatter,
  - ``sources``: the citation(s) for that composite.

ACTION REQUIRED before the figure is publishable:
  1. Make sure a ``match`` substring resolves each model id you actually run.
  2. Replace each ``bench_composite`` with a REAL number from a cited source and
     flip ``confirmed=True``. Use the SAME benchmark set for every model.
  3. The figure renders a "PLACEHOLDER benchmarks" watermark while any plotted
     model is unconfirmed — so unfilled numbers can never silently ship.

Matching is by case-insensitive substring against the config model id; the
longest matching ``match`` wins (so "gpt-5-mini" beats "gpt-5").
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ModelSpec:
    match: str           # case-insensitive substring matched against the config model id
    short_name: str      # display label on the figure/table
    lab: str             # "US" or "CN"
    bench_composite: float  # normalized 0-100 public reasoning composite (x-axis)
    sources: str         # citation for the composite
    confirmed: bool = False  # flip to True once bench_composite is a real cited number


# NOTE: every bench_composite below is a PLACEHOLDER (confirmed=False). Replace
# with cited numbers for the exact models you run. Names reflect the paper's
# "-class" slots; adjust `match`/`short_name` to your actual model ids.
MODELS: list[ModelSpec] = [
    # ---- US labs ----
    ModelSpec("gpt-5-mini", "GPT-5-mini", "US", 74.0, "OpenAI model card [CONFIRM]"),
    ModelSpec("gpt-5.5",    "GPT-5.5",    "US", 82.0, "OpenAI model card [CONFIRM]"),
    ModelSpec("gpt-5",      "GPT-5",      "US", 80.0, "OpenAI model card [CONFIRM]"),
    ModelSpec("claude",     "Claude",     "US", 83.0, "Anthropic model card [CONFIRM]"),
    ModelSpec("gemini",     "Gemini 3",   "US", 81.0, "Google model card [CONFIRM]"),
    ModelSpec("nemotron",   "Nemotron",   "US", 72.0, "NVIDIA model card [CONFIRM]"),
    # ---- Chinese labs ----
    ModelSpec("kimi",       "Kimi K2",    "CN", 81.0, "Moonshot model card [CONFIRM]"),
    ModelSpec("moonshot",   "Kimi K2",    "CN", 81.0, "Moonshot model card [CONFIRM]"),
    ModelSpec("qwen",       "Qwen3-Max",  "CN", 83.0, "Alibaba model card [CONFIRM]"),
    ModelSpec("glm",        "GLM",        "CN", 80.0, "Zhipu model card [CONFIRM]"),
    ModelSpec("zhipu",      "GLM",        "CN", 80.0, "Zhipu model card [CONFIRM]"),
    ModelSpec("z-ai",       "GLM",        "CN", 80.0, "Zhipu model card [CONFIRM]"),
]


def resolve(model_id: str) -> ModelSpec | None:
    """Resolve a config model id to its ModelSpec (longest substring match wins)."""
    mid = (model_id or "").lower()
    best: ModelSpec | None = None
    for spec in MODELS:
        if spec.match.lower() in mid:
            if best is None or len(spec.match) > len(best.match):
                best = spec
    return best
