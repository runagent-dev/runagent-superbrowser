"""Label-sanitization for ledger args.

Element labels flow into StepOutcome.args (e.g. ``V2|"Add to cart"``)
as the syntactic anchor for re-identifying click/scroll/slider targets
after message-history compaction. They must survive double-quoting in
the rendered ledger line, JSON serialization in steps.jsonl, and a
small token budget — hence the cap.
"""

from __future__ import annotations

import re

_WHITESPACE_RE = re.compile(r"\s+")
_MAX_LEN = 60


def clean_label(s: str | None) -> str:
    if not s:
        return ""
    out = _WHITESPACE_RE.sub(" ", s).strip().replace('"', "'")
    out = "".join(ch for ch in out if ch >= " ")
    if len(out) > _MAX_LEN:
        out = out[: _MAX_LEN - 1] + "…"
    return out
