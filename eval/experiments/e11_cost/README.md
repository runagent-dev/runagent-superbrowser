# e11_cost — cost accounting

**Question.** What does a task cost, by role (orchestrator / worker / vision / compressor), per task and per
successful task, at list price and with prompt-cache discounts?

`eval/core/pricing.json` is a dated snapshot of list prices (`python -m eval.core.pricing --refresh`
regenerates it from the public OpenRouter models API). Judge tokens are priced separately
(`usd_judge`) so evaluation never leaks into the agent's cost.

```bash
python -m eval.experiments.e11_cost.analyze --experiments e1_main,e2_memory_policy
```
Outputs `cost_by_arm.csv/.tex` and the price table used.
