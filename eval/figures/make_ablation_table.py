"""Fill Table 1 (paper/tables/tab1_ablations.tex) from ablation runs.

Reads eval/runs_ablation/ablation__<config>/<task>/seed<k>/{meta.json,
ledgers/*/events.jsonl}, groups by config, and computes per row:

  Success(%)  mean over runs of (judge.success, else heuristic_success) x100
  Tokens/iter pooled  sum(tokens_in+tokens_out) / sum(#iterations)  over the
              harvested Worker events.jsonl of all that config's runs
              (cache tokens excluded; orchestrator events are not harvested)
  Δ to full   Success(config) - Success(full), in percentage points

Each KNOWN row of tab1_ablations.tex is rebuilt from a template every run
(idempotent + refreshable as more runs land). A cell with no data stays
\\todo{value} / \\todo{$\\Delta$} so a partial table still compiles and visibly
flags what's pending. The caption is left byte-identical unless --annotate-n is
passed (then an interim-n note is appended, opt-in, so 2x2 numbers are never
misread as the full 20-task/3-seed sweep).

Usage:  python -m eval.figures.make_ablation_table [--runs ...] [--table ...]
                                                    [--annotate-n] [--verify]
"""
from __future__ import annotations

from .. import _bootstrap  # noqa: F401
from .._bootstrap import REPO_ROOT

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

PAPER = REPO_ROOT.parent / "paper"

# (config name, exact leading row text in the .tex, kind). Order = table order.
ROWS = [
    ("full",        r"\sys{} (full system)",                        "full"),
    ("no_eviction", r"$-$ memory eviction (naive accumulation)",    "delta"),
    ("no_ledger",   r"$-$ structured ledger (rolling buffer only)", "delta"),
    ("no_chevron",  r"$-$ chevron tiebreaker",                      "delta"),
    ("tier1_only",  r"$-$ click cascade Tier-2/3 (Tier-1 only)",    "delta"),
    ("no_humanize", r"$-$ motor humanization",                      "delta"),
    ("no_prefetch", r"$-$ asynchronous vision prefetch",            "delta"),
]


# --- data --------------------------------------------------------------------
def _run_success(meta) -> float:
    j = (meta.get("judge") or {}).get("success")
    if j is not None:
        return 1.0 if j else 0.0
    return 1.0 if meta.get("heuristic_success") else 0.0


def _iter_tokens(seed_dir: Path):
    """(sum_tokens, n_iters, n_zero_iters) from this run's harvested Worker
    events.jsonl iteration events. tokens = tokens_in + tokens_out."""
    total = iters = zeros = 0
    for evt in (seed_dir / "ledgers").glob("*/events.jsonl"):
        try:
            lines = evt.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("type") != "iteration":
                continue
            ti = rec.get("tokens_in") or 0
            to = rec.get("tokens_out") or 0
            iters += 1
            total += ti + to
            if not ti and not to:
                zeros += 1
    return total, iters, zeros


def load_ablation(runs_dir: Path) -> dict:
    by_cfg: dict[str, dict] = {}
    for meta_path in sorted(Path(runs_dir).glob("ablation__*/*/seed*/meta.json")):
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            continue
        label = meta.get("label", "")
        if not label.startswith("ablation__"):
            continue
        cfg = label[len("ablation__"):]
        if meta.get("api_error"):  # never executed (quota/rate-limit) — exclude
            continue
        d = by_cfg.setdefault(cfg, {"succ": [], "tok": 0, "iters": 0, "zero": 0,
                                    "tasks": set(), "seeds": set()})
        d["succ"].append(_run_success(meta))
        d["tasks"].add(meta.get("task_id"))
        d["seeds"].add(meta.get("seed"))
        tok, iters, zero = _iter_tokens(meta_path.parent)
        d["tok"] += tok
        d["iters"] += iters
        d["zero"] += zero
    return by_cfg


def _load_full_run(run_dir: Path):
    """Build the 'full' config bucket from a single full-system run under
    eval/runs/<label>/<task>/seed<k> (e.g. a model-split run). The full system
    is exactly that run, so this fills Table 1's top row with real Success +
    Tokens/iter without needing an ablation__full sweep. Ablation rows stay TODO
    — they measure success WITHOUT a mechanism, a counterfactual no single
    full-system run can supply."""
    run_dir = Path(run_dir)
    meta_path = run_dir / "meta.json"
    if not meta_path.exists():
        print(f"[warn] --full-run: no meta.json under {run_dir}")
        return None
    meta = json.loads(meta_path.read_text())
    if meta.get("api_error"):
        print(f"[warn] --full-run: {run_dir} is an API-error run (brain never ran)")
        return None
    tok, iters, zero = _iter_tokens(run_dir)
    return {"succ": [_run_success(meta)], "tok": tok, "iters": iters, "zero": zero,
            "tasks": {meta.get("task_id")}, "seeds": {meta.get("seed")}}


def _succ_pct(d):
    return 100.0 * sum(d["succ"]) / len(d["succ"]) if d and d["succ"] else None


def _tok_per_iter(d):
    """(tokens_per_iter or None, unreliable_bool). Unreliable when >50% of
    iterations logged zero tokens (some providers return empty usage)."""
    if not d or d["iters"] == 0:
        return None, False
    if d["zero"] / d["iters"] > 0.5:
        return None, True
    return d["tok"] / d["iters"], False


# --- formatting --------------------------------------------------------------
def _fmt_pct(v):
    return f"{v:.0f}"


def _fmt_tok(v):
    return f"{v:,.0f}"


def _fmt_delta(v):
    return "$0$" if abs(v) < 0.5 else f"${v:+.0f}$"


def _build_row(cfg, label, kind, by_cfg) -> str:
    d = by_cfg.get(cfg)
    succ = _succ_pct(d)
    tok, _unreliable = _tok_per_iter(d)
    succ_cell = _fmt_pct(succ) if succ is not None else r"\todo{value}"
    tok_cell = _fmt_tok(tok) if tok is not None else r"\todo{value}"
    if kind == "full":
        delta_cell = "---"
    else:
        full_succ = _succ_pct(by_cfg.get("full"))
        if succ is not None and full_succ is not None:
            delta_cell = _fmt_delta(succ - full_succ)
        else:
            delta_cell = r"\todo{$\Delta$}"
    return f"{label:<45s}& {succ_cell} & {tok_cell} & {delta_cell} \\\\"


# --- standalone-compile verification ----------------------------------------
def _verify(table_tex: str):
    doc = (
        "\\documentclass{article}\n\\usepackage{booktabs}\n\\usepackage{xcolor}\n"
        "\\newcommand{\\sys}{SuperBrowser}\n"
        "\\newcommand{\\todo}[1]{\\textcolor{red}{[#1]}}\n"
        "\\begin{document}\n" + table_tex + "\n\\end{document}\n"
    )
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "v.tex").write_text(doc)
        try:
            subprocess.run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "v.tex"],
                           cwd=td, capture_output=True, text=True, timeout=120)
        except Exception as exc:
            return False, str(exc)
        ok = (Path(td) / "v.pdf").exists()
        return ok, "" if ok else "pdflatex failed"


def main():
    ap = argparse.ArgumentParser(description="Fill Table 1 ablations from runs")
    ap.add_argument("--runs", default=str(REPO_ROOT / "eval" / "runs_ablation"))
    ap.add_argument("--table", default=str(PAPER / "tables" / "tab1_ablations.tex"))
    ap.add_argument("--annotate-n", action="store_true",
                    help="append an interim-n note to the caption (opt-in)")
    ap.add_argument("--full-run", default=None,
                    help="fill the 'full system' row from a single full-system run dir "
                         "under eval/runs/<label>/<task>/seed<k> (e.g. the Claude Opus "
                         "model-split run); ablation rows stay TODO until run_ablations.")
    ap.add_argument("--verify", action="store_true",
                    help="standalone-compile the filled table via pdflatex")
    args = ap.parse_args()

    by_cfg = load_ablation(Path(args.runs))
    if args.full_run:
        full = _load_full_run(Path(args.full_run))
        if full:
            by_cfg["full"] = full   # real full-system run overrides any ablation__full
            print(f"[full-run] {args.full_run}: success={_succ_pct(full):.0f}% "
                  f"tok/iter={(full['tok']/full['iters'] if full['iters'] else 0):,.0f} "
                  f"(iters={full['iters']})")
    if not by_cfg:
        print(f"[error] no ablation runs under {args.runs} and no --full-run. "
              f"Run `python -m eval.run_ablations` first.")
        return

    table_path = Path(args.table)
    lines = table_path.read_text().splitlines()
    out = []
    for line in lines:
        stripped = line.lstrip()
        match = next(((c, l, k) for (c, l, k) in ROWS if stripped.startswith(l)), None)
        if match:
            indent = line[: len(line) - len(stripped)]
            out.append(indent + _build_row(*match, by_cfg))
        elif args.annotate_n and "user-side runs.}" in line:
            full = by_cfg.get("full") or next(iter(by_cfg.values()), None)
            ntask = len(full["tasks"]) if full else 0
            nseed = len(full["seeds"]) if full else 0
            note = (f" (Interim run: {nseed} seed(s) over {ntask} task(s); "
                    f"full 20-task/3-seed sweep pending.)")
            out.append(line.replace("user-side runs.}", "user-side runs." + note + "}"))
        else:
            out.append(line)
    new_text = "\n".join(out) + "\n"
    table_path.write_text(new_text)
    print(f"Wrote {table_path}")

    # console summary + empty-usage guard
    for cfg, label, _k in ROWS:
        d = by_cfg.get(cfg)
        if not d:
            print(f"  {cfg:14s} (no runs — left as TODO)")
            continue
        succ = _succ_pct(d)
        tok, unreliable = _tok_per_iter(d)
        ntask, nseed = len(d["tasks"]), len(d["seeds"])
        succ_s = f"{succ:.0f}%" if succ is not None else "n/a"
        tok_s = f"{tok:,.0f}" if tok is not None else ("UNRELIABLE" if unreliable else "n/a")
        print(f"  {cfg:14s} success={succ_s:>5s}  tok/iter={tok_s:>10s}  "
              f"runs={len(d['succ'])} ({ntask} task x {nseed} seed)")
        if unreliable:
            print(f"    [warn] >50% of {cfg} iterations logged zero tokens "
                  f"(provider returned empty usage); Tokens/iter left as TODO.")

    if args.verify:
        ok, tail = _verify(new_text)
        print(f"  verify table: {'OK' if ok else 'FAIL'}")
        if not ok:
            print(tail)


if __name__ == "__main__":
    main()
