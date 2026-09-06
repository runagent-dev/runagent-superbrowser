"""Shared harness for the SuperBrowser research evaluation suite.

Layout
------
protocol.py   frozen run protocol (caps, pins, model ids) + stable hash
tasks.py      benchmark loading (frozen jsonl files, subsets, exclusions)
arms.py       arm registry: name -> env toggles (python-side / TS-side)
server.py     TypeScript browser-server lifecycle for TS-side arms
run_one.py    one (task, arm, seed) run, executed in its own subprocess
runner.py     schedules runs (seed -> task -> arm, arms interleaved), harvests
records.py    RunRecord schema + results.jsonl
harvest.py    run directory -> RunRecord (counts, tokens, outcome)
judges/       WebJudge (Online-Mind2Web), final-answer judge, deterministic
metrics/      CSD, DRR, RPR, grounding, efficiency, cost
stats.py      paired statistics used by the analyzers
loaders.py    run directories -> pandas frames for the analyzers

Importing ``eval`` runs ``eval._bootstrap`` (sys.path + .env), exactly like
the production CLI, so every module here sees the same environment.
"""
