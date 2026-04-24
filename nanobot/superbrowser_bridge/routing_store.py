"""SQLite-backed per-domain routing ledger.

The previous implementation stored each domain's routing data as a
separate JSON file under `LEARNINGS_DIR`. That worked at single-process
scale, but every call site followed a `read JSON → modify dict → write
JSON` pattern with no locking. When two workers (or two parts of the
same worker) updated the same domain concurrently, the later write
clobbered the earlier one — losing tier outcomes, CF failure streaks,
and learned preferences. We saw this in production: tier ledgers
"forgetting" a T1 success that had just been recorded.

This module is a drop-in atomic backend. It exposes two operations:

    load(domain)              -> dict | None
    upsert(domain, mutator)   -> dict   (mutator: dict -> dict)

The mutator runs inside a single transaction. WAL mode lets concurrent
readers (e.g. `choose_starting_tier`) proceed without blocking writers.
On first import, JSON files in `LEARNINGS_DIR` are migrated into the
SQLite database; the JSON files are left in place as a one-release
back-compat read path.

The schema is intentionally one-table-one-row-per-domain with the data
serialized as a JSON blob — the existing code expects dict shapes and
this lets us swap the backend without rewriting every call site.
"""

from __future__ import annotations

import json as _json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from superbrowser_bridge.routing import LEARNINGS_DIR, _routing_path

logger = logging.getLogger(__name__)

# Database lives next to the per-domain JSON files so backups and
# inspection workflows still find it in a predictable place. WAL files
# (.db-wal, .db-shm) are written to the same directory.
DB_PATH = str(Path(LEARNINGS_DIR) / "routing.sqlite")

# Per-process connection guarded by a lock so SQLite isn't shared across
# threads without serialization. Connection is created lazily on first
# use to avoid touching the filesystem at import time of routing.py.
_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_migrated = False


def _open_db() -> sqlite3.Connection:
    """Open (and lazily create) the WAL-mode SQLite connection.

    `isolation_level=None` puts us in autocommit mode so we can issue
    explicit BEGIN/COMMIT around `upsert` transactions. WAL gives
    concurrent reader/writer semantics so `choose_starting_tier` (read)
    doesn't block `_record_routing_outcome` (write).
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(
        DB_PATH,
        isolation_level=None,  # autocommit; we run our own transactions
        timeout=10.0,           # wait up to 10s if another writer holds the lock
        check_same_thread=False,
    )
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS routing (
            domain TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    return conn


def _get_conn() -> sqlite3.Connection:
    global _conn, _migrated
    with _lock:
        if _conn is None:
            _conn = _open_db()
            if not _migrated:
                try:
                    _migrate_from_json(_conn)
                except Exception as exc:
                    # Migration is best-effort — even if a single file is
                    # corrupt we want fresh SQLite-only writes to keep
                    # working. The legacy JSON files stay on disk so the
                    # operator can recover by hand.
                    logger.info("routing_store migration partial: %s", exc)
                _migrated = True
        return _conn


def _migrate_from_json(conn: sqlite3.Connection) -> int:
    """Bulk-import existing *.routing.json files into the SQLite db.

    Idempotent — only inserts a domain when the table doesn't already
    have a row for it, so re-running on an already-migrated db is safe.
    Returns the number of rows inserted.
    """
    learnings_dir = Path(LEARNINGS_DIR)
    if not learnings_dir.exists():
        return 0
    existing: set[str] = set()
    for row in conn.execute("SELECT domain FROM routing"):
        existing.add(str(row[0]))
    inserted = 0
    for path in learnings_dir.glob("*.routing.json"):
        # Path stem like "www.example.com.routing" — strip the suffix.
        stem = path.name[: -len(".routing.json")]
        # Reverse the safe-name transform from _routing_path() — it
        # replaces "/" and ":" with "_". The original domain rarely
        # contains those characters, so the stem usually IS the domain.
        domain = stem
        if domain in existing:
            continue
        try:
            with path.open() as fp:
                data = _json.load(fp)
        except (OSError, _json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        try:
            conn.execute(
                "INSERT INTO routing(domain, data, updated_at) VALUES (?, ?, ?)",
                (domain, _json.dumps(data), time.time()),
            )
            inserted += 1
        except sqlite3.IntegrityError:
            continue  # raced with another process — fine
    if inserted:
        logger.info("routing_store migrated %d domain(s) from JSON", inserted)
    return inserted


# --- Public API -----------------------------------------------------------

def load(domain: str) -> Optional[dict]:
    """Return the stored dict for `domain`, or None if no row exists."""
    if not domain:
        return None
    try:
        conn = _get_conn()
        with _lock:
            row = conn.execute(
                "SELECT data FROM routing WHERE domain = ?", (domain,),
            ).fetchone()
        if not row:
            return None
        try:
            data = _json.loads(row[0])
        except _json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None
    except sqlite3.Error as exc:
        logger.info("routing_store load failed for %s: %s", domain, exc)
        return None


def upsert(domain: str, mutator: Callable[[dict], dict]) -> dict:
    """Atomically read-modify-write the row for `domain`.

    `mutator` receives the current dict (or an empty dict if no row
    exists yet) and returns the new dict. Wrapped in a SQLite
    transaction so concurrent writers can't lose each other's updates —
    that's the bug this module exists to fix.

    Also mirrors the resulting dict back to the legacy JSON file so the
    existing read paths in `routing.py` (which still os.path.exists on
    the JSON) keep working during the cutover. Mirror writes are
    best-effort; failure does not roll back the SQLite write.
    """
    if not domain:
        return {}
    conn = _get_conn()
    with _lock:
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT data FROM routing WHERE domain = ?", (domain,),
            ).fetchone()
            current: dict
            if row:
                try:
                    current = _json.loads(row[0])
                    if not isinstance(current, dict):
                        current = {}
                except _json.JSONDecodeError:
                    current = {}
            else:
                current = {}
            try:
                new_data = mutator(current)
            except Exception:
                conn.execute("ROLLBACK")
                raise
            if not isinstance(new_data, dict):
                conn.execute("ROLLBACK")
                raise TypeError(
                    f"mutator must return dict, got {type(new_data).__name__}"
                )
            conn.execute(
                "INSERT INTO routing(domain, data, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(domain) DO UPDATE SET data=excluded.data, "
                "updated_at=excluded.updated_at",
                (domain, _json.dumps(new_data), time.time()),
            )
            conn.execute("COMMIT")
        except sqlite3.Error:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    # Mirror to the legacy JSON path so any code that still reads
    # _routing_path() directly (via os.path.exists + json.load) sees
    # the latest data. Cleared in a follow-up release once all callers
    # route through this module.
    try:
        legacy_path = _routing_path(domain)
        with open(legacy_path, "w") as fp:
            _json.dump(new_data, fp, indent=2)
    except OSError:
        pass

    return new_data


def all_domains() -> Iterable[str]:
    """Iterate every known domain (used by audit / cleanup tooling)."""
    try:
        conn = _get_conn()
        with _lock:
            rows = conn.execute("SELECT domain FROM routing ORDER BY domain").fetchall()
        return [str(r[0]) for r in rows]
    except sqlite3.Error:
        return []


def delete(domain: str) -> bool:
    """Remove a domain row. Returns True if a row was removed."""
    if not domain:
        return False
    try:
        conn = _get_conn()
        with _lock:
            cur = conn.execute("DELETE FROM routing WHERE domain = ?", (domain,))
            return cur.rowcount > 0
    except sqlite3.Error:
        return False


__all__ = [
    "DB_PATH",
    "load",
    "upsert",
    "all_domains",
    "delete",
]
