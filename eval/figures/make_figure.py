"""Generate the §7.4 figure + table from eval/results/per_model.csv.

Emits pgfplots (bare tikzpicture) and booktabs sources that match the paper's
house style (figures/fig6_results_bar.tex, tables/tab1_ablations.tex) and are
\\input directly by sections/07_evaluation.tex — no matplotlib, no PNG step,
vector quality, always in sync with the build.

Outputs (default into the paper tree):
    paper/figures/fig_modelsplit.tex   panels (a) dissociation scatter + (b) tool-mix bars
    paper/figures/fig_rescue.tex       rescue ablation (only if *_rescue runs exist)
    paper/tables/tab_toolselection.tex booktabs model x outcome-class table

A "PLACEHOLDER benchmarks" note is rendered whenever a plotted model's
bench_composite is still unconfirmed (eval/models.py), so unfilled numbers can
never silently ship.

Usage:  python -m eval.figures.make_figure [--in ...] [--verify]
"""
from __future__ import annotations

from .. import _bootstrap  # noqa: F401
from .._bootstrap import REPO_ROOT

import argparse
import csv
import subprocess
import tempfile
from pathlib import Path

PAPER = REPO_ROOT.parent / "paper"

# bucket -> (legend label, pgf fill color). Order = stack order bottom->top.
BUCKET_STYLE = [
    ("well_formed_procedural", "well-formed proc.", "blue!55"),
    ("well_formed_declarative", "well-formed decl.", "cyan!45"),
    ("well_formed_other", "well-formed other", "green!35"),
    ("bad_tool_name", "bad tool name", "red!75!black"),
    ("bad_arg_name", "bad arg name", "red!55"),
    ("prose_instead_of_call", "prose not call", "orange!85"),
    ("stale_or_oob_vision_index", "stale/oob $[V_n]$", "orange!55"),
    ("dead_click_violation", "dead-click", "yellow!75!orange"),
    ("no_effect_retry", "no-effect retry", "yellow!45"),
]


def _f(x):
    try:
        if x is None or x == "" or str(x).lower() == "none":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _read(path: Path) -> list[dict]:
    with path.open() as f:
        return list(csv.DictReader(f))


def _is_rescue(row) -> bool:
    return str(row.get("is_rescue", "")).lower() in ("true", "1")


def _order(models: list[dict]) -> list[dict]:
    """US block then CN block; within a block, descending nav success."""
    def key(r):
        lab_rank = {"US": 0, "CN": 1}.get(r.get("lab"), 2)
        return (lab_rank, -(_f(r.get("task_success_mean")) or 0))
    return sorted(models, key=key)


def _sym(label: str) -> str:
    return "".join(c for c in label if c.isalnum()) or "m"


# --- panel (a): dissociation scatter ----------------------------------------
def _linfit(pts):
    n = len(pts)
    if n < 2:
        return None
    sx = sum(p[0] for p in pts)
    sy = sum(p[1] for p in pts)
    sxx = sum(p[0] * p[0] for p in pts)
    sxy = sum(p[0] * p[1] for p in pts)
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-9:
        return None
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    return slope, intercept


def scatter_tikz(models: list[dict]) -> str:
    pts = [(m, _f(m.get("bench_composite")), _f(m.get("task_success_mean")))
           for m in models]
    pts = [(m, bx, sy) for (m, bx, sy) in pts if bx is not None and sy is not None]
    if not pts:
        return "% no models with bench_composite + success; scatter omitted\n"
    xs = [bx for _, bx, _ in pts]
    xmin, xmax = min(xs) - 4, max(xs) + 4
    us = [(bx, sy * 100) for m, bx, sy in pts if m.get("lab") == "US"]
    cn = [(bx, sy * 100) for m, bx, sy in pts if m.get("lab") == "CN"]

    def coords(rows):
        return " ".join(f"({bx:.1f},{sy:.1f})" for bx, sy in rows)

    def errcoords(group):
        out = []
        for m, bx, sy in pts:
            if m.get("lab") != group:
                continue
            e = (_f(m.get("task_success_std")) or 0) * 100
            out.append(f"({bx:.1f},{sy*100:.1f}) +- (0,{e:.1f})")
        return " ".join(out)

    lines = [r"\begin{tikzpicture}", r"\begin{axis}[",
             r"  width=\linewidth, height=6.2cm,",
             r"  xlabel={Public reasoning composite (norm.)},",
             r"  ylabel={\sys{} web-nav success (\%)},",
             r"  xlabel style={font=\small}, ylabel style={font=\small},",
             r"  tick label style={font=\scriptsize},",
             f"  xmin={xmin:.0f}, xmax={xmax:.0f}, ymin=0, ymax=100,",
             r"  grid=major, grid style={gray!18},",
             r"  title={(a)~Reasoning $\neq$ navigation}, title style={font=\small},",
             r"  legend style={font=\tiny, at={(0.02,0.03)}, anchor=south west, draw=gray!40},",
             r"]"]
    fit = _linfit([(bx, sy * 100) for m, bx, sy in pts if m.get("lab") == "US"])
    if fit:
        s, b = fit
        lines.append(
            f"\\addplot[dashed, gray!70, thick, domain={xmin:.0f}:{xmax:.0f}, samples=2] "
            f"{{{s:.3f}*x + {b:.3f}}};"
        )
        lines.append(r"\addlegendentry{US trend}")
    if us:
        lines.append(r"\addplot[only marks, mark=*, mark size=2.4pt, blue!60!black,")
        lines.append(r"  error bars/.cd, y dir=both, y explicit, error bar style={blue!40}]")
        lines.append(f"  coordinates {{{errcoords('US')}}};")
        lines.append(r"\addlegendentry{US labs}")
    if cn:
        lines.append(r"\addplot[only marks, mark=square*, mark size=2.4pt, orange!80!black,")
        lines.append(r"  error bars/.cd, y dir=both, y explicit, error bar style={orange!50}]")
        lines.append(f"  coordinates {{{errcoords('CN')}}};")
        lines.append(r"\addlegendentry{Chinese labs}")
    # per-point name labels
    for m, bx, sy in pts:
        col = "blue!50!black" if m.get("lab") == "US" else "orange!60!black"
        name = _tex_escape(m.get("short_name", ""))
        lines.append(
            f"\\node[font=\\tiny, {col}, anchor=south, yshift=2pt] at (axis cs:{bx:.1f},{sy*100:.1f}) {{{name}}};"
        )
    lines += [r"\end{axis}", r"\end{tikzpicture}"]
    return "\n".join(lines) + "\n"


# --- panel (b): tool-call outcome stacked bars ------------------------------
def stacked_tikz(models: list[dict]) -> str:
    models = [m for m in models if (_f(m.get("total_tool_attempts")) or 0) > 0]
    if not models:
        return "% no tool attempts; stacked bars omitted\n"
    syms = [_sym(m.get("short_name", f"m{i}")) for i, m in enumerate(models)]
    # ensure uniqueness
    seen = {}
    for i, s in enumerate(syms):
        if s in seen:
            seen[s] += 1
            syms[i] = f"{s}{seen[s]}"
        else:
            seen[s] = 0
    ticklabels = ",".join("{" + _tex_escape(m.get("short_name", "")) + "}" for m in models)

    lines = [r"\begin{tikzpicture}", r"\begin{axis}[",
             r"  width=\linewidth, height=6.2cm,",
             r"  ybar stacked, bar width=11pt,",
             r"  ymin=0, ymax=100,",
             f"  symbolic x coords={{{','.join(syms)}}},",
             r"  xtick=data,",
             f"  xticklabels={{{ticklabels}}},",
             r"  x tick label style={rotate=38, anchor=east, font=\scriptsize},",
             r"  tick label style={font=\scriptsize},",
             r"  ylabel={Worker tool calls (\%)}, ylabel style={font=\small},",
             r"  title={(b)~Where the calls go}, title style={font=\small},",
             r"]"]
    for bucket, label, color in BUCKET_STYLE:
        coords = []
        for sym, m in zip(syms, models):
            total = _f(m.get("total_tool_attempts")) or 0
            cnt = _f(m.get(f"cnt_{bucket}")) or 0
            pct = (100.0 * cnt / total) if total else 0.0
            coords.append(f"({sym},{pct:.2f})")
        lines.append(f"\\addplot+[ybar, fill={color}, draw=black!40] coordinates {{{' '.join(coords)}}};")
    lines += [r"\end{axis}", r"\end{tikzpicture}"]
    return "\n".join(lines) + "\n"


def legend_tikz() -> str:
    """Shared bucket legend as a 3-column raw-TikZ swatch grid, placed below both
    panels so the panels stay structurally identical and top-align cleanly. Raw
    TikZ (no pgfplots legend) renders deterministically."""
    cols, colw, rowh = 3, 4.7, 0.46
    lines = [r"\begin{tikzpicture}[font=\scriptsize]"]
    for i, (_b, label, color) in enumerate(BUCKET_STYLE):
        x = (i % cols) * colw
        y = -(i // cols) * rowh
        lines.append(
            f"\\filldraw[fill={color}, draw=black!45] ({x:.2f},{y:.2f}) rectangle ++(0.32,0.26);"
        )
        lines.append(f"\\node[anchor=west] at ({x + 0.42:.2f},{y + 0.13:.2f}) {{{label}}};")
    lines.append(r"\end{tikzpicture}")
    return "\n".join(lines) + "\n"


# --- rescue ablation --------------------------------------------------------
def rescue_tikz(base_models: list[dict], rescue_by_base: dict) -> str | None:
    pairs = []
    for m in base_models:
        r = rescue_by_base.get(m.get("model_label"))
        if r is not None:
            pairs.append((m, r))
    if not pairs:
        return None
    syms = [_sym(m.get("short_name", f"m{i}")) for i, (m, _) in enumerate(pairs)]
    ticklabels = ",".join("{" + _tex_escape(m.get("short_name", "")) + "}" for m, _ in pairs)

    def series(metric, src):  # src: "base" or "rescue"
        coords = []
        for sym, (m, r) in zip(syms, pairs):
            row = m if src == "base" else r
            v = _f(row.get(metric))
            coords.append(f"({sym},{(v*100 if v is not None else 0):.1f})")
        return " ".join(coords)

    lines = [r"\begin{tikzpicture}", r"\begin{axis}[",
             r"  width=0.7\linewidth, height=5.2cm,",
             r"  ybar, bar width=7pt, ymin=0, ymax=105,",
             f"  symbolic x coords={{{','.join(syms)}}},",
             r"  xtick=data,", f"  xticklabels={{{ticklabels}}},",
             r"  x tick label style={font=\scriptsize},",
             r"  tick label style={font=\scriptsize},",
             r"  ylabel={Fidelity (\%)}, ylabel style={font=\small},",
             r"  title={Schema-reminder rescue (Chinese models)}, title style={font=\small},",
             r"  legend style={font=\tiny, at={(0.5,-0.18)}, anchor=north, draw=gray!40}, legend columns=2,",
             r"]"]
    lines.append(f"\\addplot+[fill=red!45, draw=black!40] coordinates {{{series('schema_fidelity_mean','base')}}};")
    lines.append(r"\addlegendentry{schema fid. (base)}")
    lines.append(f"\\addplot+[fill=blue!55, draw=black!40] coordinates {{{series('schema_fidelity_mean','rescue')}}};")
    lines.append(r"\addlegendentry{schema fid. (+reminder)}")
    lines.append(f"\\addplot+[fill=orange!55, draw=black!40] coordinates {{{series('index_discipline_mean','base')}}};")
    lines.append(r"\addlegendentry{index disc. (base)}")
    lines.append(f"\\addplot+[fill=cyan!55, draw=black!40] coordinates {{{series('index_discipline_mean','rescue')}}};")
    lines.append(r"\addlegendentry{index disc. (+reminder)}")
    lines += [r"\end{axis}", r"\end{tikzpicture}"]
    return "\n".join(lines) + "\n"


# --- table ------------------------------------------------------------------
def _tex_escape(s) -> str:
    s = str(s)
    for a, b in (("\\", r"\textbackslash{}"), ("&", r"\&"), ("%", r"\%"),
                 ("_", r"\_"), ("#", r"\#"), ("$", r"\$")):
        s = s.replace(a, b)
    return s


def _pct(x, std=None):
    v = _f(x)
    if v is None:
        return "--"
    out = f"{v*100:.0f}"
    s = _f(std)
    if s is not None and s > 0:
        out += f"\\,$\\pm$\\,{s*100:.0f}"
    return out


def _num(x):
    v = _f(x)
    return "--" if v is None else f"{v:.1f}"


def table_tex(models: list[dict]) -> str:
    any_unconfirmed = any(str(m.get("bench_confirmed", "")).lower() != "true" for m in models)
    lines = [r"\begin{table}[t]",
             r"\caption{Per-model Worker-slot behaviour on the diagnostic task suite "
             r"(mean over seeds; $\pm$ s.d.\ where shown). Scaffolding, vision tier, "
             r"prompts, and budgets are held fixed; only the Worker model varies. "
             r"\emph{Reas.}~is the normalized public reasoning composite"
             + (r"~(\textcolor{red}{PLACEHOLDER --- fill cited numbers in eval/models.py})." if any_unconfirmed else ".")
             + r"}",
             r"\label{tab:toolselection}",
             r"\begin{center}\small",
             r"\begin{tabular}{llcccccc}",
             r"\toprule",
             r"\textbf{Model} & \textbf{Lab} & \textbf{Reas.} & \textbf{Nav.\ succ.\ (\%)} & "
             r"\textbf{Schema fid.\ (\%)} & \textbf{Index disc.\ (\%)} & "
             r"\textbf{Proc.\ share (\%)} & \textbf{Dead-click (\%)} \\",
             r"\midrule"]
    us = [m for m in models if m.get("lab") == "US"]
    cn = [m for m in models if m.get("lab") == "CN"]
    other = [m for m in models if m.get("lab") not in ("US", "CN")]

    def row(m):
        return (f"{_tex_escape(m.get('short_name',''))} & {m.get('lab','?')} & "
                f"{_num(m.get('bench_composite'))} & "
                f"{_pct(m.get('task_success_mean'), m.get('task_success_std'))} & "
                f"{_pct(m.get('schema_fidelity_mean'), m.get('schema_fidelity_std'))} & "
                f"{_pct(m.get('index_discipline_mean'), m.get('index_discipline_std'))} & "
                f"{_pct(m.get('procedural_share_mean'))} & "
                f"{_pct(m.get('dead_click_violation_rate_mean'))} \\\\")

    for m in us:
        lines.append(row(m))
    if us and (cn or other):
        lines.append(r"\midrule")
    for m in cn:
        lines.append(row(m))
    for m in other:
        lines.append(row(m))
    lines += [r"\bottomrule", r"\end{tabular}\end{center}", r"\end{table}"]
    return "\n".join(lines) + "\n"


# --- standalone-compile verification ----------------------------------------
def _verify(tex_body: str) -> tuple[bool, str]:
    doc = (
        "\\documentclass{article}\n\\usepackage{tikz}\n\\usepackage{pgfplots}\n"
        "\\pgfplotsset{compat=1.17}\n\\usepgfplotslibrary{groupplots}\n"
        "\\usepackage{booktabs}\\usepackage{xcolor}\n"
        "\\newcommand{\\sys}{SuperBrowser}\n\\newcommand{\\Vn}{$[V_n]$}\n"
        "\\begin{document}\n" + tex_body + "\n\\end{document}\n"
    )
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "v.tex"
        p.write_text(doc)
        try:
            r = subprocess.run(
                ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "v.tex"],
                cwd=td, capture_output=True, text=True, timeout=120,
            )
        except Exception as exc:
            return False, str(exc)
        ok = (Path(td) / "v.pdf").exists()
        tail = "\n".join(r.stdout.splitlines()[-25:]) if not ok else ""
        return ok, tail


def main():
    p = argparse.ArgumentParser(description="Generate §7.4 figure + table")
    p.add_argument("--in", dest="inp", default=str(REPO_ROOT / "eval" / "results" / "per_model.csv"))
    p.add_argument("--fig", default=str(PAPER / "figures" / "fig_modelsplit.tex"))
    p.add_argument("--rescue", default=str(PAPER / "figures" / "fig_rescue.tex"))
    p.add_argument("--table", default=str(PAPER / "tables" / "tab_toolselection.tex"))
    p.add_argument("--verify", action="store_true", help="compile each artifact standalone via pdflatex")
    args = p.parse_args()

    rows = _read(Path(args.inp))
    base = _order([r for r in rows if not _is_rescue(r)])
    rescue_by_base = {r.get("model_label", "").removesuffix("_rescue"): r
                      for r in rows if _is_rescue(r)}

    fig_body = (
        "% Auto-generated by eval/figures/make_figure.py from per_model.csv. Do not hand-edit.\n"
        "% Two panels: (a) reasoning-vs-navigation dissociation, (b) tool-call outcome mix,\n"
        "% with a shared bucket legend below.\n"
        "\\begin{minipage}[t]{0.44\\linewidth}\\centering\n" + scatter_tikz(base)
        + "\\end{minipage}\\hfill\n"
        "\\begin{minipage}[t]{0.54\\linewidth}\\centering\n" + stacked_tikz(base)
        + "\\end{minipage}\n\\par\\vspace{2pt}\n\\centering\n" + legend_tikz()
    )
    if any(str(m.get("bench_confirmed", "")).lower() != "true" for m in base):
        fig_body += (
            "\\par\\vspace{1pt}{\\scriptsize\\color{red!70!black}"
            "Illustrative placeholder data --- regenerate from real eval runs and "
            "fill cited benchmarks in \\texttt{eval/models.py}.}\n"
        )
    Path(args.fig).write_text(fig_body)
    print(f"Wrote {args.fig}")

    rescue_body = rescue_tikz(base, rescue_by_base)
    # Always write the file (placeholder when absent) so \input never breaks the build.
    if rescue_body:
        Path(args.rescue).write_text(
            "% Auto-generated by eval/figures/make_figure.py. Rescue ablation.\n" + rescue_body
        )
        print(f"Wrote {args.rescue}")
    else:
        Path(args.rescue).write_text(
            "% Auto-generated by eval/figures/make_figure.py.\n"
            "% No *_rescue runs yet. Run: SUPERBROWSER_EVAL_SCHEMA_REMINDER=1 "
            "python -m eval.run_eval --label <model>_rescue\n"
            "\\emph{(Schema-reminder rescue ablation will appear here once "
            "\\texttt{*\\_rescue} runs are available.)}\n"
        )
        print(f"Wrote {args.rescue} (placeholder — no *_rescue runs found)")

    tbl = table_tex(base)
    Path(args.table).write_text(tbl)
    print(f"Wrote {args.table}")

    if args.verify:
        for name, body in (("figure", fig_body), ("table", tbl),
                           ("rescue", rescue_body or "")):
            if not body:
                continue
            ok, tail = _verify(body)
            print(f"  verify {name}: {'OK' if ok else 'FAIL'}")
            if not ok:
                print(tail)


if __name__ == "__main__":
    main()
