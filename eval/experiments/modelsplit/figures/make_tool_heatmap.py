"""Generate the §7.4 model-wise TOOL-ECONOMY heatmap from the step ledgers.

This is the "how many tool calls did each Worker spend to do the job" view —
complementary to the failure-signal heatmap (make_heatmap.py). It reads the
EXECUTED tool steps recorded in
    eval/runs/<label>/<task>/seed*/ledgers/*/steps.jsonl
(one JSON object per executed browser tool call), counts them per tool type per
model, and renders a models x tool-type grid:

  * Rows  = Worker models, US block then Chinese block, each block followed by a
            bold per-block MEAN row. Row labels are coloured by lab.
  * Cols  = the executed browser tool types (most-used first), then a separated,
            bold TOTAL column.
  * Tool-type cells use a sequential BLUE scale (per column, light=few calls ->
    dark=many) — a valence-free "behavioural profile": it shows WHERE each model
    spends calls (e.g. the vision-grounded browser_click_at / browser_screenshot
    path) without asserting good/bad, since the cheap procedural path (eval,
    run_script, click) is *good* to use more of.
  * The TOTAL column uses a diverging GREEN->RED scale (per column, green=fewer
    calls = more economical -> red=more) — this is the headline: US Workers
    reach the goal in fewer calls than the recent large Chinese models, which
    over-call the vision-grounded path (Kimi is the extreme).

Both scales are min--max normalised over the per-model rows only (mean rows are
mapped through the same normaliser), so colour is RELATIVE within the plotted
set — which is what makes a small contrast pop; the printed number is always the
raw mean-calls-per-run. Pure stdlib -> a raw TikZ \\fill/\\node grid (same idiom
as make_heatmap.py), so it compiles against the paper's tikz+pgfplots preamble
with no extra libraries.

Outputs:
    paper/figures/fig_modeltools_heatmap.tex   (\\input by sections/07_evaluation.tex)
    eval/results/per_model_tools.csv           (raw counts, for transparency)

Usage:
    python -m eval.experiments.modelsplit.figures.make_tool_heatmap [--task petfinder_rabbits] [--verify]
    python -m eval.experiments.modelsplit.figures.make_tool_heatmap --task all          # pool every task
"""
from __future__ import annotations

from eval import _bootstrap  # noqa: F401
from eval._bootstrap import REPO_ROOT
from eval.experiments.modelsplit import models as model_registry
from .make_figure import _tex_escape, _verify

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

PAPER = REPO_ROOT.parent / "paper"

# Cell geometry (cm). TOTAL column is offset by GAP for a visual break.
W, H = 0.92, 0.62
GAP = 0.22
_LAB_COLOR = {"US": "blue!55!black", "CN": "orange!65!black"}
_LAB_RANK = {"US": 0, "CN": 1}


# --- data --------------------------------------------------------------------
def _count_steps(seed_dir: Path) -> Counter:
    """Count executed tool steps (by `tool`) across this run's step ledgers."""
    c: Counter = Counter()
    for sf in (seed_dir / "ledgers").glob("*/steps.jsonl"):
        try:
            lines = sf.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            tool = rec.get("tool")
            if isinstance(tool, str) and tool:
                c[tool] += 1
    return c


def load_tool_counts(runs_dir: Path, task: str) -> list[dict]:
    """One dict per model: mean tool-type counts per run over its seeds (+ lab).

    task='all' pools every task's seeds; otherwise restrict to that task id.
    """
    glob = "*/*/seed*/meta.json" if task == "all" else f"*/{task}/seed*/meta.json"
    per_model_runs: dict[str, list[Counter]] = defaultdict(list)
    model_id_of: dict[str, str] = {}
    for meta_path in sorted(runs_dir.glob(glob)):
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            continue
        if meta.get("api_error"):  # provider never ran the brain model — skip
            continue
        label = meta.get("label") or meta_path.parents[2].name
        per_model_runs[label].append(_count_steps(meta_path.parent))
        model_id_of[label] = (meta.get("model") or {}).get("model") or label

    out: list[dict] = []
    for label, runs in per_model_runs.items():
        n = len(runs) or 1
        mean = Counter()
        for c in runs:
            mean.update(c)
        mean = {k: v / n for k, v in mean.items()}
        spec = model_registry.resolve(model_id_of[label])
        out.append({
            "model_label": label,
            "model_id": model_id_of[label],
            "short_name": spec.short_name if spec else (model_id_of[label] or label),
            "lab": spec.lab if spec else "?",
            "n_runs": len(runs),
            "counts": mean,
            "total": sum(mean.values()),
        })
    return out


def _seed_sequence(seed_dir: Path) -> list[str]:
    """The timestamp-ordered list of executed tool names for one run (all the
    run's worker ledgers merged by step timestamp -> the true global order)."""
    recs: list[tuple[float, str]] = []
    for sf in (seed_dir / "ledgers").glob("*/steps.jsonl"):
        try:
            lines = sf.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            tool = rec.get("tool")
            if isinstance(tool, str) and tool:
                recs.append((float(rec.get("timestamp") or 0.0), tool))
    recs.sort(key=lambda x: x[0])
    return [t for _, t in recs]


def load_tool_sequences(runs_dir: Path, task: str) -> list[dict]:
    """One dict per model holding the ORDERED tool sequence of a single
    representative run (the first seed found), for the per-model trajectory
    view. Unlike load_tool_counts this keeps execution order, not just counts."""
    glob = "*/*/seed*/meta.json" if task == "all" else f"*/{task}/seed*/meta.json"
    out: list[dict] = []
    seen: set = set()
    for meta_path in sorted(runs_dir.glob(glob)):
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            continue
        if meta.get("api_error"):
            continue
        label = meta.get("label") or meta_path.parents[2].name
        if label in seen:          # one representative run per model
            continue
        seq = _seed_sequence(meta_path.parent)
        if not seq:
            continue
        seen.add(label)
        model_id = (meta.get("model") or {}).get("model") or label
        spec = model_registry.resolve(model_id)
        out.append({
            "model_label": label,
            "model_id": model_id,
            "short_name": spec.short_name if spec else (model_id or label),
            "lab": spec.lab if spec else "?",
            "seq": seq,
            "total": len(seq),
        })
    return out


def _order(models: list[dict]) -> list[dict]:
    """US block then CN block (then other); within a block, ascending total
    calls so the most economical model sits at the top of each block."""
    return sorted(models, key=lambda m: (_LAB_RANK.get(m["lab"], 2), m["total"]))


def _columns(models: list[dict], top_n: int) -> tuple[list[str], set]:
    """The top_n most-used tool types across the plotted models (most-used
    first); every remaining tool is folded into a trailing 'other' column so
    the grid stays readable and the row totals still add up."""
    pooled: Counter = Counter()
    for m in models:
        for k, v in m["counts"].items():
            pooled[k] += v
    keep = [t for t, _ in pooled.most_common(top_n)]
    fold = [t for t in pooled if t not in keep]
    cols = keep[:]
    if fold:
        cols.append("other")
    return cols, set(fold)


def _col_value(m: dict, col: str, folded: set) -> float:
    if col == "other":
        return sum(v for k, v in m["counts"].items() if k in folded)
    return m["counts"].get(col, 0.0)


# --- block means -------------------------------------------------------------
def _mean_row(models: list[dict], lab: str) -> dict | None:
    grp = [m for m in models if m["lab"] == lab]
    if not grp:
        return None
    # Average the RAW per-tool counts (keyed exactly like model rows) so that
    # _col_value folds the 'other' column for the mean identically to a model
    # row — and the visible cells then sum to the mean total.
    keys = {k for m in grp for k in m["counts"]}
    counts = {k: sum(m["counts"].get(k, 0.0) for m in grp) / len(grp) for k in keys}
    return {
        "short_name": f"{lab} mean",
        "lab": lab,
        "is_mean": True,
        "counts": counts,
        "total": sum(m["total"] for m in grp) / len(grp),
    }


# --- colour ------------------------------------------------------------------
def _normalizer(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return lambda v: 0.0
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    return lambda v: max(0.0, min(1.0, (v - lo) / span)) if v is not None else 0.0


def _blue_fill(z: float) -> str:
    """Sequential blue: light (few) -> dark (many). Valence-free profile."""
    p = int(round(8 + z * 70))   # 8..78
    return f"blue!{p}!white"


def _econ_fill(z_more: float) -> str:
    """Diverging green->red for the TOTAL column. z_more in [0,1] is the
    'more calls' rank; FEWER calls (low z_more) -> green, MORE -> red."""
    z = 1.0 - z_more                      # 1 = fewest = good = green
    if z >= 0.5:
        p = int(round((z - 0.5) * 2 * 80)) + 12   # 12..92
        return f"green!{p}!yellow"
    q = int(round((0.5 - z) * 2 * 80)) + 12        # 12..92
    return f"red!{q}!yellow"


def _num(v: float) -> str:
    if v is None:
        return "--"
    return f"{v:.0f}" if abs(v - round(v)) < 0.05 else f"{v:.1f}"


# --- TikZ --------------------------------------------------------------------
def _grid_tikz(models: list[dict], cols: list[str], folded: set) -> str:
    ordered = _order(models)
    # rows = US models, US mean, CN models, CN mean, (other models)
    rows: list[dict] = []
    rule_after: set = set()
    for lab in ("US", "CN"):
        grp = [m for m in ordered if m["lab"] == lab]
        rows.extend(grp)
        mr = _mean_row(ordered, lab)
        if mr:
            rows.append(mr)
            rule_after.add(len(rows) - 1)   # heavier rule under each mean row
    rows.extend([m for m in ordered if m["lab"] not in ("US", "CN")])

    # per-column normalisers over MODEL rows only (means mapped through them)
    model_rows = [m for m in rows if not m.get("is_mean")]
    norm_tool = {c: _normalizer([_col_value(m, c, folded) for m in model_rows]) for c in cols}
    norm_total = _normalizer([m["total"] for m in model_rows])

    total_x = len(cols) * W + GAP        # x of the TOTAL column

    lines = [r"\begin{tikzpicture}[font=\scriptsize]"]
    # column headers (rotated), tool names with browser_ stripped
    for j, c in enumerate(cols):
        cx = j * W + W * 0.5
        label = _tex_escape(c if c == "other" else c.replace("browser_", ""))
        lines.append(
            f"\\node[rotate=40, anchor=west, font=\\scriptsize] at ({cx:.2f},0.10) {{{label}}};"
        )
    lines.append(
        f"\\node[rotate=40, anchor=west, font=\\scriptsize\\bfseries] "
        f"at ({total_x + W * 0.5:.2f},0.10) {{Total}};"
    )

    for i, m in enumerate(rows):
        y0 = -(i + 1) * H
        is_mean = m.get("is_mean", False)
        ncol = _LAB_COLOR.get(m["lab"], "black")
        nstyle = "\\scriptsize\\bfseries" if is_mean else "\\scriptsize"
        name = _tex_escape(m["short_name"])
        lines.append(
            f"\\node[anchor=east, font={nstyle}, text={ncol}] "
            f"at (-0.14,{y0 + H / 2:.2f}) {{{name}}};"
        )
        # tool cells
        for j, c in enumerate(cols):
            x0 = j * W
            v = _col_value(m, c, folded)
            fill = _blue_fill(norm_tool[c](v))
            edge = "draw=black!45, line width=0.5pt" if is_mean else "draw=black!22"
            lines.append(
                f"\\fill[{fill}, {edge}] ({x0:.2f},{y0:.2f}) rectangle ++({W:.2f},{H:.2f});"
            )
            txt = _num(v)
            if v > 0 or is_mean:
                tstyle = "\\scriptsize\\bfseries" if is_mean else "\\scriptsize"
                lines.append(
                    f"\\node[font={tstyle}] at ({x0 + W / 2:.2f},{y0 + H / 2:.2f}) {{{txt}}};"
                )
        # TOTAL cell (offset, diverging colour)
        fill = _econ_fill(norm_total(m["total"]))
        edge = "draw=black!55, line width=0.7pt"
        lines.append(
            f"\\fill[{fill}, {edge}] ({total_x:.2f},{y0:.2f}) rectangle ++({W:.2f},{H:.2f});"
        )
        lines.append(
            f"\\node[font=\\scriptsize\\bfseries] "
            f"at ({total_x + W / 2:.2f},{y0 + H / 2:.2f}) {{{_num(m['total'])}}};"
        )

    # vertical separator rule before the TOTAL column
    y_top, y_bot = 0.0, -len(rows) * H
    lines.append(
        f"\\draw[black!45, line width=0.6pt] ({len(cols) * W + GAP / 2:.2f},{y_top:.2f}) "
        f"-- ({len(cols) * W + GAP / 2:.2f},{y_bot:.2f});"
    )
    # heavier horizontal rules under each block mean row
    for i in sorted(rule_after):
        yy = -(i + 1) * H
        lines.append(
            f"\\draw[black!55, line width=0.7pt] (-0.02,{yy:.2f}) -- ({total_x + W:.2f},{yy:.2f});"
        )
    lines.append(r"\end{tikzpicture}")
    return "\n".join(lines) + "\n"


def _legend_tikz() -> str:
    lines = [r"\begin{tikzpicture}[font=\scriptsize]"]
    # blue profile scale
    lines.append(r"\node[anchor=west] at (0,0) {Tool cols (calls, per col.):};")
    x = 4.1
    for color, lab in (("blue!8!white", "few"), ("blue!43!white", ""), ("blue!78!white", "many")):
        lines.append(f"\\filldraw[fill={color}, draw=black!40] ({x:.2f},-0.13) rectangle ++(0.32,0.26);")
        if lab:
            lines.append(f"\\node[anchor=west, font=\\scriptsize] at ({x + 0.36:.2f},0) {{{lab}}};")
        x += 0.9 if not lab else 1.55
    # total econ scale
    lines.append(f"\\node[anchor=west] at ({x + 0.2:.2f},0) {{Total:}};")
    x += 1.25
    for color, lab in (("green!80!yellow", "fewer"), ("yellow", ""), ("red!80!yellow", "more")):
        lines.append(f"\\filldraw[fill={color}, draw=black!40] ({x:.2f},-0.13) rectangle ++(0.32,0.26);")
        if lab:
            lines.append(f"\\node[anchor=west, font=\\scriptsize] at ({x + 0.36:.2f},0) {{{lab}}};")
        x += 0.9 if not lab else 1.7
    lines.append(r"\end{tikzpicture}")
    return "\n".join(lines) + "\n"


# --- transparency CSV --------------------------------------------------------
def _write_csv(models: list[dict], cols: list[str], folded: set, path: Path):
    tools = sorted({k for m in models for k in m["counts"]})
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["short_name", "model_id", "lab", "n_runs", "total"] + tools)
        for m in _order(models):
            w.writerow([m["short_name"], m["model_id"], m["lab"], m["n_runs"],
                        f"{m['total']:.2f}"] + [f"{m['counts'].get(t, 0):.2f}" for t in tools])


def main():
    p = argparse.ArgumentParser(description="§7.4 model-wise tool-economy heatmap (from steps.jsonl)")
    p.add_argument("--runs", default=str(REPO_ROOT / "eval" / "runs"))
    p.add_argument("--task", default="petfinder_rabbits",
                   help="task id to restrict to (default petfinder_rabbits), or 'all' to pool")
    p.add_argument("--out", default=str(PAPER / "figures" / "fig_modeltools_heatmap.tex"))
    p.add_argument("--csv", default=str(REPO_ROOT / "eval" / "results" / "per_model_tools.csv"))
    p.add_argument("--top", type=int, default=6,
                   help="show the N most-used tools as columns; fold the rest into 'other'")
    p.add_argument("--verify", action="store_true", help="compile the artifact standalone via pdflatex")
    args = p.parse_args()

    models = load_tool_counts(Path(args.runs), args.task)
    if not models:
        print(f"[error] no runs found under {args.runs} for task={args.task!r}.")
        return
    cols, folded = _columns(models, args.top)

    Path(args.csv).parent.mkdir(parents=True, exist_ok=True)
    _write_csv(models, cols, folded, Path(args.csv))

    body = (
        "% Auto-generated by eval/figures/make_tool_heatmap.py from steps.jsonl. Do not hand-edit.\n"
        f"% Task={args.task}. Rows = Worker models (US block then CN block, + per-block mean);\n"
        "% tool columns = executed tool-call counts (blue: light=few -> dark=many, per column);\n"
        "% TOTAL column = calls to finish (green=fewer/economical -> red=more, per column).\n"
        "% Printed value = mean executed calls per run. Colour is relative within the plotted set.\n"
        "\\centering\n"
        + _grid_tikz(models, cols, folded)
        + "\\par\\vspace{6pt}\n"
        + _legend_tikz()
    )
    Path(args.out).write_text(body)
    print(f"Wrote {args.out} ({len(models)} models, {len(cols)} tool cols + Total; task={args.task})")
    print(f"Wrote {args.csv}")
    for m in _order(models):
        print(f"  {m['short_name']:18s} lab={m['lab']:3s} total={m['total']:.1f}  runs={m['n_runs']}")

    if args.verify:
        ok, tail = _verify(body)
        print(f"  verify tool-heatmap: {'OK' if ok else 'FAIL'}")
        if not ok:
            print(tail)


if __name__ == "__main__":
    main()
