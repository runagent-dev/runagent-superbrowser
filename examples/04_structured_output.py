"""04 — Structured output: get typed data back, not just prose.

Pass `output_schema` (a pydantic model, a `list[Model]`, or a JSON-Schema dict)
and the SDK instructs the agent to return matching JSON, then parses it into
`res.data`. Parsing is best-effort: `res.data` is the validated value, or
`None` if the answer didn't contain clean JSON — it never raises.

This one runs in fetch mode, so no browser engine is needed.

Prerequisites:
  - An LLM configured (`nanobot onboard`)
  - pydantic (ships with the package)

Run:
  python examples/04_structured_output.py
"""

from __future__ import annotations

import pathlib
import sys

try:
    import runagent_superbrowser  # noqa: F401
except ModuleNotFoundError:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "nanobot"))

from pydantic import BaseModel

from runagent_superbrowser import SuperBrowser


class Story(BaseModel):
    title: str
    points: int | None = None


def main() -> int:
    sb = SuperBrowser()

    res = sb.run(
        "List the top 5 stories on Hacker News with their titles and points.",
        mode="fetch",
        output_schema=list[Story],  # -> res.data is a list[Story]
    )

    print("success:", res.success)
    if res.data is not None:
        print(f"\nparsed {len(res.data)} stories:")
        for i, story in enumerate(res.data, 1):
            print(f"  {i}. {story.title}  ({story.points} pts)")
    else:
        # Fall back to the prose answer if structured parsing didn't land.
        print("\nno structured data parsed; raw answer:\n", res.text)
    return 0 if res.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
