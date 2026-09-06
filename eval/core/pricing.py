"""Price table for cost accounting (USD per token, by model id).

``pricing.json`` is a dated snapshot generated from the public OpenRouter
models API (``python -m eval.core.pricing --refresh``); the eval runs route
through OpenRouter, so the ids match the ``model`` recorded per run. Prices
are LIST prices at ``as_of``; the paper reports both raw and cache-discounted
cost and keeps the table so numbers can be recomputed if prices change.
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

PRICING_PATH = Path(__file__).with_name("pricing.json")
SOURCE_URL = "https://openrouter.ai/api/v1/models"

# Models the harness knows it may need (brain candidates, vision, judges).
TRACKED_MODELS = (
    "anthropic/claude-opus-4.8",
    "openai/gpt-5.4",
    "google/gemini-3.5-flash",
    "google/gemini-3-flash-preview",
    "openai/gpt-4o",
    "openai/o4-mini",
    "openai/gpt-5.5",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "moonshotai/kimi-k2.6",
    "deepseek/deepseek-v4-pro",
    "minimax/minimax-m3",
    "inclusionai/ring-2.6-1t",
)


def load() -> dict[str, Any]:
    if not PRICING_PATH.exists():
        return {"as_of": None, "source": SOURCE_URL, "models": {}}
    return json.loads(PRICING_PATH.read_text())


def _norm(model: str | None) -> str:
    return (model or "").strip().lower()


def price_for(model: str | None, table: dict[str, Any] | None = None) -> dict[str, float] | None:
    """{input_per_token, output_per_token, cached_input_per_token} or None."""
    table = table or load()
    m = _norm(model)
    models = table.get("models", {})
    if m in models:
        return models[m]
    # tolerate provider prefixes / suffixes (":free", "openrouter/")
    base = m.split(":")[0]
    for key, val in models.items():
        if key.split(":")[0] == base or key.endswith("/" + base) or base.endswith("/" + key):
            return val
    return None


def refresh(extra_models: tuple[str, ...] = ()) -> dict[str, Any]:
    with urllib.request.urlopen(SOURCE_URL, timeout=30) as resp:  # noqa: S310 - public API
        data = json.load(resp)["data"]
    want = {_norm(m) for m in TRACKED_MODELS + tuple(extra_models)}
    models: dict[str, Any] = {}
    for row in data:
        mid = _norm(row.get("id"))
        if mid not in want:
            continue
        p = row.get("pricing") or {}

        def f(key: str) -> float:
            try:
                return float(p.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        models[mid] = {
            "input_per_token": f("prompt"),
            "output_per_token": f("completion"),
            "cached_input_per_token": f("input_cache_read") or f("prompt"),
            "image_per_unit": f("image"),
            "context_length": row.get("context_length"),
            "name": row.get("name"),
        }
    table = {"as_of": _dt.date.today().isoformat(), "source": SOURCE_URL, "currency": "USD",
             "note": "list prices per token at as_of; cached_input falls back to input when the provider reports none",
             "models": dict(sorted(models.items()))}
    PRICING_PATH.write_text(json.dumps(table, indent=2) + "\n")
    return table


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Show or refresh the price table")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--models", default="", help="extra model ids to include on refresh (comma-separated)")
    args = ap.parse_args(argv)
    if args.refresh:
        t = refresh(tuple(m.strip() for m in args.models.split(",") if m.strip()))
        print(f"wrote {PRICING_PATH} ({len(t['models'])} models, as_of {t['as_of']})")
    t = load()
    print(f"as_of={t.get('as_of')} source={t.get('source')}")
    for mid, p in t.get("models", {}).items():
        print(f"  {mid:44s} in ${p['input_per_token'] * 1e6:8.3f}/M  out ${p['output_per_token'] * 1e6:8.3f}/M"
              f"  cached ${p['cached_input_per_token'] * 1e6:8.3f}/M")
    return 0


if __name__ == "__main__":
    sys.exit(main())
