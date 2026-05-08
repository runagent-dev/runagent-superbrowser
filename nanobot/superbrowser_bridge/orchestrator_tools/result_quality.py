"""Result-quality + task-fingerprint helpers.

`_result_is_substantive` decides whether a worker's output is worth
preserving instead of triggering a reflex re-delegation. `_task_fingerprint`
gates the "Resume From Checkpoint" injection so checkpoints from one task
don't leak into a different task on the same domain.
"""

from __future__ import annotations

from .constants import _SUBSTANTIVE_KEYWORDS, _SUBSTANTIVE_PRICE_RE


def _result_is_substantive(text: str) -> tuple[bool, list[str]]:
    """Return (is_substantive, reasons) for a worker result.

    Substantive means: ≥ 400 chars AND at least one of:
      - contains a price token like "$15.72" or "$ 6"
      - contains a numbered list of options ("1.", "2.")
      - contains domain-specific keywords ("In & Out", "garage", "verified")
    Used to refuse a 2nd re-delegation that would discard verified work.
    The 400-char floor avoids false positives from short error captions
    that happen to contain a price or keyword (e.g. "[error: $0 returned"
    on a 50-char failure).
    """
    if not text:
        return False, []
    reasons: list[str] = []
    if len(text) < 400:
        return False, []
    if _SUBSTANTIVE_PRICE_RE.search(text):
        reasons.append("price_tokens")
    lower = text.lower()
    kw_hits = [kw for kw in _SUBSTANTIVE_KEYWORDS if kw in lower]
    if kw_hits:
        reasons.append(f"keywords({','.join(kw_hits[:3])})")
    if "1." in text and "2." in text:
        reasons.append("numbered_list")
    return (bool(reasons), reasons)


def _task_fingerprint(instructions: str) -> str:
    """SHA1 fingerprint of a normalized task-instruction string.

    Used to gate the "Resume From Checkpoint" injection so checkpoints
    from a completed task can't leak into the prompt of a SUBSEQUENT,
    different task on the same domain. Normalization is aggressive on
    purpose — whitespace collapsed, lowercased, punctuation stripped —
    so that tiny prompt tweaks (extra space, casing) still match.
    """
    import hashlib as _hashlib
    import re as _re
    if not instructions:
        return ""
    s = (instructions or "").lower()
    # Strip punctuation beyond alnum/space; collapse whitespace.
    s = _re.sub(r"[^a-z0-9 ]+", " ", s)
    s = _re.sub(r"\s+", " ", s).strip()
    return _hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]
