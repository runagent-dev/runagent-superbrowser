"""Render the §7.4 model-wise tool-economy heatmap as a PNG (matplotlib/seaborn).

The matplotlib/seaborn analog of make_tool_heatmap.py (which emits TikZ). ONE
canvas, all models: rows = Worker models (US block then Chinese block, each
followed by a per-block mean), columns = the executed browser tool types
(most-used first) plus a separated TOTAL column. Reads the EXECUTED step ledgers
    eval/runs/<label>/<task>/seed*/ledgers/*/steps.jsonl
directly (via the shared loader in make_tool_heatmap.py) and counts tool calls
per model.

Colour is min--max normalised PER COLUMN over the per-model rows (mean rows are
mapped through the same scale, then clamped), so each tool's heavy vs.\ light
users pop and the TOTAL column's different magnitude doesn't wash everything
out; the printed number in every cell is the RAW mean calls per run. cmap is a
blue->red diverging map (blue = few = economical, red = many). US Workers finish
in fewer calls (blue Total) than the recent large Chinese models (Kimi-K2.6 the
red outlier), which over-call the vision-grounded browser_click_at path.

Outputs:
    paper/figures/fig_modeltools_heatmap.png
    eval/results/per_model_tools.csv   (raw counts, via the shared loader)

Usage:
    python -m eval.experiments.modelsplit.figures.make_tool_heatmap_png [--task petfinder_rabbits] [--top 6]
    python -m eval.experiments.modelsplit.figures.make_tool_heatmap_png --task all --dpi 220
"""
from __future__ import annotations

from eval import _bootstrap  # noqa: F401
from eval._bootstrap import REPO_ROOT
from .make_tool_heatmap import load_tool_counts, _order, _columns, _col_value, _mean_row, _write_csv

import argparse
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # headless
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import numpy as np
import seaborn as sns

PAPER = REPO_ROOT.parent / "paper"

LAB_TEXT = {"US": "#1f3fb0", "CN": "#c1440e"}   # navy / burnt-orange
TOTAL_LABEL = "Total"


def _rows_in_order(models: list[dict]) -> tuple[list[dict], list[int]]:
    """US models, US mean, CN models, CN mean, (others). Returns (rows,
    indices-of-mean-rows) so the caller can draw a heavier rule under each."""
    ordered = _order(models)
    rows: list[dict] = []
    mean_idx: list[int] = []
    for lab in ("US", "CN"):
        grp = [m for m in ordered if m["lab"] == lab]
        rows.extend(grp)
        mr = _mean_row(ordered, lab)
        if mr:
            rows.append(mr)
            mean_idx.append(len(rows) - 1)
    rows.extend([m for m in ordered if m["lab"] not in ("US", "CN")])
    return rows, mean_idx


def _matrices(rows, cols, folded, scale="column"):
    """raw[r][c] counts and a min--max normalised colour matrix (normalised over
    MODEL rows only; mean rows mapped through + clamped).

    scale='column' : each column scaled by its own min/max -> reveals each tool's
                     heavy vs. light users; magnitudes NOT comparable across cols.
    scale='global' : the TOOL cells share one scale (magnitudes comparable; e.g.
                     click_at=23 is redder than eval=9); the TOTAL column keeps
                     its own scale (its magnitude is an order larger)."""
    ncol = len(cols) + 1  # + TOTAL
    raw = np.zeros((len(rows), ncol))
    for i, m in enumerate(rows):
        for j, c in enumerate(cols):
            raw[i, j] = _col_value(m, c, folded)
        raw[i, -1] = m["total"]

    norm = np.full_like(raw, 0.5)
    model_mask = [not r.get("is_mean") for r in rows]

    def _scale_cols(js):
        block = raw[np.ix_(model_mask, js)]
        lo, hi = (block.min(), block.max()) if block.size else (0.0, 1.0)
        span = (hi - lo) or 1.0
        for j in js:
            norm[:, j] = np.clip((raw[:, j] - lo) / span, 0.0, 1.0)

    if scale == "global":
        _scale_cols(list(range(ncol - 1)))   # all tool columns share one scale
        _scale_cols([ncol - 1])               # TOTAL keeps its own
    else:
        for j in range(ncol):
            _scale_cols([j])
    return raw, norm


def _annot(raw, rows):
    out = np.empty(raw.shape, dtype=object)
    for i in range(raw.shape[0]):
        is_mean = rows[i].get("is_mean", False)
        for j in range(raw.shape[1]):
            v = raw[i, j]
            if v == 0 and not is_mean:
                out[i, j] = ""
            elif abs(v - round(v)) < 0.05:
                out[i, j] = f"{v:.0f}"
            else:
                out[i, j] = f"{v:.1f}"
    return out


def render(models, cols, folded, out_png: Path, task: str, dpi: int, scale: str = "column"):
    rows, mean_idx = _rows_in_order(models)
    raw, norm = _matrices(rows, cols, folded, scale=scale)
    annot = _annot(raw, rows)
    col_labels = [c if c == "other" else c.replace("browser_", "") for c in cols] + [TOTAL_LABEL]
    row_labels = [m["short_name"] for m in rows]
    nrow, ncol = raw.shape

    fig_w = 1.05 + 0.92 * ncol
    fig_h = 1.20 + 0.52 * nrow
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))

    cbar_label = ("calls, normalised per column  (blue = few → red = many)"
                  if scale == "column" else
                  "calls, shared tool scale  (blue = few → red = many)")
    sns.heatmap(
        norm, annot=annot, fmt="", cmap="coolwarm", vmin=0.0, vmax=1.0,
        linewidths=1.2, linecolor="white", square=False, ax=ax,
        cbar_kws={"label": cbar_label, "shrink": 0.55, "pad": 0.02, "aspect": 18},
        annot_kws={"fontsize": 9},
    )
    ax.set_xticklabels(col_labels, rotation=35, ha="right", fontsize=9)
    ax.set_yticklabels(row_labels, rotation=0, fontsize=9)
    ax.tick_params(left=False, bottom=False)
    for tick, m in zip(ax.get_yticklabels(), rows):
        tick.set_color(LAB_TEXT.get(m["lab"], "black"))
        if m.get("is_mean"):
            tick.set_fontweight("bold")

    # heavier rule under each block mean row; a thin border around the TOTAL col
    for i in mean_idx:
        ax.axhline(i + 1, color="black", lw=1.6)
    ax.add_patch(Rectangle((ncol - 1, 0), 1, nrow, fill=False, edgecolor="black", lw=1.8))
    ax.axvline(ncol - 1, color="black", lw=1.2)

    ax.set_title(
        f"Worker tool-call economy on  {task}  (executed steps.jsonl; only the Worker model varies)",
        fontsize=10.5, pad=10,
    )
    # US / CN colour key
    fig.text(0.012, 0.5, "US labs", color=LAB_TEXT["US"], fontsize=9,
             rotation=90, va="center", fontweight="bold")
    fig.text(0.012, 0.18, "Chinese labs", color=LAB_TEXT["CN"], fontsize=9,
             rotation=90, va="center", fontweight="bold")

    fig.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=dpi, bbox_inches="tight")
    plt.close(fig)


def main():
    p = argparse.ArgumentParser(description="§7.4 tool-economy heatmap as PNG (matplotlib/seaborn)")
    p.add_argument("--runs", default=str(REPO_ROOT / "eval" / "runs"))
    p.add_argument("--task", default="petfinder_rabbits", help="task id, or 'all' to pool")
    p.add_argument("--out", default=str(PAPER / "figures" / "fig_modeltools_heatmap.png"))
    p.add_argument("--csv", default=str(REPO_ROOT / "eval" / "results" / "per_model_tools.csv"))
    p.add_argument("--top", type=int, default=6, help="show the N most-used tools; fold the rest into 'other'")
    p.add_argument("--scale", choices=("column", "global"), default="column",
                   help="'column' = colour each tool by its own min/max (reveals heavy users); "
                        "'global' = one scale across tool cells (magnitudes comparable)")
    p.add_argument("--dpi", type=int, default=200)
    args = p.parse_args()

    models = load_tool_counts(Path(args.runs), args.task)
    if not models:
        print(f"[error] no runs found under {args.runs} for task={args.task!r}.")
        return
    cols, folded = _columns(models, args.top)
    _write_csv(models, cols, folded, Path(args.csv))
    render(models, cols, folded, Path(args.out), args.task, args.dpi, scale=args.scale)
    print(f"Wrote {args.out} ({len(models)} models, {len(cols)} tool cols + Total; "
          f"task={args.task}, scale={args.scale}, dpi={args.dpi})")
    print(f"Wrote {args.csv}")
    for m in _order(models):
        print(f"  {m['short_name']:18s} lab={m['lab']:3s} total={m['total']:.1f}")


if __name__ == "__main__":
    main()
