"""Per-model tool-use TRAJECTORY panels (matplotlib) -> one PNG canvas.

A small-multiples companion to the aggregate model x tool grid: ONE heatmap
PANEL PER MODEL, all on one canvas. Within each panel the second axis is the
trajectory itself, so you see not just HOW MANY calls a model spends but WHEN /
in what pattern:
    y-axis = executed browser tool types (shared across panels, most-used on top)
    x-axis = the run binned into equal slices of its trajectory (0 -> 100%)
    cell   = number of times that tool fired in that slice
Colour is one GLOBAL blue->red scale shared by every panel (blue = few, red =
many), so a red band means a model is hammering that tool in that phase — e.g.
the recent large Chinese models show sustained red in the vision rows
(browser_screenshot / browser_click_at): redundant vision spend, looped across
the whole trajectory. US panels are sparser and finish sooner (fewer calls,
shown in each panel title). Reads the executed step ledgers
(eval/runs/<m>/<task>/seed*/ledgers/*/steps.jsonl) via the shared loader.

Outputs:
    paper/figures/fig_modeltools_panels.png

Usage:
    python -m eval.experiments.modelsplit.figures.make_tool_trajectory_png [--task petfinder_rabbits]
    python -m eval.experiments.modelsplit.figures.make_tool_trajectory_png --bins 10 --top 8 --dpi 220
"""
from __future__ import annotations

from eval import _bootstrap  # noqa: F401
from eval._bootstrap import REPO_ROOT
from .make_tool_heatmap import load_tool_sequences, _order

import argparse
from collections import Counter
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec
import numpy as np

PAPER = REPO_ROOT.parent / "paper"
LAB_TEXT = {"US": "#1f3fb0", "CN": "#c1440e"}


def _tool_axis(models: list[dict], top_n: int) -> tuple[list[str], set]:
    """Shared y-axis: the top_n tools by pooled frequency (most-used FIRST, so it
    lands at the top row), with the rest folded into a trailing 'other'."""
    pooled: Counter = Counter()
    for m in models:
        pooled.update(m["seq"])
    keep = [t for t, _ in pooled.most_common(top_n)]
    fold = {t for t in pooled if t not in keep}
    return (keep + (["other"] if fold else [])), fold


def _matrix(seq: list[str], tools: list[str], folded: set, nbins: int) -> np.ndarray:
    """tools x nbins counts: step i falls in bin floor(i*nbins/len)."""
    row_of = {t: r for r, t in enumerate(tools)}
    other_r = row_of.get("other")
    mat = np.zeros((len(tools), nbins))
    L = len(seq) or 1
    for i, t in enumerate(seq):
        b = min(nbins - 1, int(i * nbins / L))
        r = row_of.get(t, other_r if t in folded else None)
        if r is not None:
            mat[r, b] += 1
    return mat


def render(models, tools, folded, out_png: Path, task: str, nbins: int, dpi: int):
    ordered = _order(models)
    mats = {m["model_label"]: _matrix(m["seq"], tools, folded, nbins) for m in ordered}
    gmax = max((mt.max() for mt in mats.values()), default=1.0) or 1.0
    ylabels = [t if t == "other" else t.replace("browser_", "") for t in tools]
    us = [m for m in ordered if m["lab"] == "US"]
    cn = [m for m in ordered if m["lab"] == "CN"]
    other = [m for m in ordered if m["lab"] not in ("US", "CN")]
    ncols = max(len(us), len(cn), 1)

    fig_w = 1.4 + 2.05 * ncols
    fig_h = 1.2 + 2.35 * (2 if (us and (cn or other)) else 1)
    fig = plt.figure(figsize=(fig_w, fig_h))
    gs = GridSpec(2, ncols + 1, figure=fig, width_ratios=[1] * ncols + [0.16],
                  hspace=0.5, wspace=0.16)

    im = None

    def _panel(ax, m, show_y):
        nonlocal im
        mt = mats[m["model_label"]]
        im = ax.imshow(mt, aspect="auto", cmap="coolwarm", vmin=0, vmax=gmax)
        ax.set_title(f"{m['short_name']}\n{m['total']} calls",
                     color=LAB_TEXT.get(m["lab"], "black"), fontsize=9.5, fontweight="bold")
        ax.set_xticks([-0.5, nbins - 0.5])
        ax.set_xticklabels(["start", "end"], fontsize=7)
        ax.set_yticks(range(len(tools)))
        if show_y:
            ax.set_yticklabels(ylabels, fontsize=8)
        else:
            ax.set_yticklabels([])
        ax.tick_params(length=0)
        for s in ax.spines.values():
            s.set_edgecolor("black"); s.set_linewidth(0.8)
        # annotate non-zero counts
        for r in range(mt.shape[0]):
            for c in range(mt.shape[1]):
                v = mt[r, c]
                if v > 0:
                    ax.text(c, r, f"{int(v)}", ha="center", va="center",
                            fontsize=6.0, color="white" if v / gmax > 0.6 else "black")

    rows_groups = [us]
    if cn or other:
        rows_groups.append(cn + other)
    for ri, grp in enumerate(rows_groups):
        for ci, m in enumerate(grp):
            ax = fig.add_subplot(gs[ri, ci])
            _panel(ax, m, show_y=(ci == 0))

    # shared colorbar in the right-hand spare column
    cax = fig.add_subplot(gs[:, ncols])
    cb = fig.colorbar(im, cax=cax)
    cb.set_label("tool calls per trajectory slice  (blue = few → red = many)", fontsize=8)
    cb.ax.tick_params(labelsize=7)

    fig.suptitle(
        "Per-model Worker tool-use over the trajectory "
        "(executed step ledgers; US labs blue, Chinese labs orange)",
        fontsize=11, y=0.995,
    )
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=dpi, bbox_inches="tight")
    plt.close(fig)


def main():
    p = argparse.ArgumentParser(description="§7.4 per-model tool-use trajectory panels (PNG)")
    p.add_argument("--runs", default=str(REPO_ROOT / "eval" / "runs"))
    p.add_argument("--task", default="petfinder_rabbits", help="task id, or 'all'")
    p.add_argument("--out", default=str(PAPER / "figures" / "fig_modeltools_panels.png"))
    p.add_argument("--bins", type=int, default=12, help="trajectory slices per panel (x-axis)")
    p.add_argument("--top", type=int, default=8, help="tool rows shown; rest folded into 'other'")
    p.add_argument("--dpi", type=int, default=200)
    args = p.parse_args()

    models = load_tool_sequences(Path(args.runs), args.task)
    if not models:
        print(f"[error] no runs found under {args.runs} for task={args.task!r}.")
        return
    tools, folded = _tool_axis(models, args.top)
    render(models, tools, folded, Path(args.out), args.task, args.bins, args.dpi)
    print(f"Wrote {args.out} ({len(models)} model panels, {len(tools)} tool rows, "
          f"{args.bins} bins; task={args.task}, dpi={args.dpi})")
    for m in _order(models):
        print(f"  {m['short_name']:18s} lab={m['lab']:3s} steps={m['total']}")


if __name__ == "__main__":
    main()
