"""Claim sets for the content-equivalence gate.

Automatic claims are URLs, numbers, and yes/no sentences, as frozen in the
analysis plan. Named entities are left to the human sample.
"""
from __future__ import annotations

import re

_URL = re.compile(r"https?://[^\s)>\]]+")
_HOST = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b", re.I)
_NUM = re.compile(r"(?<![\w.])[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
_YESNO_SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")
_YESNO_WORD = re.compile(r"\b(?:yes|no)\b", re.I)
_TRAIL_PUNCT = ".,);:\"'`*"


def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _norm_url(url: str) -> str:
    return url.lower().rstrip(_TRAIL_PUNCT)


def _norm_num(num: str) -> str:
    sign = ""
    body = num
    if body[:1] in "+-":
        sign = body[:1]
        body = body[1:]
    if "," in body:
        body = body.replace(",", "")
    return sign + body


def claim_set(text: str) -> set[tuple[str, str]]:
    """Return the comparable claim set of a report."""
    raw = text or ""
    urls = [(m.start(), m.end(), m.group(0)) for m in _URL.finditer(raw)]
    covered = [(a, b) for a, b, _ in urls]
    claims: set[tuple[str, str]] = {("url", _norm_url(u)) for _, _, u in urls}

    def _inside(start: int) -> bool:
        return any(a <= start < b for a, b in covered)

    for m in _HOST.finditer(raw):
        if _inside(m.start()):
            continue
        claims.add(("url", _norm_url(m.group(0))))
    for m in _NUM.finditer(raw):
        if _inside(m.start()):
            continue
        claims.add(("number", _norm_num(m.group(0))))
    for m in _YESNO_SENTENCE.finditer(raw):
        sentence = m.group(0).strip()
        if sentence and _YESNO_WORD.search(sentence):
            claims.add(("yesno", _norm_ws(sentence)))
    return claims


def claim_diff(source: str, rewrite: str) -> dict[str, list[str]]:
    src, rew = claim_set(source), claim_set(rewrite)
    missing = sorted(f"{k}:{v}" for k, v in src - rew)
    added = sorted(f"{k}:{v}" for k, v in rew - src)
    return {"missing": missing, "added": added, "equal": not missing and not added}
