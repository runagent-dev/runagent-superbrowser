"""E4 — dead-end memory ablation."""
from eval.core import arms as _arms
from eval.core.experiment import ExperimentSpec

SPEC = ExperimentSpec(
    name="e4_deadend",
    title="Dead-end memory on/off",
    question="Does remembering failures as first-class dead-ends reduce revisits of failed states and step inflation, holding everything else fixed?",
    arms=lambda args: [_arms.get("ledger"), _arms.get("no_deadend")],
    default_tasks="ablate24",
    # PROTOCOL.md pre-registers only C1/C2 (E2). This study's pair is SECONDARY:
    # reported as diagnostic and Holm-adjusted with the other secondary tests.
    confirmatory=(),
    secondary=(("ledger", "no_deadend"),),
    metrics=("tool_calls", "worker_iterations", "vision_calls", "wall_s", "usd"),
    notes=["Primary process metric: Dead-End Revisit Rate (revisits / distinct failed action signatures)."],
)
