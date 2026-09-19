"""E7 — click recovery ladder ablation (execution robustness across control paths)."""
from eval.core import arms as _arms
from eval.core.experiment import ExperimentSpec

SPEC = ExperimentSpec(
    name="e7_click_cascade",
    title="Click recovery ladder on/off",
    question="How often does the first click path succeed, how often does escalation recover a silent click, and does removing the ladder change task success?",
    arms=lambda args: [_arms.get("ledger"), _arms.get("no_ladder")],
    default_tasks="ablate24",
    # PROTOCOL.md pre-registers only C1/C2 (E2). This study's pair is SECONDARY:
    # reported as diagnostic and Holm-adjusted with the other secondary tests.
    confirmatory=(),
    secondary=(("ledger", "no_ladder"),),
    metrics=("tool_calls", "worker_iterations", "wall_s", "usd"),
    notes=["no_ladder is TS-side (SUPERBROWSER_CLICK_TIERS=tier1): run with --manage-server or restart the server with that env.",
           "Framed as execution robustness across browser control paths, not as bot-detection."],
)
