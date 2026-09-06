"""Tool-use DENSITY / contour view (matplotlib) -> one PNG canvas.

A smoothed 2D density companion to the discrete per-model panels. Pools every
executed tool call of a lab into the plane
    x = trajectory progress (0 -> 1, where in the run the call fired)
    y = executed browser tool type (a small fixed, vision-grouped ordering)
builds a 2D histogram, Gaussian-smooths it, and draws FILLED CONTOURS (+ contour
lines) — one panel for US labs, one for the Chinese labs, on a shared scale.

Honesty notes (baked into the caption): tools are categorical, so the y-axis is
an ordinal tool index, not a continuous variable — this is a *smoothed 2D
histogram* drawn as contours, not a true continuous-kernel density. Colour is
RAW pooled call counts on a shared scale, so the Chinese panel reads hotter
because those Workers spend more calls (per-model means 41.2 vs 32.7); the
shape shows *which* tools carry the mass (the vision-grounded
browser_click_at / browser_screenshot band).

Reads eval/runs/<m>/<task>/seed*/ledgers/*/steps.jsonl via the shared loader.

Outputs:
    paper/figures/fig_modeltools_density.png

Usage:
    python -m eval.experiments.modelsplit.figures.make_tool_density_png [--task petfinder_rabbits]
    python -m eval.experiments.modelsplit.figures.make_tool_density_png --pbins 60 --sigma-x 2.2 --dpi 220
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
from matplotlib.colors import PowerNorm
import numpy as np
from scipy.ndimage import gaussian_filter

PAPER = REPO_ROOT.parent / "paper"
LAB_TEXT = {"US": "#1f3fb0", "CN": "#c1440e"}

# Semantic, vision-grouped tool order (vision block first so a contour blob over
# the top rows reads as "vision spend"). Only tools actually present are kept.
TOOL_ORDER = [
    "browser_click_at", "browser_screenshot", "browser_type_at",   # vision-grounded
    "browser_navigate", "browser_open",                            # navigation
    "browser_eval", "browser_run_script", "browser_click",         # procedural
    "browser_type", "browser_verify_fact",                         # misc
]


def _tool_rows(models, top_n):
    present = Counter()
    for m in models:
        present.update(m["seq"])
    ordered = [t for t in TOOL_ORDER if t in present]
    ordered += [t for t, _ in present.most_common() if t not in ordered]  # any stragglers
    keep = ordered[:top_n]
    fold = {t for t in present if t not in keep}
    return keep + (["other"] if fold else []), fold


def _density(models, tools, folded, pbins, sigma_x, sigma_y):
    """Smoothed (tool x progress) 2D histogram of a lab's calls, averaged PER
    MODEL (so a lab with more models isn't hotter just for that; the remaining
    heat difference is genuine per-model call volume)."""
    row_of = {t: r for r, t in enumerate(tools)}
    other_r = row_of.get("other")
    H = np.zeros((len(tools), pbins))
    for m in models:
        L = len(m["seq"]) or 1
        for i, t in enumerate(m["seq"]):
            b = min(pbins - 1, int(i * pbins / L))
            r = row_of.get(t, other_r if t in folded else None)
            if r is not None:
                H[r, b] += 1.0
    H /= max(1, len(models))
    return gaussian_filter(H, sigma=(sigma_y, sigma_x), mode="nearest")


def render(models, tools, folded, out_png, task, pbins, sigma_x, sigma_y, dpi):
    ordered = _order(models)
    groups = [("US", [m for m in ordered if m["lab"] == "US"]),
              ("Chinese", [m for m in ordered if m["lab"] == "CN"])]
    groups = [(name, g) for name, g in groups if g]
    dens = {name: _density(g, tools, folded, pbins, sigma_x, sigma_y) for name, g in groups}
    vmax = max((d.max() for d in dens.values()), default=1.0) or 1.0
    ylabels = [t if t == "other" else t.replace("browser_", "") for t in tools]
    xs = (np.arange(pbins) + 0.5) / pbins
    ys = np.arange(len(tools))
    X, Y = np.meshgrid(xs, ys)

    fig, axes = plt.subplots(1, len(groups), figsize=(4.9 * len(groups) + 0.8, 4.7),
                             sharey=True)
    if len(groups) == 1:
        axes = [axes]
    # PowerNorm (gamma<1) stretches the low-density range so the blue→red
    # gradient is readable instead of a flat blue field with one red dot.
    norm = PowerNorm(gamma=0.7, vmin=0.0, vmax=vmax)
    mesh = None
    for ax, (name, g) in zip(axes, groups):
        Z = dens[name]
        mesh = ax.pcolormesh(X, Y, Z, cmap="coolwarm", norm=norm,
                             shading="gouraud", rasterized=True)
        ax.contour(X, Y, Z, levels=np.linspace(vmax * 0.3, vmax, 4),
                   colors="k", linewidths=0.5, alpha=0.28)
        calls = sum(m["total"] for m in g)
        ax.set_title(f"{name} labs  ({len(g)} models, {calls} calls)",
                     color=LAB_TEXT["US" if name == "US" else "CN"], fontsize=11, fontweight="bold")
        ax.set_xlabel("trajectory progress  (start → end)", fontsize=9)
        ax.set_xticks([0, 0.5, 1.0])
        ax.set_yticks(ys)
        ax.set_yticklabels(ylabels, fontsize=8.5)
        ax.set_ylim(len(tools) - 0.5, -0.5)   # row 0 (vision) at the TOP
        ax.tick_params(length=0)
        ax.set_xlim(0, 1)

    cb = fig.colorbar(mesh, ax=axes, shrink=0.85, pad=0.02, aspect=22)
    cb.set_label("mean calls per model, per slice  (blue = few → red = many)", fontsize=8.5)
    cb.ax.tick_params(labelsize=7)

    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=dpi, bbox_inches="tight")
    plt.close(fig)


def main():
    p = argparse.ArgumentParser(description="§7.4 tool-use density / contour view (PNG)")
    p.add_argument("--runs", default=str(REPO_ROOT / "eval" / "runs"))
    p.add_argument("--task", default="petfinder_rabbits", help="task id, or 'all'")
    p.add_argument("--out", default=str(PAPER / "figures" / "fig_modeltools_density.png"))
    p.add_argument("--top", type=int, default=7, help="tool rows shown; rest folded into 'other'")
    p.add_argument("--pbins", type=int, default=60, help="progress bins along x before smoothing")
    p.add_argument("--sigma-x", type=float, default=2.4, help="Gaussian sigma along progress (bins)")
    p.add_argument("--sigma-y", type=float, default=0.7, help="Gaussian sigma along tools (rows)")
    p.add_argument("--dpi", type=int, default=200)
    args = p.parse_args()

    models = load_tool_sequences(Path(args.runs), args.task)
    if not models:
        print(f"[error] no runs found under {args.runs} for task={args.task!r}.")
        return
    tools, folded = _tool_rows(models, args.top)
    render(models, tools, folded, args.out, args.task, args.pbins, args.sigma_x, args.sigma_y, args.dpi)
    print(f"Wrote {args.out} ({len(tools)} tool rows, {args.pbins} progress bins; task={args.task})")


if __name__ == "__main__":
    main()
