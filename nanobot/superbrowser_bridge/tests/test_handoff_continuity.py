"""Unit tests for handoff continuity across orchestrator-driven retries
(arch v3 fixes H + I + J).

Covers:
  - take_recent_for_domain returns most-recent entry on dedup_key miss
  - merge_brief_progress copies satisfied statuses by canonical match
  - _build_retry_instructions surfaces VERIFIED / FAILED / REMAINING
    sections so the successor worker knows what to skip

Run:
    source venv/bin/activate && \
        python3 -m superbrowser_bridge.tests.test_handoff_continuity
"""

from __future__ import annotations

import sys


def _state(session_id: str = "session-x", url: str = "https://wineaccess.com/store/white-wine/"):
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    s.session_id = session_id
    s.current_url = url
    s.pinned_domain = "wineaccess.com"
    s.task_instruction = "find oregon white wines"
    return s


# ── Fix H — domain-fallback handoff lookup ───────────────────────────


def test_take_recent_for_domain_returns_most_recent_match() -> None:
    from superbrowser_bridge.handoff_store import (
        save, take_recent_for_domain, clear,
    )

    clear()
    s1 = _state(session_id="A", url="https://wineaccess.com/store/red/")
    s2 = _state(session_id="B", url="https://wineaccess.com/store/white-wine/")
    s3 = _state(session_id="C", url="https://booking.com/hotels/")
    s3.pinned_domain = "booking.com"

    save("k1", s1)
    save("k2", s2)
    save("k3", s3)

    h = take_recent_for_domain("wineaccess.com")
    assert h is not None
    # Most recent of the two wineaccess entries: s2 (saved last).
    assert h.session_id == "B"


def test_take_recent_for_domain_pops_entry() -> None:
    """One-shot: a second domain lookup returns None (or the OTHER match)."""
    from superbrowser_bridge.handoff_store import (
        save, take_recent_for_domain, clear,
    )

    clear()
    save("k1", _state(session_id="A", url="https://wineaccess.com/x/"))
    h1 = take_recent_for_domain("wineaccess.com")
    assert h1 is not None
    assert h1.session_id == "A"
    h2 = take_recent_for_domain("wineaccess.com")
    assert h2 is None  # entry was popped


def test_take_recent_for_domain_skips_other_domains() -> None:
    from superbrowser_bridge.handoff_store import (
        save, take_recent_for_domain, clear,
    )

    clear()
    s = _state(session_id="X", url="https://booking.com/r/")
    s.pinned_domain = "booking.com"
    save("k", s)
    assert take_recent_for_domain("wineaccess.com") is None


def test_take_recent_for_domain_empty_domain_returns_none() -> None:
    from superbrowser_bridge.handoff_store import (
        save, take_recent_for_domain, clear,
    )

    clear()
    save("k", _state())
    assert take_recent_for_domain("") is None


# ── Fix I — merge_brief_progress ─────────────────────────────────────


def test_merge_brief_progress_copies_satisfied_statuses() -> None:
    from superbrowser_bridge.task_brief import (
        TaskBrief, Constraint, merge_brief_progress,
    )

    old = TaskBrief(
        original_query="x",
        constraints=[
            Constraint(
                text="WiFi", kind="filter", canonical_value="wifi",
                status="satisfied", evidence="Free WiFi badge",
                last_checked_url="/p/123",
            ),
            Constraint(
                text="Oregon", kind="filter", canonical_value="oregon",
                status="satisfied", evidence="URL contains 'oregon'",
            ),
            Constraint(
                text="Pets", kind="negative", canonical_value="pets",
                status="failed", evidence="Pets allowed visible",
            ),
        ],
    )
    # New brief from re-extracted instructions — same task, but status reset.
    new = TaskBrief(
        original_query="y",
        constraints=[
            Constraint(text="WiFi", kind="filter", canonical_value="wifi"),
            Constraint(text="Oregon, USA", kind="filter", canonical_value="oregon"),
            Constraint(text="Parking", kind="filter", canonical_value="parking"),
            Constraint(text="No pets", kind="negative", canonical_value="pets"),
        ],
    )

    n = merge_brief_progress(old=old, new=new)
    assert n == 3  # wifi + oregon + pets
    assert new.constraints[0].status == "satisfied"
    assert new.constraints[1].status == "satisfied"
    assert new.constraints[2].status == "unverified"  # parking was new
    assert new.constraints[3].status == "failed"
    # Evidence should carry too.
    assert "WiFi" in new.constraints[0].evidence


def test_merge_brief_progress_fuzzy_token_match() -> None:
    """canonical_value 'willamette valley region' (new) merges with
    'willamette valley' (old) via token overlap."""
    from superbrowser_bridge.task_brief import (
        TaskBrief, Constraint, merge_brief_progress,
    )

    old = TaskBrief(
        original_query="x",
        constraints=[
            Constraint(
                text="Willamette Valley", kind="filter",
                canonical_value="willamette valley",
                status="satisfied", evidence="URL contains 'willamette'",
            ),
        ],
    )
    new = TaskBrief(
        original_query="y",
        constraints=[
            Constraint(
                text="Willamette Valley region", kind="filter",
                canonical_value="willamette valley region",
            ),
        ],
    )
    n = merge_brief_progress(old=old, new=new)
    assert n == 1
    assert new.constraints[0].status == "satisfied"


def test_merge_brief_progress_does_not_overwrite_set_status() -> None:
    """If new already has a non-unverified status (e.g., the rebuild
    set it from regex), don't overwrite."""
    from superbrowser_bridge.task_brief import (
        TaskBrief, Constraint, merge_brief_progress,
    )

    old = TaskBrief(
        original_query="x",
        constraints=[
            Constraint(canonical_value="wifi", status="satisfied", evidence="old"),
        ],
    )
    new = TaskBrief(
        original_query="y",
        constraints=[
            Constraint(canonical_value="wifi", status="failed", evidence="new"),
        ],
    )
    n = merge_brief_progress(old=old, new=new)
    assert n == 0  # new's failed status preserved
    assert new.constraints[0].status == "failed"
    assert new.constraints[0].evidence == "new"


def test_merge_brief_progress_handles_none_inputs() -> None:
    from superbrowser_bridge.task_brief import merge_brief_progress, TaskBrief

    assert merge_brief_progress(None, None) == 0
    assert merge_brief_progress(None, TaskBrief(original_query="x")) == 0
    assert merge_brief_progress(TaskBrief(original_query="x"), None) == 0


# ── Fix J — retry instructions surface VERIFIED + REMAINING ──────────


def test_build_retry_instructions_renders_verified_block() -> None:
    from superbrowser_bridge.orchestrator_tools import _build_retry_instructions
    from superbrowser_bridge.task_brief import TaskBrief, Constraint

    brief = TaskBrief(
        original_query="find oregon white wines",
        constraints=[
            Constraint(
                text="white wine", kind="filter",
                canonical_value="white wine",
                status="satisfied",
                evidence="URL contains 'white-wine'",
            ),
            Constraint(
                text="Oregon", kind="filter",
                canonical_value="oregon",
                status="satisfied",
                evidence="URL contains 'oregon'",
            ),
            Constraint(
                text="under $40", kind="numeric",
                canonical_value="price",
                operator="lte", threshold="40", unit="USD",
            ),
        ],
    )
    out = _build_retry_instructions(
        original_instructions="find oregon white wines under $40",
        prior_response="bailed via request_help",
        unsatisfied_steps=["apply price filter"],
        task_brief=brief,
        failed_tactics=["price-accordion-click-no-expansion"],
    )
    assert "## ALREADY VERIFIED CONSTRAINTS (2 of 3 — DO NOT RE-DO)" in out
    assert "white wine" in out
    assert "oregon" in out
    assert "## REMAINING TO VERIFY (1 — focus here)" in out
    assert "price" in out
    assert "lte 40USD" in out
    assert "## FAILED TACTICS" in out


def test_build_retry_instructions_handles_no_brief() -> None:
    from superbrowser_bridge.orchestrator_tools import _build_retry_instructions

    out = _build_retry_instructions(
        original_instructions="x",
        prior_response="y",
        unsatisfied_steps=["a"],
    )
    # No brief — verified/remaining blocks omitted, but template still renders
    assert "ORIGINAL USER TASK" in out
    assert "ALREADY VERIFIED" not in out


def test_build_retry_instructions_failed_constraints_section() -> None:
    from superbrowser_bridge.orchestrator_tools import _build_retry_instructions
    from superbrowser_bridge.task_brief import TaskBrief, Constraint

    brief = TaskBrief(
        original_query="x",
        constraints=[
            Constraint(
                canonical_value="pets", kind="negative",
                status="failed", evidence="Pets allowed visible",
            ),
            Constraint(canonical_value="parking", kind="filter"),
        ],
    )
    out = _build_retry_instructions(
        original_instructions="x", prior_response="", unsatisfied_steps=[],
        task_brief=brief,
    )
    assert "CONSTRAINTS THAT FAILED" in out
    assert "pets" in out
    # Failed gets its own section + advice
    assert "DIFFERENT angle" in out


def main() -> int:
    tests = [
        # H
        test_take_recent_for_domain_returns_most_recent_match,
        test_take_recent_for_domain_pops_entry,
        test_take_recent_for_domain_skips_other_domains,
        test_take_recent_for_domain_empty_domain_returns_none,
        # I
        test_merge_brief_progress_copies_satisfied_statuses,
        test_merge_brief_progress_fuzzy_token_match,
        test_merge_brief_progress_does_not_overwrite_set_status,
        test_merge_brief_progress_handles_none_inputs,
        # J
        test_build_retry_instructions_renders_verified_block,
        test_build_retry_instructions_handles_no_brief,
        test_build_retry_instructions_failed_constraints_section,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"ok  {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {t.__name__}: {exc}")
        except Exception as exc:
            failed += 1
            print(f"ERR  {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
