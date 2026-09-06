"""USD cost of a run from ``tokens.by_role`` and the dated price table.

Two numbers per role: ``usd`` (list price, every prompt token billed as
uncached input) and ``usd_cached`` (cache-read tokens billed at the cached
rate) — the paper reports both because prompt caching favours the arms with
a stable prefix. Judge usage (recorded per verdict) is reported separately as
``usd_judge`` so evaluation cost never leaks into the agent's cost.
"""
from __future__ import annotations

from typing import Any

from eval.core import pricing
from eval.core.records import RunRecord


def role_cost(role_usage: dict[str, Any], price: dict[str, float] | None) -> tuple[float, float]:
    if not price:
        return (0.0, 0.0)
    inp = float(role_usage.get("input_tokens") or 0)
    out = float(role_usage.get("output_tokens") or 0)
    cached = float(role_usage.get("cache_read_tokens") or 0)
    usd = inp * price["input_per_token"] + out * price["output_per_token"]
    usd_cached = ((inp - min(cached, inp)) * price["input_per_token"] + min(cached, inp) * price["cached_input_per_token"]
                  + out * price["output_per_token"])
    return (usd, usd_cached)


def cost_of_record(rec: RunRecord, table: dict[str, Any] | None = None) -> dict[str, Any]:
    table = table or pricing.load()
    brain_model = rec.protocol.get("model")
    vision_model = (rec.protocol.get("environment") or {}).get("vision_model") or None
    by_role = rec.tokens.get("by_role") or {}
    out: dict[str, Any] = {"as_of": table.get("as_of"), "by_role": {}, "usd": 0.0, "usd_cached": 0.0,
                           "priced": True, "missing_prices": []}
    for role, usage in by_role.items():
        model = vision_model if role == "vision" else brain_model
        price = pricing.price_for(model, table)
        if price is None:
            out["missing_prices"].append({"role": role, "model": model})
            out["priced"] = False
            continue
        usd, usd_cached = role_cost(usage, price)
        out["by_role"][role] = {"model": model, "usd": round(usd, 6), "usd_cached": round(usd_cached, 6)}
        out["usd"] += usd
        out["usd_cached"] += usd_cached
    out["usd"] = round(out["usd"], 6)
    out["usd_cached"] = round(out["usd_cached"], 6)
    judge_usd = 0.0
    for name, usage in (rec.tokens.get("judge_usage") or {}).items():
        model = (rec.outcome.get("judge_models") or {}).get(name)
        price = pricing.price_for(model, table)
        if price:
            judge_usd += role_cost(usage, price)[0]
    out["usd_judge"] = round(judge_usd, 6)
    return out
