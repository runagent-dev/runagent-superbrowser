"""Unit tests for the SQLite-backed routing ledger.

Verifies the lost-write race the JSON-file backend suffered from is
gone, and that load/upsert/migration round-trip the same dict shape.

No external services required. Run:
    source venv/bin/activate && \
        python nanobot/superbrowser_bridge/tests/test_routing_store.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone


def _isolate_store():
    """Point routing_store at a fresh tmpdir and reset the singleton.

    Imported lazily so callers can monkey-patch LEARNINGS_DIR before
    routing_store opens its connection.
    """
    from superbrowser_bridge import routing, routing_store
    tmpdir = tempfile.mkdtemp(prefix="routing-test-")
    routing.LEARNINGS_DIR = tmpdir
    routing_store.LEARNINGS_DIR = tmpdir
    routing_store.DB_PATH = os.path.join(tmpdir, "routing.sqlite")
    routing_store._conn = None
    routing_store._migrated = False
    return tmpdir


def test_load_and_upsert_roundtrip() -> None:
    tmpdir = _isolate_store()
    from superbrowser_bridge import routing_store
    try:
        assert routing_store.load("example.com") is None

        def _add(d: dict) -> dict:
            d["domain"] = "example.com"
            d["count"] = int(d.get("count", 0)) + 1
            d["tier_outcomes"] = {"1": "success"}
            return d

        routing_store.upsert("example.com", _add)
        routing_store.upsert("example.com", _add)
        result = routing_store.load("example.com")
        assert result is not None, "load returned None after upsert"
        assert result["count"] == 2, result
        assert result["tier_outcomes"] == {"1": "success"}, result
    finally:
        shutil.rmtree(tmpdir)
    print("✓ test_load_and_upsert_roundtrip")


def test_concurrent_writes_no_lost_updates() -> None:
    """The point of the SQLite migration: 10 threads × 100 increments
    each must converge to count == 1000 without any loss.

    Under the JSON-file implementation this regularly drops 50–200
    increments because of the read-modify-write race between concurrent
    workers.
    """
    tmpdir = _isolate_store()
    from superbrowser_bridge import routing_store
    try:
        def _inc(d: dict) -> dict:
            d["count"] = int(d.get("count", 0)) + 1
            return d

        errors: list[Exception] = []

        def worker() -> None:
            try:
                for _ in range(100):
                    routing_store.upsert("counter.com", _inc)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"thread errors: {errors}"
        result = routing_store.load("counter.com")
        assert result is not None
        assert result["count"] == 1000, (
            f"expected 1000 increments preserved, got {result['count']}"
        )
    finally:
        shutil.rmtree(tmpdir)
    print("✓ test_concurrent_writes_no_lost_updates")


def test_mirror_writes_to_legacy_json() -> None:
    """`upsert` mirrors back to the per-domain JSON file so legacy
    readers (the cutover path in routing.py) keep working.
    """
    tmpdir = _isolate_store()
    from superbrowser_bridge import routing, routing_store
    try:
        routing_store.upsert(
            "mirror.example.com",
            lambda d: {**d, "lowest_successful_tier": 1},
        )
        path = routing._routing_path("mirror.example.com")
        assert os.path.exists(path), f"mirror file not written at {path}"
        with open(path) as fp:
            content = json.load(fp)
        assert content.get("lowest_successful_tier") == 1, content
    finally:
        shutil.rmtree(tmpdir)
    print("✓ test_mirror_writes_to_legacy_json")


def test_migration_picks_up_existing_json() -> None:
    """First open of the SQLite db should suck in any pre-existing
    .routing.json files so the cutover doesn't lose history.
    """
    tmpdir = _isolate_store()
    try:
        # Drop a .routing.json file BEFORE the first SQLite open.
        legacy_path = os.path.join(tmpdir, "legacy.example.com.routing.json")
        with open(legacy_path, "w") as fp:
            json.dump({
                "domain": "legacy.example.com",
                "lowest_successful_tier": 3,
                "tier_outcomes": {"1": "fail:antibot_403"},
            }, fp)

        # Force a fresh connection so migration runs.
        from superbrowser_bridge import routing_store
        routing_store._conn = None
        routing_store._migrated = False

        loaded = routing_store.load("legacy.example.com")
        assert loaded is not None, "legacy domain not migrated"
        assert loaded["lowest_successful_tier"] == 3
        assert loaded["tier_outcomes"] == {"1": "fail:antibot_403"}
    finally:
        shutil.rmtree(tmpdir)
    print("✓ test_migration_picks_up_existing_json")


def test_transient_failure_does_not_poison_tier() -> None:
    """A transient (timeout, short rate-limit) failure on T1 must NOT
    flip `tier_outcomes['1']` to fail — that would force every future
    request on this domain to start on T3 unnecessarily.
    """
    tmpdir = _isolate_store()
    from superbrowser_bridge import routing, routing_store
    try:
        # Transient timeout — should land in tier_transient_failures only.
        routing._record_routing_outcome(
            "flaky.example.com", approach="browser",
            success=False, tier=1, block_class="timeout",
        )
        data = routing_store.load("flaky.example.com")
        assert data is not None
        assert data.get("tier_outcomes", {}) == {}, (
            f"transient failure leaked into tier_outcomes: {data['tier_outcomes']}"
        )
        assert len(data.get("tier_transient_failures", [])) == 1
        # Permanent block — SHOULD flip tier_outcomes['1'].
        routing._record_routing_outcome(
            "blocked.example.com", approach="browser",
            success=False, tier=1, block_class="antibot_403",
        )
        data = routing_store.load("blocked.example.com")
        assert data is not None
        assert data.get("tier_outcomes", {}).get("1", "").startswith("fail"), data
    finally:
        shutil.rmtree(tmpdir)
    print("✓ test_transient_failure_does_not_poison_tier")


def test_ttl_decays_t3_graduation() -> None:
    """A domain graduated to T3 30+ days ago should get a chance to
    re-prove itself on a cheaper tier — the site may have loosened
    bot-detection in the interim.
    """
    tmpdir = _isolate_store()
    from superbrowser_bridge import routing, routing_store
    # This test exercises read-time TTL logic in choose_starting_tier,
    # which is gated by LEARNING_READS_ENABLED (default off in production).
    prior = os.environ.get("LEARNING_READS_ENABLED")
    os.environ["LEARNING_READS_ENABLED"] = "1"
    try:
        # Stale T3 graduation, 60 days back.
        old_ts = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()

        def _seed(d: dict) -> dict:
            d["lowest_successful_tier"] = 3
            d["lowest_successful_tier_last_seen"] = old_ts
            return d

        routing_store.upsert("graduated.example.com", _seed)
        # Default TTL is 30 days → should suggest a cheaper retry.
        chosen = routing.choose_starting_tier("graduated.example.com")
        assert chosen == 2, f"expected TTL-decayed retry tier 2, got {chosen}"
        # Pending flag set so the next failure re-promotes immediately.
        data = routing_store.load("graduated.example.com")
        assert data and data.get("tier_retry_pending") is True

        # Fresh T3 graduation — TTL should NOT fire.
        def _seed_fresh(d: dict) -> dict:
            d["lowest_successful_tier"] = 3
            d["lowest_successful_tier_last_seen"] = datetime.now(timezone.utc).isoformat()
            d["tier_retry_pending"] = False
            return d

        routing_store.upsert("fresh.example.com", _seed_fresh)
        chosen = routing.choose_starting_tier("fresh.example.com")
        assert chosen == 3, f"fresh T3 graduation should stay at 3, got {chosen}"
    finally:
        if prior is None:
            os.environ.pop("LEARNING_READS_ENABLED", None)
        else:
            os.environ["LEARNING_READS_ENABLED"] = prior
        shutil.rmtree(tmpdir)
    print("✓ test_ttl_decays_t3_graduation")


def main() -> int:
    tests = [
        test_load_and_upsert_roundtrip,
        test_concurrent_writes_no_lost_updates,
        test_mirror_writes_to_legacy_json,
        test_migration_picks_up_existing_json,
        test_transient_failure_does_not_poison_tier,
        test_ttl_decays_t3_graduation,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as exc:
            failed += 1
            print(f"✗ {t.__name__}: {exc}")
        except Exception as exc:
            failed += 1
            print(f"✗ {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
