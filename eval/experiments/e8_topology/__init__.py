"""E8 — role separation: orchestrator->worker delegation vs flat single agent."""
from eval.core import arms as _arms
from eval.core.experiment import ExperimentSpec

SPEC = ExperimentSpec(
    name="e8_topology",
    title="Orchestrator->worker vs flat single agent",
    question="With identical tools, memory hook and budgets, does the strategic orchestrator layer change success, step count, repeated actions or premature completion, and what is its overhead?",
    arms=lambda args: [_arms.get("ledger"), _arms.get("flat")],
    confirmatory=(("ledger", "flat"),),
    metrics=("tool_calls", "worker_iterations", "input_tokens", "wall_s", "usd"),
    notes=["The paper's periodic Planner lives only in the disabled TypeScript agent; the evaluated pipeline has Orchestrator + Worker, hence a topology ablation."],
)
