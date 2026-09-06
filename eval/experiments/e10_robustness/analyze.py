"""E10 analysis: TSR / peak context per (policy, budget|window), per model, and seed variance."""
from __future__ import annotations

import math
import re
from collections import defaultdict

from eval.core import report, stats
from eval.core.experiment import analyze_main
from eval.experiments.e10_robustness import SPEC

_ARM_RE = re.compile(r"^(?P<base>[a-z_]+)(?:__(?P<suffix>.*))?$")


def _extra(args, recs, result):
    out = report.out_dir(SPEC.name)
    cells: dict[tuple[str, str, str], list] = defaultdict(list)
    for r in recs:
        m = _ARM_RE.match(str(r.ids.get("arm")))
        base, suffix = (m["base"], m["suffix"] or "base") if m else (str(r.ids.get("arm")), "base")
        cells[(str(r.protocol.get("model")), base, suffix)].append(r)
    rows = []
    for (model, base, suffix), rs in sorted(cells.items()):
        n = len(rs)
        k = sum(1 for x in rs if x.outcome.get("success"))
        peaks = [x.tokens.get("prompt_tokens_per_iter_peak") for x in rs if isinstance(x.tokens.get("prompt_tokens_per_iter_peak"), (int, float))]
        # seed variance: per-seed TSR
        by_seed: dict[int, list[bool]] = defaultdict(list)
        for x in rs:
            by_seed[int(x.ids.get("seed") or 0)].append(bool(x.outcome.get("success")))
        seed_tsr = [sum(v) / len(v) for v in by_seed.values() if v]
        se = (math.sqrt(sum((t - sum(seed_tsr) / len(seed_tsr)) ** 2 for t in seed_tsr) / (len(seed_tsr) - 1)) / math.sqrt(len(seed_tsr))
              if len(seed_tsr) > 1 else None)
        rows.append({"model": model, "policy": base, "variant": suffix, "n": n, "k": k, "tsr": k / n if n else None,
                     "wilson_low": stats.wilson_ci(k, n)[0], "wilson_high": stats.wilson_ci(k, n)[1],
                     "n_seeds": len(by_seed), "tsr_seed_se": se,
                     "prompt_peak_mean": sum(peaks) / len(peaks) if peaks else None,
                     "prompt_peak_max": max(peaks) if peaks else None,
                     "usd": sum(float((x.cost or {}).get("usd") or 0) for x in rs) / n})
    report.write_csv(out / "robustness.csv", rows)
    report.write_tex_table(out / "robustness.tex", ["Model", "Policy", "Variant", "Success", "TSR (\\%)", "SE (seeds)", "Peak prompt (mean/max)", "\\$/task"],
                           [[report.tex_escape(r["model"]), report.tex_escape(r["policy"]), report.tex_escape(r["variant"]), f"{r['k']}/{r['n']}",
                             report.fmt(r["tsr"], "pct"), report.fmt(r["tsr_seed_se"], "pct") if r["tsr_seed_se"] is not None else "--",
                             f"{report.fmt(r['prompt_peak_mean'], 'k')}/{report.fmt(r['prompt_peak_max'], 'k')}", report.fmt(r["usd"], "usd")] for r in rows])
    # negative results: cells where a bounded policy loses to the unbounded one (if present)
    losses = []
    by_key = {(r["model"], r["policy"], r["variant"]): r for r in rows}
    for (model, base, variant), r in by_key.items():
        full = by_key.get((model, "full_history", "base"))
        if full and base != "full_history" and r["tsr"] is not None and full["tsr"] is not None and r["tsr"] < full["tsr"]:
            losses.append({"model": model, "policy": base, "variant": variant, "tsr": r["tsr"], "full_history_tsr": full["tsr"]})
    report.write_csv(out / "where_bounded_memory_loses.csv", losses)


main = analyze_main(SPEC, _extra)

if __name__ == "__main__":
    raise SystemExit(main())
