"""E2 — matched memory-policy ablation (the main experiment)."""
from eval.core import arms as _arms
from eval.core.experiment import ExperimentSpec


def _arms_for(args):
    names = ["ledger", "fifo", "summary", "full_history"]
    if getattr(args, "with_noevict", False):
        names.append("ledger_noevict")
    return [_arms.get(n) for n in names]


def _extra_args(ap):
    ap.add_argument("--with-noevict", action="store_true", help="add the ledger_noevict diagnostic arm")


SPEC = ExperimentSpec(
    name="e2_memory_policy",
    title="Matched memory policies: full history / FIFO / LLM summary / Ledger",
    question="Holding model, prompts, vision tier, click system, task ids, budgets and evaluator fixed, does the structured Ledger + eviction retain task-critical state and succeed more often than recency or LLM compression, and at what cost relative to full history?",
    arms=_arms_for,
    default_tasks="ablate24",   # the subset the reported sweep ran (see PROTOCOL.md "As-run deviations")
    default_seeds=1,
    confirmatory=(("ledger", "fifo"), ("ledger", "full_history")),
    secondary=(("ledger", "summary"), ("summary", "fifo"), ("ledger", "ledger_noevict")),
    metrics=("tool_calls", "worker_iterations", "vision_calls", "prompt_mean", "prompt_peak", "input_tokens", "wall_s", "usd", "usd_cached"),
    extra_args=_extra_args,
    notes=["C1: ledger vs fifo on TSR (McNemar). C2: ledger vs full_history on cost with non-inferior TSR.",
           "Run with --seeds 3 for the paper; the summary arm books its compressor calls under usage role 'compressor'."],
)
