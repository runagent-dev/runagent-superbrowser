"""E10 — budget sweep, seed variance and cross-model check for the memory comparison."""
from eval.core import arms as _arms
from eval.core.experiment import ExperimentSpec

BUDGETS = (1024, 2048, 3072)      # 0.5x, 1x, 1.5x of the 2048-token history budget
WINDOWS = (3, 5, 8)               # recent-window K for fifo (0.6x, 1x, 1.6x)
BASES = ("ledger", "fifo", "summary")


def _arms_for(args):
    mode = getattr(args, "sweep", "budget")
    bases = [b.strip() for b in str(getattr(args, "bases", ",".join(BASES))).split(",") if b.strip()]
    out = []
    if mode in ("budget", "both"):
        for b in bases:
            for B in BUDGETS:
                out.append(_arms.budget_arm(b, budget_tokens=B))
    if mode in ("window", "both"):
        for b in bases:
            for K in WINDOWS:
                out.append(_arms.budget_arm(b, recent_k=K))
    if mode == "seeds":
        out = [_arms.get(b) for b in bases]
    return out


def _extra_args(ap):
    ap.add_argument("--sweep", choices=("budget", "window", "both", "seeds"), default="budget",
                    help="budget: B in {1024,2048,3072}; window: K in {3,5,8}; seeds: plain arms (use --seeds 3)")
    ap.add_argument("--bases", default=",".join(BASES))


SPEC = ExperimentSpec(
    name="e10_robustness",
    title="Budget sweep, seed variance, cross-model check",
    question="Does the memory result hold across history budgets, repeated seeds and a second host model, and where does bounded memory lose?",
    arms=_arms_for,
    default_tasks="ablation24",
    default_seeds=1,
    confirmatory=(),
    metrics=("tool_calls", "prompt_peak", "input_tokens", "usd"),
    extra_args=_extra_args,
    notes=["Cross-model: rerun with --model <other id>; the analyzer groups by protocol.model.",
           "Seeds: --sweep seeds --seeds 3 (reports mean +- SE per arm)."],
)
