"""Cross-task per-domain procedural memory.

Engineering boundary: this is the ONLY cross-task memory in
superbrowser. Everything else dies with the task_id directory.
Use sparingly; this lives forever (until /tmp/superbrowser/site_models/
is cleared) and grows monotonically with the number of unique
domains the agent has visited.

The site model encodes "this site behaves like X" — the kind of
procedural prior a human picks up after a few visits to a site
("filter sidebar bbox clicks are unreliable; use DOM index 49 to
expand Region first"). Stored as a small JSON file per eTLD+1
domain at ``/tmp/superbrowser/site_models/{domain}.json``.

Lifecycle:

- **Load** (orchestrator side, at task start or first navigation):
  if a model file exists for the goal URL's domain, the orchestrator
  Memory ingests it as a single synthetic fact
  ``site_model_{domain} = <summary>`` plus the dead-target list is
  re-materialized as DeadEnd entries on the ledger so they show up
  under the URL-keyed render block.

- **Save** (at task end): ``merge_from_ledger`` distills the task's
  ledger into behavior_notes (from facts of category=derived or
  observation that map to constraints/preferences) and dead_targets
  (from URL-tagged DeadEnd entries) and overwrites the per-domain
  file.

Schema (one file per domain):

::

    {
      "domain": "wineaccess.com",
      "first_seen": "2026-05-15T15:18:27",
      "last_seen": "2026-05-15T15:20:54",
      "task_count": 3,
      "behavior_notes": ["..."],
      "dead_targets": [
        {
          "url_pattern": "/store/white-wine/",
          "description": "Oregon checkbox in Region sidebar",
          "cause": "stale_selector",
          "failure_count": 5
        }
      ]
    }
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from loguru import logger

if TYPE_CHECKING:
    from .ledger import Ledger


_BASE_DIR = Path("/tmp/superbrowser/site_models")
_MAX_NOTES = 20
_MAX_DEAD_TARGETS = 30


def _domain_of(url: str) -> str:
    """Extract a sanitized eTLD+1 domain from a URL.

    Empty string for bad input. Sanitized by stripping ``www.`` and
    any characters that would be invalid in a filename — paranoia
    against URL fragments that contain ``/`` or ``:``.
    """
    if not url:
        return ""
    try:
        netloc = urlparse(url).netloc
    except Exception:
        return ""
    if not netloc:
        return ""
    if netloc.startswith("www."):
        netloc = netloc[4:]
    # Drop port + sanitize any remaining illegal chars
    netloc = netloc.split(":", 1)[0]
    return "".join(c for c in netloc if c.isalnum() or c in ".-")


class SiteModelStore:
    """Filesystem-backed cross-task site model."""

    @classmethod
    def _path_for(cls, domain: str) -> Path:
        _BASE_DIR.mkdir(parents=True, exist_ok=True)
        return _BASE_DIR / f"{domain}.json"

    @classmethod
    def domain_of(cls, url: str) -> str:
        return _domain_of(url)

    @classmethod
    def load(cls, domain: str) -> dict[str, Any] | None:
        """Return the site model dict for this domain, or None."""
        if not domain:
            return None
        path = cls._path_for(domain)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("SiteModel load failed at {}: {}", path, exc)
        return None

    @classmethod
    def save(cls, domain: str, model: dict[str, Any]) -> None:
        """Atomically write the site model for this domain."""
        if not domain:
            return
        path = cls._path_for(domain)
        try:
            tmp = path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(
                    model,
                    f,
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
            tmp.replace(path)
        except OSError as exc:
            logger.debug("SiteModel save failed at {}: {}", path, exc)

    @classmethod
    def merge_from_ledger(
        cls,
        ledger: "Ledger",
        *,
        success: bool,
        now: float | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Update site models with lessons from the just-completed task.

        Walks the ledger's URL-tagged dead_ends and groups them by
        domain. For each domain we touch, merge with the existing
        site model file (creating one if absent), update
        ``last_seen`` + ``task_count``, and append new behavior_notes
        derived from constraint/preference/derived facts.

        Returns a dict ``{domain: model}`` for callers that want to
        log or inspect what was written.
        """
        per_domain_dead: dict[str, list[Any]] = {}
        for d in ledger.dead_ends:
            if not d.url:
                continue
            dom = _domain_of(d.url)
            if not dom:
                continue
            per_domain_dead.setdefault(dom, []).append(d)

        # Constraints / preferences / derived facts become behavior
        # notes — they survive the task because they describe the SITE,
        # not the goal.
        notes_facts: list[Any] = [
            f
            for f in ledger.facts.values()
            if f.category in ("constraint", "preference", "derived")
        ]

        timestamp = now or time.time()
        iso = datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")

        # Domains seen via facts (their source_step URL is unknown
        # at the ledger level; we use ledger.current_url as a
        # weak attribution). Add the current domain too so a successful
        # task on a site at least bumps task_count.
        all_domains: set[str] = set(per_domain_dead.keys())
        cur_dom = _domain_of(ledger.current_url) if ledger.current_url else ""
        if cur_dom:
            all_domains.add(cur_dom)

        written: dict[str, dict[str, Any]] = {}
        for dom in all_domains:
            existing = cls.load(dom) or {
                "domain": dom,
                "first_seen": iso,
                "last_seen": iso,
                "task_count": 0,
                "behavior_notes": [],
                "dead_targets": [],
            }
            existing["last_seen"] = iso
            existing["task_count"] = int(existing.get("task_count", 0)) + 1

            # Merge dead_targets — dedupe by (url_pattern + description)
            # and bump failure_count when the same target re-fails.
            existing_dead = existing.get("dead_targets") or []
            keyed: dict[tuple[str, str], dict[str, Any]] = {
                (entry.get("url_pattern", ""), entry.get("description", "")): entry
                for entry in existing_dead
                if isinstance(entry, dict)
            }
            for d in per_domain_dead.get(dom, []):
                url_pattern = ""
                try:
                    url_pattern = urlparse(d.url).path or "/"
                except Exception:
                    url_pattern = "/"
                key = (url_pattern, d.description)
                if key in keyed:
                    keyed[key]["failure_count"] = int(
                        keyed[key].get("failure_count", 1)
                    ) + 1
                    # Most recent cause wins (causes are coarse so
                    # collisions are rare).
                    keyed[key]["cause"] = d.cause
                else:
                    keyed[key] = {
                        "url_pattern": url_pattern,
                        "description": d.description,
                        "cause": d.cause,
                        "failure_count": 1,
                    }
            # Trim oldest entries if we exceed the cap.
            merged_dead = sorted(
                keyed.values(),
                key=lambda e: int(e.get("failure_count", 1)),
                reverse=True,
            )[:_MAX_DEAD_TARGETS]
            existing["dead_targets"] = merged_dead

            # Behavior notes — append novel constraint/preference/derived
            # fact values; dedupe by value string; cap to _MAX_NOTES.
            existing_notes = list(existing.get("behavior_notes") or [])
            existing_set = set(existing_notes)
            for f in notes_facts:
                line = f"{f.key}: {f.value}"
                if line not in existing_set:
                    existing_notes.append(line)
                    existing_set.add(line)
            if len(existing_notes) > _MAX_NOTES:
                existing_notes = existing_notes[-_MAX_NOTES:]
            existing["behavior_notes"] = existing_notes

            cls.save(dom, existing)
            written[dom] = existing
        return written


def ingest_into_orchestrator(
    memory: Any, url: str
) -> tuple[int, int]:
    """Load the site model for ``url``'s domain into the orchestrator.

    Promotes:
    - ``behavior_notes`` -> a single ``site_model_{domain}`` fact
      (category=derived) carrying the joined notes.
    - ``dead_targets`` -> per-entry DeadEnd records on the orchestrator
      ledger, so URL-keyed render shows the prior failures.

    Returns ``(notes_count, dead_targets_count)`` for observability.
    """
    if memory is None:
        return (0, 0)
    domain = _domain_of(url)
    if not domain:
        return (0, 0)
    model = SiteModelStore.load(domain)
    if not model:
        return (0, 0)

    notes = model.get("behavior_notes") or []
    dead_targets = model.get("dead_targets") or []
    task_count = int(model.get("task_count", 0))

    # Always create the marker fact so the orchestrator gets a clear
    # "we've been here before" signal even when behavior_notes is empty
    # (e.g., first revisit after a task that only logged dead-ends).
    notes_count = 0
    try:
        if notes:
            summary = "; ".join(notes[:8])[:480]
        else:
            summary = (
                f"visited {task_count} prior task(s); "
                f"{len(dead_targets)} dead target(s) on record"
            )
        memory.remember(
            f"site_model_{domain}",
            summary,
            category="derived",
            confidence=0.85,
        )
        notes_count = len(notes)
    except Exception as exc:  # pragma: no cover
        logger.debug("site_model fact ingest failed: {}", exc)

    dead_count = 0
    for entry in dead_targets:
        if not isinstance(entry, dict):
            continue
        url_pattern = entry.get("url_pattern", "/") or "/"
        synth_url = f"https://{domain}{url_pattern}"
        desc = entry.get("description", "") or ""
        cause = entry.get("cause", "unknown") or "unknown"
        if not desc:
            continue
        try:
            memory.mark_dead_end(
                desc,
                url=synth_url,
                cause=cause,
            )
            dead_count += 1
        except Exception as exc:  # pragma: no cover
            logger.debug("site_model dead_end ingest failed: {}", exc)

    try:
        memory.events.log(
            "site_model_ingested",
            {
                "domain": domain,
                "notes": notes_count,
                "dead_targets": dead_count,
                "task_count": int(model.get("task_count", 0)),
            },
        )
    except Exception:
        pass

    return (notes_count, dead_count)
