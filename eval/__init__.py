"""SuperBrowser §7.4 model-family evaluation harness.

Reproduces the paper's claim that recent large Chinese models degrade on
web-navigation in the Worker slot despite strong public reasoning benchmarks,
and produces the figure + table for sections/07_evaluation.tex.

Entry points (run from the repo root):
    python -m eval.run_eval                 # batch full-orchestrator runs
    python -m eval.analyzer                 # transcripts -> metrics CSVs
    python -m eval.figures.make_figure      # figure + table
    python -m eval.figures.make_appendix_traces

Importing this package first runs eval._bootstrap (sys.path + .env setup).
"""
from . import _bootstrap  # noqa: F401  (side effects: sys.path + .env)
