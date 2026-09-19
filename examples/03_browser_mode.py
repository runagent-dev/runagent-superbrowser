"""03 — Browser mode: a real interactive browser, with an audit trail.

`mode="browser"` drives a real headless browser over the TypeScript engine on
:3100 — for anything that clicks, fills forms, logs in, or books. This example
uses `auto_start_server=True` so the SDK starts the engine for you and tears it
down on exit (it only ever stops an engine it started itself).

`audit_dir=` makes every run leave the evaluation harness's run directory —
`eval/runs/sdk/ledger/<task_id>/seed<N>/` with the spec, the ordered screenshots,
the worker transcripts, the memory ledgers (steps, events, live context, vision
and click traces), the final answer, token usage and a provenance manifest (git
commit, prompt hashes, env toggles). When the instruction is an Online-Mind2Web
task typed verbatim, the run is labelled with that benchmark task id; anything
else gets an id derived from the instruction. Judge and record it afterwards:

  python -m eval.core.judge  --experiment sdk    # WebJudge + answer judge -> judges/, run_record.json, results.jsonl
  python -m eval.core.record --experiment sdk    # record only (no judge credentials needed)

In THIS source checkout we point auto-start at the no-build watch server
(`npm run dev`). If you've installed the npm package globally
(`npm i -g runagent-superbrowser`) you can drop `server_cmd` and it'll use the
`superbrowser` binary.

Prerequisites:
  - An LLM configured (`nanobot onboard`)
  - Google Chrome installed + `patchright install chromium`
  - Node (for the engine). Either it's already running on :3100, or
    auto_start_server starts it.

Run:
  python examples/03_browser_mode.py
"""

from __future__ import annotations

import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

try:
    import runagent_superbrowser  # noqa: F401
except ModuleNotFoundError:
    sys.path.insert(0, str(REPO_ROOT / "nanobot"))

from runagent_superbrowser import ServerStartError, ServerUnavailable, SuperBrowser

# Optional: label the run with the frozen benchmark task when the instruction
# matches one verbatim. Only available in a source checkout (eval/ is not part
# of the installed package); the audit trail works without it.
try:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from eval.core.tasks import find_by_instruction
except Exception:  # noqa: BLE001
    find_by_instruction = None  # type: ignore[assignment]

TASK = "What is the second stop among the best stops along the road trip from Yellowstone National Park to Las Vegas?"
URL = "https://wanderlog.com/"


def main() -> int:
    benchmark_task = find_by_instruction(TASK, URL) if find_by_instruction else None
    if benchmark_task is not None:
        print(f"benchmark task: {benchmark_task.benchmark} {benchmark_task.task_id} ({benchmark_task.level})")

    # `server_cmd` is only needed for the watch-mode dev server in a checkout;
    # omit it if the `superbrowser` binary is on your PATH.
    sb = SuperBrowser(
        auto_start_server=True,
        server_cmd=["npm", "run", "dev"],
        server_start_timeout=60.0,  # the dev server compiles on first boot
        audit_dir=REPO_ROOT / "eval" / "runs",   # -> eval/runs/sdk/ledger/<task_id>/seed0/
    )

    # `with` guarantees the engine is torn down even if the task raises.
    try:
        with sb:
            res = sb.run(
                TASK,
                url=URL,
                mode="browser",  # it also can be "auto"
                audit_task=benchmark_task.to_dict() if benchmark_task is not None else None,
            )
    except (ServerUnavailable, ServerStartError) as exc:
        print("Could not start/reach the browser engine:\n ", exc)
        print("Start it manually with `npm run dev` and retry, or check Chrome is installed.")
        return 1

    print("success:", res.success)
    print("answer:\n", res.text)
    if not res.success:
        print("error:", res.error)
    if res.audit_dir:
        print("audit trail:", res.audit_dir)
        print("next:  python -m eval.core.judge --experiment sdk     # judge + record")
        print("       python -m eval.core.record --experiment sdk    # record without judging")
    return 0 if res.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
