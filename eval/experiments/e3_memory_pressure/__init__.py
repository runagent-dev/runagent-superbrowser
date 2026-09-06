"""E3 — memory-pressure perturbation ladder x memory policy."""
from eval.core import arms as _arms
from eval.core.experiment import ExperimentSpec

LEVELS = (0, 1500, 4000)          # distractor tokens appended per step
BASES = ("fifo", "summary", "ledger")


def _arms_for(args):
    levels = [int(x) for x in str(getattr(args, "levels", ",".join(map(str, LEVELS)))).split(",") if x.strip()]
    bases = [b.strip() for b in str(getattr(args, "bases", ",".join(BASES))).split(",") if b.strip()]
    return [_arms.pressure_arm(b, lv) for b in bases for lv in levels]


def _extra_args(ap):
    ap.add_argument("--levels", default=",".join(map(str, LEVELS)), help="distractor tokens per step (comma-separated)")
    ap.add_argument("--bases", default=",".join(BASES), help="memory policies to ladder (comma-separated)")


SPEC = ExperimentSpec(
    name="e3_memory_pressure",
    title="Memory-pressure ladder (distractor tokens per step) x memory policy",
    question="As irrelevant observation volume grows with task semantics fixed, do FIFO and LLM-summary lose task-critical state faster than the structured Ledger (flatter CSD/TSR degradation)?",
    arms=_arms_for,
    default_tasks="ablation24",
    default_seeds=1,
    confirmatory=(),
    secondary=tuple((f"ledger__p{lv}", f"fifo__p{lv}") for lv in LEVELS) + tuple((f"ledger__p{lv}", f"summary__p{lv}") for lv in LEVELS),
    metrics=("tool_calls", "worker_iterations", "prompt_peak", "input_tokens", "usd"),
    extra_args=_extra_args,
    notes=["Construct-validity ladder in the AgentCollabBench sense: each metric must move in the predicted direction with pressure."],
)
