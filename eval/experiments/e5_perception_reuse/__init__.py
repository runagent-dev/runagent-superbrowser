"""E5 — perception reuse (DOM-cache / prefetch / epoch) ablation."""
from eval.core import arms as _arms
from eval.core.experiment import ExperimentSpec

SPEC = ExperimentSpec(
    name="e5_perception_reuse",
    title="Perception reuse on/off",
    question="How much redundant perception (vision calls, cost, latency) does reuse of grounded perception save, and does the saving shrink on pages that change more?",
    arms=lambda args: [_arms.get("ledger"), _arms.get("fresh_vision")],
    confirmatory=(("ledger", "fresh_vision"),),
    metrics=("vision_calls", "tool_calls", "worker_iterations", "wall_s", "usd"),
    notes=["Page-churn strata are measured from the vision fingerprints (fraction of consecutive vision passes with a changed DOM hash) on the ledger arm."],
)
