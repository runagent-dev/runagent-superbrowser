"""E10 — budget sweep, seed variance and cross-model check for the memory comparison."""
from eval.core import arms as _arms
from eval.core.experiment import ExperimentSpec

BUDGETS = (1024, 2048, 3072)      # 0.5x, 1x, 1.5x of the 2048-token history budget
WINDOWS = (3, 5, 8)               # verbatim recent window K (0.6x, 1x, 1.6x)
# The token budget B only affects policies that hold a budgeted artefact (the
# summary block, the rendered Ledger); FIFO has none, so it is swept over K only.
BUDGET_BASES = ("ledger", "summary")
WINDOW_BASES = ("fifo", "ledger", "summary")
SEED_BASES = ("ledger", "fifo", "summary", "full_history")


def _split(v, default):
    raw = str(v) if v else ""
    return [b.strip() for b in raw.split(",") if b.strip()] or list(default)


def _arms_for(args):
    mode = getattr(args, "sweep", "budget")
    bases = getattr(args, "bases", None)
    out = []
    if mode in ("budget", "both"):
        for b in _split(bases, BUDGET_BASES):
            for B in BUDGETS:
                out.append(_arms.budget_arm(b, budget_tokens=B))
    if mode in ("window", "both"):
        for b in _split(bases, WINDOW_BASES):
            for K in WINDOWS:
                out.append(_arms.budget_arm(b, recent_k=K))
    if mode == "seeds":
        out = [_arms.get(b) for b in _split(bases, SEED_BASES)]
    return out


def _extra_args(ap):
    ap.add_argument("--sweep", choices=("budget", "window", "both", "seeds"), default="budget",
                    help="budget: B in {1024,2048,3072} for ledger+summary; window: K in {3,5,8} for fifo+ledger+summary; "
                         "seeds: plain arms (use --seeds 3)")
    ap.add_argument("--bases", default=None, help="override the per-sweep default policy list (comma-separated)")


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
           "Seeds: --sweep seeds --seeds 3 (reports mean +- SE per arm).",
           "B is swept for ledger+summary only (FIFO holds no budgeted artefact); K for fifo+ledger+summary."],
)
