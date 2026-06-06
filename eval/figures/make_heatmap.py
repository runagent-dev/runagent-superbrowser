"""Generate the §7.4 model-split HEATMAP from eval/results/per_model.csv.

A models × failure-signal heatmap that shows WHICH specific signals drive the
US-vs-Chinese Worker gap. Rows = Worker models (US block then CN block);
columns = the diagnostic signals (task success, schema fidelity, [Vn] index
discipline, procedural share, dead-click rate, and the bad-tool / bad-arg /
prose / stale-index tool-call fractions).

Colour is min--max normalised PER COLUMN and oriented so green = better and
red = worse, uniformly across signals (the "bad" signals are sign-flipped). The
PRINTED cell value is the RAW number (percent), so the reader sees true numbers
while colour encodes the within-column rank. Because normalisation is per
column across only the plotted models, colour is RELATIVE within the plotted
set — which is exactly what makes a small (e.g. 3-model) contrast pop. This is
stated in the figure caption so absolute meaning is never read into a shade.

Pure stdlib -> a hand-built TikZ \\fill/\\node grid. The paper preamble loads
only tikz + pgfplots + groupplots (no `matrix`/`colormaps` library), so the
grid is built from raw \\fill rectangles + \\node labels (the same idiom
make_figure.legend_tikz uses) and compiles against the existing setup. The
output is \\input directly by sections/07_evaluation.tex.

Outputs:
    paper/figures/fig_modelsplit_heatmap.tex

Usage:  python -m eval.figures.make_heatmap [--in ...] [--in-task ...] [--verify]
"""
from __future__ import annotations

from .. import _bootstrap  # noqa: F401
from .._bootstrap import REPO_ROOT
from .make_figure import _f, _read, _is_rescue, _order, _tex_escape, _verify

import argparse
from pathlib import Path

PAPER = REPO_ROOT.parent / "paper"

# (csv key, display label [LaTeX-ready], good_direction).
#   good_direction=+1 : higher is better (green when high)
#   good_direction=-1 : higher is worse  (flipped so red marks the worst)
# frac_* keys are derived in-script as cnt_<bucket> / total_tool_attempts.
SIGNALS = [
    ("task_success_mean",              r"Success",       +1),
    ("schema_fidelity_mean",           r"Schema fid.",   +1),
    ("index_discipline_mean",          r"Index disc.",   +1),
    ("procedural_share_mean",          r"Proc.\ share",  +1),
    ("dead_click_violation_rate_mean", r"Dead-click",    -1),
    ("frac_bad_tool_name",             r"Bad tool",      -1),
    ("frac_bad_arg_name",              r"Bad arg",       -1),
    ("frac_prose_instead_of_call",     r"Prose",         -1),
    ("frac_stale_or_oob_vision_index", r"Stale $[V_n]$", -1),
]

W, H = 1.25, 0.66   # cell size (cm)
_NAME_COLOR = {"US": "blue!55!black", "CN": "orange!65!black"}


def _signal_value(row, key):
    """Raw value of a signal for one model row (frac_* derived from counts)."""
    if key.startswith("frac_"):
        bucket = key[len("frac_"):]
        total = _f(row.get("total_tool_attempts")) or 0
        cnt = _f(row.get(f"cnt_{bucket}")) or 0
        return (cnt / total) if total else None
    return _f(row.get(key))


def _normalizer(values, good_dir):
    """Min--max [0,1] over the column's non-None values; flip when lower=better
    so 1.0 always means "good" (green) and 0.0 "bad" (red)."""
    vals = [v for v in values if v is not None]
    if not vals:
        return lambda v: None
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0

    def nrm(v):
        if v is None:
            return None
        z = (v - lo) / span
        return z if good_dir > 0 else (1.0 - z)

    return nrm


def _cell_fill(z):
    """Diverging red->yellow->green using only xcolor arithmetic (no colormap
    library). z in [0,1]; None -> faint grey 'n/a'."""
    if z is None:
        return "black!8"
    z = max(0.0, min(1.0, z))
    if z >= 0.5:
        p = int(round((z - 0.5) * 2 * 80)) + 10   # 10..90
        return f"green!{p}!yellow"
    q = int(round((0.5 - z) * 2 * 80)) + 10        # 10..90
    return f"red!{q}!yellow"


def _cell_label(raw):
    return "--" if raw is None else f"{raw * 100:.0f}"


def _grid_tikz(models) -> str:
    """One heatmap grid: rows = models (already ordered), cols = SIGNALS.
    Cell colour = per-column normalised rank; cell text = raw percent."""
    if not models:
        return "% (no models to plot)\n"
    nrow = len(models)
    raw = [[_signal_value(m, key) for (key, _l, _d) in SIGNALS] for m in models]
    norms = [
        _normalizer([raw[i][j] for i in range(nrow)], gdir)
        for j, (_k, _l, gdir) in enumerate(SIGNALS)
    ]

    lines = [r"\begin{tikzpicture}[font=\scriptsize]"]
    # rotated column headers, just above the top row
    for j, (_key, label, _d) in enumerate(SIGNALS):
        cx = j * W + W * 0.5
        lines.append(
            f"\\node[rotate=38, anchor=west, font=\\scriptsize] at ({cx:.2f},0.10) {{{label}}};"
        )
    # cells + value labels + (left) row labels, coloured by lab
    for i, m in enumerate(models):
        y0 = -(i + 1) * H
        name = _tex_escape(m.get("short_name", ""))
        ncol = _NAME_COLOR.get(m.get("lab"), "black")
        lines.append(
            f"\\node[anchor=east, font=\\scriptsize, text={ncol}] "
            f"at (-0.14,{y0 + H / 2:.2f}) {{{name}}};"
        )
        for j in range(len(SIGNALS)):
            x0 = j * W
            fill = _cell_fill(norms[j](raw[i][j]))
            lines.append(
                f"\\fill[{fill}, draw=black!25] ({x0:.2f},{y0:.2f}) rectangle ++({W:.2f},{H:.2f});"
            )
            lines.append(
                f"\\node[font=\\scriptsize] at ({x0 + W / 2:.2f},{y0 + H / 2:.2f}) "
                f"{{{_cell_label(raw[i][j])}}};"
            )
    lines.append(r"\end{tikzpicture}")
    return "\n".join(lines) + "\n"


def _legend_tikz() -> str:
    """Colour key + US/CN name-colour note as a raw-TikZ swatch row."""
    lines = [r"\begin{tikzpicture}[font=\scriptsize]"]
    lines.append(r"\node[anchor=west] at (0,0) {Colour (per column, min--max):};")
    x = 4.5
    for color, lab in (("red!90!yellow", "worse"), ("yellow", "mid"),
                       ("green!90!yellow", "better")):
        lines.append(f"\\filldraw[fill={color}, draw=black!40] ({x:.2f},-0.13) rectangle ++(0.32,0.26);")
        lines.append(f"\\node[anchor=west] at ({x + 0.40:.2f},0) {{{lab}}};")
        x += 1.35
    lines.append(f"\\node[anchor=west, text=blue!55!black] at ({x + 0.3:.2f},0) {{US model}};")
    lines.append(f"\\node[anchor=west, text=orange!65!black] at ({x + 2.0:.2f},0) {{CN model}};")
    lines.append(r"\end{tikzpicture}")
    return "\n".join(lines) + "\n"


def heatmap_per_task_tikz(rows) -> str:
    """Optional per-task panels: one grid per task_id, stacked vertically, each
    with a small bold task title above it. Empty string when no usable rows."""
    rows = [r for r in rows if not _is_rescue(r)]
    if not rows:
        return ""
    by_task: dict[str, list[dict]] = {}
    for r in rows:
        by_task.setdefault(r.get("task_id", "?"), []).append(r)
    blocks = []
    for task_id in sorted(by_task):
        models = _order(by_task[task_id])
        title = "{\\small\\bfseries " + _tex_escape(task_id) + "}\\par\\nobreak\\vspace{2pt}\n"
        blocks.append(title + _grid_tikz(models))
    return "\n\\par\\vspace{12pt}\n".join(blocks)


def main():
    p = argparse.ArgumentParser(description="Generate §7.4 model-split heatmap")
    p.add_argument("--in", dest="inp",
                   default=str(REPO_ROOT / "eval" / "results" / "per_model.csv"))
    p.add_argument("--in-task", dest="inp_task",
                   default=str(REPO_ROOT / "eval" / "results" / "per_model_task.csv"))
    p.add_argument("--out", default=str(PAPER / "figures" / "fig_modelsplit_heatmap.tex"))
    p.add_argument("--verify", action="store_true",
                   help="compile the artifact standalone via pdflatex")
    args = p.parse_args()

    rows = _read(Path(args.inp))
    base = _order([r for r in rows if not _is_rescue(r)])

    parts = [
        "% Auto-generated by eval/figures/make_heatmap.py from per_model.csv. Do not hand-edit.\n"
        "% Rows = Worker models (US block then CN block); columns = failure signals.\n"
        "% Colour is min--max normalised PER COLUMN, green=better / red=worse; printed\n"
        "% value is the raw percent. Colour is RELATIVE within the plotted model set.\n"
        "\\centering\n",
        _grid_tikz(base),
        "\\par\\vspace{6pt}\n",
        _legend_tikz(),
    ]

    tp = Path(args.inp_task)
    task_body = heatmap_per_task_tikz(_read(tp)) if tp.exists() else ""
    if task_body:
        parts.append("\\par\\vspace{12pt}\n{\\small\\itshape Per-task breakdown:}\\par\\vspace{4pt}\n")
        parts.append("\\centering\n" + task_body)
    else:
        parts.append("% (no per_model_task.csv yet — per-task panels omitted)\n")

    body = "".join(parts)
    Path(args.out).write_text(body)
    print(f"Wrote {args.out} ({len(base)} models, {len(SIGNALS)} signals)")

    if args.verify:
        ok, tail = _verify(body)
        print(f"  verify heatmap: {'OK' if ok else 'FAIL'}")
        if not ok:
            print(tail)


if __name__ == "__main__":
    main()
