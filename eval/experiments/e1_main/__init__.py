"""E1 — full-system evaluation on the frozen hard split."""
from eval.core import arms as _arms
from eval.core.experiment import ExperimentSpec

SPEC = ExperimentSpec(
    name="e1_main",
    title="Full system on Online-Mind2Web hard",
    question="What is the task success rate, cost and step profile of the full system on the frozen 74-task hard split, by level and site family?",
    arms=lambda args: [_arms.get("ledger")],
    default_tasks="all",
    default_seeds=1,
    confirmatory=(),
    notes=["This run regenerates the headline number; E0 audits it (k/N, exclusions, 66-vs-74)."],
)
