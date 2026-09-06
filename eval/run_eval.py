"""Compatibility shim — the §7.4 model-split harness moved.

The research evaluation suite was modularised (see eval/README.md). The old
entry points live under ``eval.experiments.modelsplit``:

    python -m eval.experiments.modelsplit.run_eval ...
    python -m eval.experiments.modelsplit.analyzer ...
    python -m eval.experiments.modelsplit.run_ablations ...

New experiments (E0-E12) are ``python -m eval.experiments.<name>.run``.
"""
import sys

if __name__ == "__main__":
    sys.stderr.write(__doc__)
    sys.exit(2)
