# e12_traces — mechanism traces, selected after the aggregate

**Rule.** A trace explains a population effect that the paired analysis already established; it is never
used to establish it. The picker therefore only looks at discordant pairs (one arm succeeded, the other
failed) of an experiment that has been analysed, ranks them by the metric the experiment is about
(CSD for E2, DRR for E4, RPR for E5) and writes a markdown excerpt per case: lost critical-state items,
revisited dead ends, redundant vision passes, the last actions and both final answers.

```bash
python -m eval.experiments.e12_traces.analyze --experiment e2_memory_policy --arm-a ledger --arm-b fifo --metric csd_observed
python -m eval.experiments.e12_traces.analyze --experiment e4_deadend --arm-a ledger --arm-b no_deadend --metric drr
python -m eval.experiments.e12_traces.analyze --experiment e5_perception_reuse --arm-a ledger --arm-b fresh_vision --metric rpr
```
