"""Form-fill state machine for multi-field form orchestration.

Why this exists
---------------
The brain frequently misses fields on dense filter forms (booking sites,
search results, checkout). Two failure modes recur:

  1. The brain enumerates required fields once at task start, then as it
     types into one field an autocomplete suggestion overlay covers the
     next field. The brain picks a suggestion, the overlay collapses,
     and the now-uncovered field is forgotten because the LLM's plan
     "ended" with the click.

  2. After all visible fields are filled, the brain submits without
     verifying that the typed values stuck. Form widgets that use
     custom React state often reject programmatic input or auto-correct
     it (e.g. typing 'kh' becomes 'Khulna, Bangladesh' via autocomplete
     selection — the brain wanted 'kh').

`FormFillSession` is a deterministic shell around the LLM's free-form
filling. The brain still drives every keystroke and click — but the
session tracks per-field state, forces a re-screenshot after each
autocomplete dismiss, and at commit time verifies every typed value
against what's actually visible on the page.

Public surface
--------------
- `FormFillSession` — dataclass + methods for state tracking
- `FieldStatus` — enum of per-field states
- `FieldState` — per-field record

Wiring
------
A session is attached to `BrowserSessionState.form_session` when the
brain calls `browser_form_begin`. While attached, every mutating tool
result is enriched with a remaining-field checklist via the worker
hook. The session is cleared on `browser_form_commit` (success) or
`browser_navigate` to a different host (form abandoned).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class FieldStatus(str, Enum):
    PENDING = "pending"
    TYPING = "typing"
    AWAIT_AUTOCOMPLETE = "await_autocomplete"
    FILLED = "filled"
    VERIFIED = "verified"
    SKIPPED = "skipped"
    MISMATCH = "mismatch"


@dataclass
class FieldState:
    label: str
    target_value: str
    bbox_index: Optional[int] = None
    last_value_typed: str = ""
    last_observed_value: str = ""
    status: FieldStatus = FieldStatus.PENDING
    autocomplete_pick_required: bool = False
    fill_attempts: int = 0
    last_attempt_turn: int = -1


@dataclass
class FormFillSession:
    """Per-form state attached to BrowserSessionState while the brain
    is filling a multi-field form.

    Lifecycle:
      INIT (begin)  → FILLING (per-field) → COMMITTED (verified)
                                          → ABANDONED (nav away)
    """

    intent: str
    started_at_turn: int
    fields: dict[str, FieldState] = field(default_factory=dict)
    submit_label: Optional[str] = None
    submit_bbox_index: Optional[int] = None
    state: str = "init"
    last_screenshot_turn: int = -1
    autocomplete_pending_for: Optional[str] = None
    started_at: float = field(default_factory=time.time)

    @classmethod
    def begin(
        cls,
        *,
        intent: str,
        fields: list[dict[str, Any]],
        started_at_turn: int,
        submit_label: str | None = None,
    ) -> "FormFillSession":
        """Build a session from a list of `{label, value, anchor_hint?}` dicts.

        `label` is the field's human-readable name (used to match against
        vision bbox labels later — case-insensitive substring match).
        `value` is what the brain plans to type. `anchor_hint` is an
        optional CSS selector or aria description.
        """
        sess = cls(
            intent=intent,
            started_at_turn=started_at_turn,
            submit_label=submit_label,
            state="init",
        )
        for f in fields:
            label = (f.get("label") or "").strip()
            if not label:
                continue
            sess.fields[label.lower()] = FieldState(
                label=label,
                target_value=str(f.get("value", "") or ""),
                autocomplete_pick_required=bool(f.get("autocomplete", False)),
            )
        return sess

    def next_pending(self) -> Optional[FieldState]:
        """Return the first field still requiring action, or None."""
        for fs in self.fields.values():
            if fs.status in (FieldStatus.PENDING, FieldStatus.MISMATCH):
                return fs
        return None

    def mark_typed(
        self,
        *,
        label_or_index: str | int,
        value_typed: str,
        turn: int,
    ) -> Optional[FieldState]:
        """Record that the brain typed into a field. Matches by label
        substring or vision_index. Returns the matched FieldState (so
        the worker hook can remind the brain whether autocomplete
        handling is still required) or None when no field matched."""
        target = self._match_field(label_or_index)
        if target is None:
            return None
        target.last_value_typed = value_typed
        target.fill_attempts += 1
        target.last_attempt_turn = turn
        if target.autocomplete_pick_required:
            target.status = FieldStatus.AWAIT_AUTOCOMPLETE
            self.autocomplete_pending_for = target.label.lower()
        else:
            target.status = FieldStatus.FILLED
        return target

    def mark_autocomplete_picked(
        self, observed_value: str = ""
    ) -> Optional[FieldState]:
        """The brain chose an autocomplete suggestion. Move the
        currently-pending field from AWAIT_AUTOCOMPLETE to FILLED."""
        if self.autocomplete_pending_for is None:
            return None
        fs = self.fields.get(self.autocomplete_pending_for)
        self.autocomplete_pending_for = None
        if fs is None:
            return None
        if observed_value:
            fs.last_observed_value = observed_value
        fs.status = FieldStatus.FILLED
        return fs

    def mark_verified(self, label: str, observed_value: str) -> None:
        fs = self.fields.get(label.lower())
        if fs is None:
            return
        fs.last_observed_value = observed_value
        # Lenient match: target value should appear in observed (handles
        # autocomplete completing 'kh' → 'Khulna, Bangladesh' as a
        # successful fill rather than a mismatch).
        target_lower = (fs.target_value or "").strip().lower()
        observed_lower = (observed_value or "").strip().lower()
        if not target_lower:
            fs.status = FieldStatus.VERIFIED
            return
        if target_lower in observed_lower or observed_lower in target_lower:
            fs.status = FieldStatus.VERIFIED
        else:
            fs.status = FieldStatus.MISMATCH

    def remaining_checklist(self, max_lines: int = 8) -> str:
        """Render a brief checklist of remaining fields. Used by the
        worker hook to remind the brain after every tool result."""
        if not self.fields:
            return ""
        lines: list[str] = []
        for fs in self.fields.values():
            mark = {
                FieldStatus.PENDING: "[ ]",
                FieldStatus.TYPING: "[~]",
                FieldStatus.AWAIT_AUTOCOMPLETE: "[?]",
                FieldStatus.FILLED: "[+]",
                FieldStatus.VERIFIED: "[x]",
                FieldStatus.SKIPPED: "[-]",
                FieldStatus.MISMATCH: "[!]",
            }.get(fs.status, "[?]")
            target_short = fs.target_value[:30]
            lines.append(f"  {mark} {fs.label}: {target_short!r}")
            if len(lines) >= max_lines:
                lines.append(f"  … +{len(self.fields) - max_lines} more")
                break
        return "[FORM_PROGRESS]\n" + "\n".join(lines)

    def needs_screenshot(self, current_turn: int) -> Optional[str]:
        """Return a one-liner instructing the brain to re-screenshot
        when the form session would otherwise act on stale vision.

        Triggers when:
          - autocomplete just dismissed (overlay may be uncovering fields)
          - 2+ turns since last screenshot during active filling
        """
        if self.autocomplete_pending_for is not None:
            return None  # brain hasn't picked yet
        # Just-finished autocomplete: pending_for cleared THIS turn.
        if self.last_screenshot_turn < 0:
            return (
                "[form_session] No screenshot taken since form_begin. "
                "Call browser_screenshot now so vision can map every "
                "field's bbox before you start typing."
            )
        if current_turn - self.last_screenshot_turn >= 2:
            return (
                "[form_session] 2+ actions since last screenshot. The "
                "field positions / autocomplete state may have shifted. "
                "Call browser_screenshot before the next form action."
            )
        return None

    def record_screenshot(self, turn: int) -> None:
        self.last_screenshot_turn = turn

    def is_complete(self) -> bool:
        if not self.fields:
            return False
        return all(
            fs.status in (FieldStatus.VERIFIED, FieldStatus.SKIPPED)
            for fs in self.fields.values()
        )

    def commit_summary(self) -> str:
        verified = sum(
            1 for f in self.fields.values()
            if f.status == FieldStatus.VERIFIED
        )
        mismatched = [
            f for f in self.fields.values()
            if f.status == FieldStatus.MISMATCH
        ]
        pending = [
            f for f in self.fields.values()
            if f.status in (FieldStatus.PENDING, FieldStatus.AWAIT_AUTOCOMPLETE)
        ]
        parts = [
            f"verified={verified}/{len(self.fields)}",
        ]
        if pending:
            parts.append(
                "pending=" + ",".join(f.label for f in pending[:5])
            )
        if mismatched:
            parts.append(
                "mismatch=" + ",".join(
                    f"{f.label}({f.last_observed_value[:20]!r}!="
                    f"{f.target_value[:20]!r})"
                    for f in mismatched[:3]
                )
            )
        return " ".join(parts)

    # --- internal -----------------------------------------------------

    def _match_field(self, label_or_index: str | int) -> Optional[FieldState]:
        if isinstance(label_or_index, int):
            for fs in self.fields.values():
                if fs.bbox_index == label_or_index:
                    return fs
            return None
        token = (label_or_index or "").strip().lower()
        if not token:
            return None
        if token in self.fields:
            return self.fields[token]
        # Substring fallback — bbox label can be wordier than the form
        # field name ("Departure city" vs label="from").
        for key, fs in self.fields.items():
            if key in token or token in key:
                return fs
        return None
