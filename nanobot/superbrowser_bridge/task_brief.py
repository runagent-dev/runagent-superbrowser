"""
TaskBrief — structured constraint tracking for multi-condition browser tasks.

When the orchestrator delegates a query like "white wines from Oregon under
$40 that pair with both dessert and fish", the worker's brain has been
shown to lose track of individual filters as the iteration window grows. The
free-text task block drifts back in the message history; the model finishes
2 of 5 constraints and reports a half-result.

This module turns the orchestrator's pre-decomposed checklist into a live
state object. The worker hook reconciles it from the current URL and the
last vision response on every iteration, then re-renders [BRIEF]/[FOCUS]/
[CHECKLIST] blocks into the next tool result. The rendering shape mirrors
``form_session.remaining_checklist`` so the brain sees a consistent style.

Reconciliation is deliberately conservative — it only flips ``open -> done``
when at least one predicate matches. It never reverses a flip and never
guesses when evidence is absent. Action/extraction items use ``manual: true``
and are flipped via ``browser_brief_mark``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional


# Allowed status values for a constraint. The reconciler only ever moves
# items through ``open -> done`` automatically; ``failed`` /
# ``not_applicable`` are set manually via ``mark()``.
_STATUSES = ("open", "active", "done", "failed", "not_applicable")
# Allowed kinds. The reconciler treats them all the same; ``kind`` is purely
# a hint for the brain about what flavor of progress to make.
_KINDS = ("filter", "action", "extraction", "navigation", "verification")

# Phase M: stop-words excluded from token-overlap validation in
# `_evidence_validation_error`. These appear in too many constraint
# labels / evidence strings to be discriminating.
_GENERIC_EVIDENCE_STOPWORDS: frozenset[str] = frozenset({
    "filter", "section", "type", "kind", "options", "option", "value",
    "values", "the", "and", "for", "with", "from", "into", "this",
    "that", "any", "all", "shows", "showing", "show", "page", "result",
    "results", "list", "item", "items", "active", "applied", "selected",
    "set", "done", "match", "matches", "matched", "manual", "default",
    "page_text", "page_url", "url_contains", "url_param", "url_query",
    "vision_active_label", "predicate", "constraint",
    # HTTP/URL plumbing tokens that shouldn't credit evidence.
    "https", "http", "www", "com", "org", "net", "store", "search",
    "catalog", "category", "categories",
})


@dataclass
class Constraint:
    """One element of the task checklist."""

    id: int
    label: str
    kind: str = "filter"
    predicate: dict = field(default_factory=dict)
    status: str = "open"
    evidence: str = ""
    # Per-focus attempt log. Each entry is {tool, target, result_kind, iter}
    # appended by the worker hook from step_history while this constraint
    # was the focus. Used by `failed_attempts_on()` + `[FOCUS_EXHAUSTED]`.
    attempts: list[dict] = field(default_factory=list)
    # Threshold levels at which `[FOCUS_EXHAUSTED]` already fired. Prevents
    # the directive from spamming on every iteration after the threshold.
    nudges_emitted: set[int] = field(default_factory=set)

    def is_open(self) -> bool:
        return self.status in ("open", "active")

    def is_terminal(self) -> bool:
        return self.status in ("done", "failed", "not_applicable")


class TaskBrief:
    """Live state of the user's decomposed query."""

    def __init__(self, original_query: str, constraints: Iterable[dict]):
        self.original_query = original_query or ""
        self.version = 1
        self.constraints: list[Constraint] = []
        for i, raw in enumerate(constraints, start=1):
            if not isinstance(raw, dict):
                continue
            label = str(raw.get("label") or "").strip()
            if not label:
                continue
            kind = raw.get("kind") or "filter"
            if kind not in _KINDS:
                kind = "filter"
            predicate = raw.get("predicate") or {}
            if not isinstance(predicate, dict):
                predicate = {}
            self.constraints.append(
                Constraint(id=i, label=label, kind=kind, predicate=predicate)
            )

    # ---------------------------------------------------------------- core

    def open_count(self) -> int:
        return sum(1 for c in self.constraints if c.is_open())

    def done_count(self) -> int:
        return sum(1 for c in self.constraints if c.status == "done")

    def is_complete(self) -> bool:
        return all(c.is_terminal() for c in self.constraints)

    def next_focus(self) -> Optional[Constraint]:
        """First open|active constraint, or None when all terminal."""
        active = [c for c in self.constraints if c.status == "active"]
        if active:
            return active[0]
        for c in self.constraints:
            if c.status == "open":
                return c
        return None

    def mark(
        self,
        constraint_id: int,
        status: str,
        evidence: str = "",
        validate_evidence: bool = False,
    ) -> bool:
        """Set a constraint's status. Returns True if the constraint
        existed and the status was applied. Idempotent — re-marking with
        the same status is a no-op but still returns True.

        Phase M: when ``validate_evidence`` is True (passed by
        ``BrowserBriefMarkTool``), filter constraints marked ``done``
        require the evidence string to reference at least one
        predicate-relevant token. Prevents the wineaccess case where
        the brain marked "Oregon region" done with evidence about
        "United States" — wrong region, real cascade trigger.
        """
        if status not in _STATUSES:
            return False
        for c in self.constraints:
            if c.id == constraint_id:
                if (
                    validate_evidence
                    and status == "done"
                    and c.kind == "filter"
                ):
                    err = self._evidence_validation_error(c, evidence or "")
                    if err:
                        # Stash the error for the caller — the tool layer
                        # converts it into a structured refusal message.
                        # Don't flip status; mark as a sentinel so the
                        # tool can detect it.
                        c.evidence = (
                            (c.evidence or "")
                            + f" [refused:{err}]"
                        )[:200]
                        return False
                if c.status != status:
                    c.status = status
                    c.evidence = (evidence or c.evidence)[:200]
                    self.version += 1
                return True
        return False

    @staticmethod
    def _evidence_validation_error(
        constraint: "Constraint",
        evidence: str,
    ) -> str:
        """Phase M: return a short error string when evidence does NOT
        reference a predicate-VALUE token, or empty string when
        evidence is acceptable.

        Strategy: predicate VALUE tokens (vision_active_label items,
        url_contains items, url_param values) are the ground truth —
        they're the specific things that prove the constraint was
        actually applied. Label tokens are display-only and contain
        category words ("region", "filter") that match indiscriminately.
        We prefer value tokens over label tokens; only fall back to
        label tokens when the predicate has no value tokens (i.e.
        `manual: True` with nothing else) AND the label has at least
        one non-stop-word token.
        """
        ev_lower = (evidence or "").lower()
        if not ev_lower.strip():
            return "empty_evidence"

        import re as _re_local

        # Predicate-value tokens come from the actual values that prove
        # the constraint is satisfied. For "Oregon region" with
        # predicate.vision_active_label=['Oregon'], the only acceptable
        # evidence token is "oregon" — NOT "region".
        value_tokens: set[str] = set()
        # Label tokens are a fallback used only when predicate has no
        # values (manual-only constraint).
        label_tokens: set[str] = set()

        def _add(target: set[str], s: str) -> None:
            for m in _re_local.findall(r"[a-z0-9]{3,}", s.lower()):
                if m in _GENERIC_EVIDENCE_STOPWORDS:
                    continue
                target.add(m)

        # Walk predicate looking ONLY at string values, skipping the
        # `manual` boolean and predicate-key strings (they're metadata,
        # not values).
        def _walk_values(obj: object) -> None:
            if isinstance(obj, str):
                _add(value_tokens, obj)
            elif isinstance(obj, bool):
                return  # `manual: True` etc.
            elif isinstance(obj, (list, tuple, set)):
                for x in obj:
                    _walk_values(x)
            elif isinstance(obj, dict):
                for v in obj.values():
                    _walk_values(v)
        _walk_values(constraint.predicate or {})
        _add(label_tokens, constraint.label)
        # Filter label tokens that are ALSO in the stop-word list
        # (already done by _add) AND drop common category words even
        # when not in the global stopword list — these appear in
        # multiple constraints' labels and aren't discriminating.
        # ("region" in "Oregon region" is the category, "oregon" is
        # the discriminating part.)
        label_category_words: set[str] = {
            "region", "country", "state", "color", "size", "brand",
            "year", "rating", "score", "stars", "review", "reviewed",
            "price", "type", "kind", "variety", "varietal", "style",
            "pairing", "pairings", "pairs", "wines", "wine",
        }
        label_tokens -= label_category_words

        # Choose the active token set.
        if value_tokens:
            tokens = value_tokens
        elif label_tokens:
            tokens = label_tokens
        else:
            # No discriminating tokens available — skip validation.
            # Constraint is unverifiable from evidence text alone.
            return ""

        # Tokenize evidence and match.
        ev_tokens = set(_re_local.findall(r"[a-z0-9]{3,}", ev_lower))
        # Don't credit the same generic category words on the evidence
        # side either — "region" in evidence shouldn't satisfy a
        # "region"-categorized constraint.
        ev_tokens -= label_category_words
        if tokens & ev_tokens:
            return ""
        # No overlap. Build a hint with the expected tokens.
        expected = ", ".join(sorted(tokens)[:8])
        return f"evidence_doesnt_reference_predicate (expected one of: {expected})"

    # --------------------------------------------------------- reconciliation

    def reconcile_from_url(self, url: str) -> bool:
        """Flip predicates that match the current URL.

        Returns True if at least one constraint was flipped this call.
        Conservative: only ``open -> done`` transitions; ignores already-
        terminal items.
        """
        if not url:
            return False
        flipped = False
        url_l = url.lower()
        # Phase O: detect search-query paths. URLs like
        # `/store/search/<query>/` have the user's query as a path
        # segment — that is NOT a filter being applied. The brain on
        # wineaccess marked "Oregon region" as done because the search
        # URL `/store/search/white wine Oregon/` contained "oregon" via
        # url_contains predicate. Build a "search-stripped" URL that
        # excludes the search path so the substring match doesn't
        # auto-flip on text-search.
        try:
            from urllib.parse import urlparse, urlunparse
            _parsed = urlparse(url)
            _path = _parsed.path or ""
            # Common search-path patterns: /search/<query>, /store/search/
            # <query>, /results/<query>. The query is everything AFTER
            # the search marker — strip it out for url_contains matching.
            import re as _re_search
            _search_strip_re = _re_search.compile(
                r"/(?:store/)?(?:search|results|find)/[^?#]*",
                _re_search.IGNORECASE,
            )
            _stripped_path = _search_strip_re.sub("/", _path)
            url_no_search_l = urlunparse((
                _parsed.scheme, _parsed.netloc, _stripped_path,
                _parsed.params, _parsed.query, "",
            )).lower()
        except Exception:
            url_no_search_l = url_l
        # Lazily parse the query string only if any predicate needs it.
        params: Optional[dict[str, list[str]]] = None
        for c in self.constraints:
            if not c.is_open():
                continue
            pred = c.predicate or {}
            if pred.get("manual"):
                continue
            # url_contains: any substring match flips the item.
            # For `filter` constraints, match against the search-stripped
            # URL — text-search containing a value isn't the same as a
            # filter being applied. For `navigation` constraints,
            # original URL is fine (the brain navigating to a search
            # results page IS the goal of a navigation step).
            _haystack = (
                url_no_search_l if c.kind == "filter" else url_l
            )
            for sub in _str_list(pred.get("url_contains")):
                if sub.lower() in _haystack:
                    c.status = "done"
                    c.evidence = f"url~={sub}"
                    flipped = True
                    self.version += 1
                    break
            if not c.is_open():
                continue
            # url_param: any (key, value) pair present in the query string flips.
            up = pred.get("url_param")
            if isinstance(up, dict) and up:
                if params is None:
                    params = _parse_query(url)
                for k, vals in up.items():
                    if not isinstance(k, str):
                        continue
                    have = params.get(k.lower(), [])
                    for v in _str_list(vals):
                        if v.lower() in have:
                            c.status = "done"
                            c.evidence = f"url?{k}={v}"
                            flipped = True
                            self.version += 1
                            break
                    if not c.is_open():
                        break
        return flipped

    def reconcile_from_page_state(
        self,
        vision_response: Any = None,
        markdown: str = "",
    ) -> bool:
        """Flip predicates that match the current page text or vision labels.

        Two text channels:

        * ``vision_active_label`` — the strong signal. Bbox labels that
          vision flagged with ``is_selected`` / ``is_active``. Used by
          all kinds.
        * ``page_text`` — substrings of markdown / vision label list. The
          weak signal: a filter sidebar renders every option's label as
          plain text, so ``page_text=['Oregon']`` matches even when the
          Oregon filter was never clicked. To prevent that false positive
          we **ignore page_text for ``kind == 'filter'``**: filters must
          have URL evidence (``url_contains`` / ``url_param``) or an
          active vision label to flip done. Page_text stays useful for
          ``verification``, ``extraction``, and ``navigation`` kinds
          where "string visible anywhere" really does indicate progress.
        """
        if not vision_response and not markdown:
            return False

        # Build the haystack once. Lowercased for case-insensitive substring
        # matching. Cap the markdown to keep this cheap on long pages.
        parts: list[str] = []
        if markdown:
            parts.append(markdown[:20000])
        labels: list[str] = []
        active_labels: list[str] = []
        if vision_response is not None:
            labels = _vision_labels(vision_response)
            active_labels = _vision_active_labels(vision_response)
            parts.extend(labels)
            for attr in ("summary", "relevant_text"):
                txt = getattr(vision_response, attr, "")
                if isinstance(txt, str) and txt:
                    parts.append(txt)
        haystack = "\n".join(parts).lower()
        active_hay = "\n".join(active_labels).lower()
        if not haystack and not active_hay:
            return False

        flipped = False
        for c in self.constraints:
            if not c.is_open():
                continue
            pred = c.predicate or {}
            if pred.get("manual"):
                continue
            # vision_active_label takes precedence — strongest signal.
            # Always allowed regardless of kind because "active=true" is
            # already specific evidence.
            for sub in _str_list(pred.get("vision_active_label")):
                if sub and sub.lower() in active_hay:
                    c.status = "done"
                    c.evidence = f"vision-active~={sub}"
                    flipped = True
                    self.version += 1
                    break
            if not c.is_open():
                continue
            # page_text is the weak signal. For filter constraints the
            # haystack contains every sidebar option label, which gives
            # false positives — so we restrict page_text to non-filter
            # kinds. Filter constraints rely on URL evidence
            # (handled by reconcile_from_url) and vision_active_label.
            if c.kind == "filter":
                continue
            for sub in _str_list(pred.get("page_text")):
                if sub and sub.lower() in haystack:
                    c.status = "done"
                    c.evidence = f"page~={sub}"
                    flipped = True
                    self.version += 1
                    break
        return flipped

    # ------------------------------------------------------------- rendering

    def render_brief(self) -> str:
        """One-line header summarizing progress + current focus."""
        focus = self.next_focus()
        focus_slug = _slug(focus.label) if focus else "all-done"
        return (
            f"[BRIEF v={self.version}] checklist={self.done_count()}/"
            f"{len(self.constraints)} focus={focus_slug}"
        )

    def render_focus(self) -> str:
        focus = self.next_focus()
        if focus is None:
            return ""
        return (
            f"[FOCUS] #{focus.id} {focus.label!r} ({focus.kind}, {focus.status})"
            " — system recommends attacking next"
        )

    def render_checklist(self, max_lines: int = 12) -> str:
        if not self.constraints:
            return ""
        focus = self.next_focus()
        focus_id = focus.id if focus else -1
        done_ids = [c.id for c in self.constraints if c.status == "done"]
        failed_ids = [c.id for c in self.constraints if c.status == "failed"]
        remaining = [c for c in self.constraints if c.status not in ("done",)]
        header = (
            f"[CHECKLIST] {len(self.constraints)} items: "
            f"{len(done_ids)} done, {len(failed_ids)} failed, "
            f"{sum(1 for c in self.constraints if c.is_open())} remaining"
        )
        lines: list[str] = []
        if done_ids:
            done_str = ", ".join(f"#{i}" for i in done_ids)
            lines.append(
                f"  Done: {done_str}  (already complete — do not revisit)"
            )
        marks = {
            "open": "  ",
            "active": "  ",
            "failed": "[failed] ",
            "not_applicable": "[n/a]    ",
        }
        shown = 0
        for c in remaining:
            if shown >= max_lines:
                lines.append(f"  … +{len(remaining) - shown} more")
                break
            is_focus = c.id == focus_id
            arrow = "→" if is_focus else " "
            mark = marks.get(c.status, "  ")
            label = c.label[:60]
            ev = f" → {c.evidence}" if c.evidence else ""
            tag = (
                "  (active — work on this)"
                if is_focus
                else f"  ({c.status})"
            )
            lines.append(f"  {arrow} {mark}{c.id}) {label}{ev}{tag}")
            shown += 1
        return header + "\n" + "\n".join(lines)

    def render_for_prompt(self) -> str:
        """Initial-prompt rendering — used inside the worker's system
        prompt so the brain reads the full constraint list at task start.
        """
        return self.render_checklist(max_lines=50)

    def summary_open_items(self) -> str:
        """Human-readable list of still-open items, used by the
        orchestrator's post-run handler to surface partial-completion."""
        rows = [
            f"  - #{c.id} {c.label} ({c.kind}, {c.status})"
            for c in self.constraints if c.is_open()
        ]
        return "\n".join(rows)

    # ----------------------------------------------- per-focus attempt ledger
    #
    # The reactive guards (click crosscheck, repeat-type, filter-hack URL)
    # each refuse one bad call but the brain re-rolls a different bad call
    # against the same focus until a global limit catches it. Tracking
    # attempts per constraint lets the hook emit a kind-specific
    # `[FOCUS_EXHAUSTED]` directive with concrete tool-family pivots
    # before the brain burns through 5+ iterations.

    # Substring markers in `step.result` that indicate the call was
    # refused / rejected / failed. The hook normalizes the result string
    # to lowercase before checking, so all entries here are lowercase.
    _FAILURE_MARKERS = (
        "refused", "rejected", "mismatch", "failed", "blocked",
        "stale_index", "dead_click", "dead_type", "loop_detected",
        "no_vision_match", "rush_refused", "epoch_too_old",
        "filter_hack", "detail_nav_refused", "domain_pinned",
        "needs_deliberation", "nav_locked",
    )

    @classmethod
    def _looks_like_failure(cls, result: str) -> bool:
        if not result:
            return False
        r = result.lower()
        return any(m in r for m in cls._FAILURE_MARKERS)

    def record_attempt(
        self,
        tool: str,
        target: str,
        result: str,
        iteration: int,
    ) -> Optional[Constraint]:
        """Append one step to the *currently focused* constraint's ledger.

        Returns the constraint that received the attempt (None when
        there is no open focus, e.g. all done). Idempotency: the worker
        hook calls this once per iteration with the most-recent step;
        we de-dup against the constraint's last entry to handle replay.
        """
        focus = self.next_focus()
        if focus is None or not tool:
            return None
        result_short = (result or "")[:200]
        is_failure = self._looks_like_failure(result_short)
        entry = {
            "tool": tool,
            "target": (target or "")[:80],
            "result": result_short,
            "iter": iteration,
            "failed": is_failure,
        }
        # De-dup: don't append if this exact entry is already the tail
        # (the hook may run twice on the same step in some edge cases).
        if focus.attempts and focus.attempts[-1].get("iter") == iteration \
                and focus.attempts[-1].get("tool") == tool:
            return focus
        focus.attempts.append(entry)
        # Cap the per-focus log to 20 — keeps memory bounded on long runs.
        if len(focus.attempts) > 20:
            focus.attempts = focus.attempts[-20:]
        return focus

    def attempts_on(self, focus_id: int) -> int:
        for c in self.constraints:
            if c.id == focus_id:
                return len(c.attempts)
        return 0

    def failed_attempts_on(self, focus_id: int) -> int:
        for c in self.constraints:
            if c.id == focus_id:
                return sum(1 for a in c.attempts if a.get("failed"))
        return 0

    def render_focus_exhausted(
        self,
        focus_id: int,
        threshold: int,
    ) -> str:
        """Render the `[FOCUS_EXHAUSTED]` directive for a constraint that
        has accumulated ``threshold`` failed attempts. Empty string when
        the focus_id is invalid or the threshold has already fired for
        this constraint (idempotent — caller should still pass through;
        the empty result skips the block).
        """
        focus = next((c for c in self.constraints if c.id == focus_id), None)
        if focus is None:
            return ""
        if threshold in focus.nudges_emitted:
            return ""
        focus.nudges_emitted.add(threshold)
        label = focus.label
        # Recent failed attempt summary — show the brain WHAT it tried.
        recent = [a for a in focus.attempts if a.get("failed")][-3:]
        recent_lines = "\n".join(
            f"    - iter {a['iter']}: {a['tool']}({a['target'][:40]}) "
            f"→ {a['result'][:60]}"
            for a in recent
        )
        # Kind-specific tool-family recommendations.
        is_numeric_filter = focus.kind == "filter" and _looks_numeric_range(label)
        if is_numeric_filter:
            pivot = (
                "  This constraint reads like a numeric / range filter "
                "(price, year, rating, etc.). The most likely UI is a "
                "SLIDER, not a text input. Try ONE of these in order:\n"
                "    1) browser_list_slider_handles(session_id) — "
                "introspect the slider's min/max/current.\n"
                "    2) browser_set_slider_at(vision_index=V_n, percent=P) "
                "— if vision sees the slider, set it to a percent of "
                "the track.\n"
                "    3) browser_drag_slider_until(target_text='<value>', "
                "direction='right') — search for a slider whose readout "
                "matches a target value.\n"
                "    4) If the page has min/max number inputs, scroll "
                "to make them visible and use browser_type_at(V_n, '<n>') "
                "exactly ONCE per input — do NOT re-type the same value "
                "into different indices."
            )
        elif focus.kind == "filter":
            pivot = (
                "  Try a different approach to find the filter chip:\n"
                f"    1) browser_scroll_until(target_text={label[:40]!r}) "
                "— the chip may be off-screen.\n"
                "    2) Expand collapsed filter accordions you can see "
                "on the current screenshot.\n"
                "    3) browser_get_markdown to grep for the exact "
                "label text — sometimes filter labels differ from the "
                "constraint phrasing."
            )
        else:
            pivot = (
                "  Re-observe the page and pick a fresh strategy:\n"
                "    1) browser_screenshot — refresh the V_n bbox list.\n"
                "    2) browser_get_markdown — text-only view in case "
                "the data lives outside interactive elements."
            )
        # Wording escalation by threshold.
        if threshold >= 5:
            preamble = (
                f"[FOCUS_EXHAUSTED level=mandatory] Constraint #{focus.id} "
                f"{label!r} has now had {threshold} failed attempts. "
                f"MANDATORY: pivot to a different tool family OR mark "
                f"the constraint via "
                f"browser_brief_mark(constraint_id={focus.id}, "
                f"status='not_applicable', evidence='<why>'). "
                f"NO further attempts of the same kind will be productive."
            )
        else:
            preamble = (
                f"[FOCUS_EXHAUSTED level=warn] Constraint #{focus.id} "
                f"{label!r} has had {threshold} failed attempts. "
                f"You are reaching for the same tool family repeatedly "
                f"with different addresses; the page is telling you "
                f"that approach won't work. Pivot now."
            )
        escape = (
            f"  Escape hatch: if this constraint genuinely doesn't exist "
            f"on the page (e.g. the site has no such filter), call "
            f"browser_brief_mark(constraint_id={focus.id}, "
            f"status='not_applicable', evidence='<one-line reason>') and "
            f"continue with the remaining constraints. Do NOT attempt "
            f"to navigate to a constructed URL with hallucinated query "
            f"params — the navigate guard will refuse those, and even "
            f"if it didn't, sites' filter param names are almost never "
            f"what the brain guesses."
        )
        parts = [preamble]
        if recent_lines:
            parts.append("  Recent failed attempts on this focus:\n" + recent_lines)
        parts.append(pivot)
        parts.append(escape)
        return "\n".join(parts)

    # ---------------------------------------------------------- diagnostics

    def diagnostic_line(self) -> str:
        """One-line state snapshot for stdout. Cheap to log on every reconcile.

        Format: ``[brief] open=K/N focus='label'``.
        """
        f = self.next_focus()
        focus = f.label if f else "(all done)"
        return (
            f"[brief] v={self.version} open={self.open_count()}/"
            f"{len(self.constraints)} focus={focus!r}"
        )

    # ------------------------------------------- focus ↔ bbox recommendation

    def recommend_bboxes(
        self, vision_response: Any, top_k: int = 3
    ) -> list[dict]:
        """Rank vision bboxes by how well they match the current focus
        constraint's label and predicate hints.

        Returns up to ``top_k`` ranked dicts plus, when V1 isn't already
        in the top-k, a separate V1 entry tagged ``is_v1=True``. The
        ranking is descending on ``score`` (higher = better match).

        Empty list when there's no focus, no vision response, or no
        bboxes — caller should treat that as "skip the [FOCUS_BBOX]
        block this iteration."
        """
        focus = self.next_focus()
        if focus is None:
            return []
        bboxes = list(getattr(vision_response, "bboxes", []) or [])
        if not bboxes:
            return []
        active_hints = _str_list(
            (focus.predicate or {}).get("vision_active_label")
        )
        ranked: list[dict] = []
        for v_idx, bb in enumerate(bboxes, start=1):
            label = (getattr(bb, "label", "") or "").strip()
            if not label:
                continue
            score = _label_match_score(focus.label, label)
            # 1.5x boost when the label matches an explicit
            # vision_active_label hint from the predicate. This lets
            # orchestrator-supplied hints override raw token overlap.
            if active_hints and any(
                h.lower() in label.lower() for h in active_hints
            ):
                score = min(1.0, score * 1.5 + 0.1)
            ranked.append({
                "v_index": v_idx,
                "label": label,
                "score": round(score, 2),
                "is_v1": v_idx == 1,
            })
        ranked.sort(key=lambda r: r["score"], reverse=True)
        top = ranked[:top_k]
        # Always include V1's entry so the brain sees the highest-priority
        # bbox's match score even when it isn't in the top-k. Skip when V1
        # is already in top.
        if top and not any(r["is_v1"] for r in top):
            v1_entry = next((r for r in ranked if r["is_v1"]), None)
            if v1_entry is not None:
                top.append(v1_entry)
        return top

    def render_focus_bbox(self, vision_response: Any) -> str:
        """Render the [FOCUS_BBOX] block for the worker hook.

        Empty string when there's no focus or no useful bbox data —
        the hook then skips the block.
        """
        focus = self.next_focus()
        if focus is None:
            return ""
        recs = self.recommend_bboxes(vision_response, top_k=3)
        if not recs:
            return ""
        lines = [f"[FOCUS_BBOX] focus=#{focus.id} {focus.label!r}"]
        # Top recommendation (highest score among non-V1-only entries).
        top = recs[0]
        v1_marker = " ← also the highest-priority bbox" if top["is_v1"] else ""
        lines.append(
            f"  → recommended: V{top['v_index']} {top['label'][:60]!r} "
            f"(match {top['score']}){v1_marker}"
        )
        # Alternates (rank 2 + 3, excluding any duplicate V1 entry that
        # was appended for visibility).
        alts = [r for r in recs[1:] if r is not top and not (r["is_v1"] and len(recs) > 3)]
        if alts and len(alts) >= 1:
            alt_strs = [
                f"V{r['v_index']} {r['label'][:40]!r} ({r['score']})"
                for r in alts[:2]
                if not r["is_v1"]
            ]
            if alt_strs:
                lines.append(f"  alternates: {', '.join(alt_strs)}")
        # V1 explicit row when V1 isn't already the top recommendation.
        if not top["is_v1"]:
            v1_entry = next((r for r in recs if r["is_v1"]), None)
            if v1_entry is not None:
                relevance = (
                    "matches focus weakly — likely OK to skip"
                    if v1_entry["score"] < 0.3
                    else "moderate match"
                )
                lines.append(
                    f"  V1 in this view: {v1_entry['label'][:60]!r} "
                    f"(match {v1_entry['score']}) — {relevance}"
                )
        return "\n".join(lines)


# ----------------------------------------------------- fallback decomposer

# Heuristic constraint extractor — used when the orchestrator forgets to
# pass ``task_checklist`` for a multi-condition query. The output is
# strictly inferior to a hand-crafted checklist (predicates are all
# ``manual: true`` so nothing auto-flips), but the brain still gets
# structured visibility — `[CHECKLIST]` rows + `[FOCUS]` pin + periodic
# `[TASK_REMINDER]`. That alone is enough to keep multi-filter queries
# from losing their tail items.
#
# Triggers when the query looks like it has multiple conditions:
#   * 2+ occurrences of " and " or " & "
#   * commas joining 3+ noun phrases
#   * keywords: under, below, above, over, between, with, that pair, that have,
#     sort by, sorted by, including, only, except, no
#
# Decomposition pass:
#   1. Replace decomposition keywords with sentinel splits.
#   2. Split on "and"/","/"with"/"&" boundaries.
#   3. Trim each chunk; drop ones too short to be a real constraint.
#   4. Tag kind heuristically: "sort by" -> verification, "extract"/"list"/"return"
#      -> extraction, "click"/"submit"/"buy" -> action, otherwise filter.

import re as _re

# Tokens that strongly hint a phrase boundary between two constraints.
# Order matters — longer phrases first so re.split doesn't cut inside them.
_SPLIT_TOKENS = [
    r"\s+and\s+",
    r"\s+&\s+",
    r"\s+that\s+pair(?:s|ed)?\s+with\s+",
    r"\s+pair(?:s|ed)?\s+with\s+",
    r"\s+with\s+",
    r"\s*,\s+",
]
_SPLIT_RE = _re.compile("|".join(_SPLIT_TOKENS), _re.IGNORECASE)

# Phrases that *gate* fragment kind assignment — the matched fragment
# becomes the constraint regardless of length.
_KIND_HINTS: list[tuple[str, str]] = [
    (r"sort(?:ed)?\s+by", "verification"),
    (r"order(?:ed)?\s+by", "verification"),
    (r"return\b|extract\b|list\b|find\b|get\b|fetch\b|grab\b", "extraction"),
    (r"click\b|submit\b|buy\b|order\b|book\b|complete\b", "action"),
]


def _looks_multi_condition(query: str) -> bool:
    """Cheap predicate: does this query likely have >1 constraint?"""
    if not query or len(query) < 30:
        return False
    q = query.lower()
    score = 0
    if q.count(" and ") + q.count(" & ") >= 1:
        score += q.count(" and ") + q.count(" & ")
    score += q.count(",")
    for kw in (
        "under ", "below ", "above ", "over ", "between ", "with ",
        "that pair", "that have", "sort by", "sorted by", "including ",
        "only ", "except ", "less than", "more than", "at least",
    ):
        if kw in q:
            score += 1
    return score >= 2


def heuristic_decompose(query: str) -> list[dict]:
    """Best-effort split of a free-text query into constraint dicts.

    All predicates are ``{"manual": true}`` — the brain sees the
    structure but must call ``browser_brief_mark`` to flip items. This
    is a safety net, not a replacement for the orchestrator populating
    ``task_checklist`` properly.
    """
    q = (query or "").strip()
    if not q or not _looks_multi_condition(q):
        return []

    fragments = [f.strip(" .;:") for f in _SPLIT_RE.split(q) if f and f.strip()]
    # Discard tiny fragments that aren't real constraints. 3 chars filters
    # out connectors like "a", "an", "to", "the" while keeping short
    # real-world labels like "fish", "red", "USD", "$40".
    fragments = [f for f in fragments if len(f) >= 3]

    # Cap to avoid runaway lists for verbose queries.
    fragments = fragments[:8]

    items: list[dict] = []
    for frag in fragments:
        # Trim leading "to ", "the ", articles that hurt label quality.
        cleaned = _re.sub(
            r"^(?:to|the|a|an|please|could you|can you|i want|i'd like)\s+",
            "",
            frag,
            flags=_re.IGNORECASE,
        ).strip()
        if not cleaned:
            continue
        kind = "filter"
        for pattern, k in _KIND_HINTS:
            if _re.search(pattern, cleaned, _re.IGNORECASE):
                kind = k
                break
        # Cap label at 80 chars so [CHECKLIST] rows stay readable.
        label = cleaned[:80]
        items.append({
            "label": label,
            "kind": kind,
            "predicate": {"manual": True},
        })

    # If the heuristic only produced 1 item we don't gain much over
    # leaving the brain free-text — return empty so the legacy path is used.
    if len(items) < 2:
        return []
    return items


# ---------------------------------------------------------------- helpers

def _str_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v else []
    if isinstance(v, (list, tuple, set)):
        return [str(x) for x in v if x]
    return []


def _parse_query(url: str) -> dict[str, list[str]]:
    """Lowercased query-string parser. Returns {key_lower: [val_lower, ...]}."""
    try:
        from urllib.parse import urlparse, parse_qs
    except Exception:
        return {}
    try:
        q = urlparse(url).query or ""
    except Exception:
        return {}
    out: dict[str, list[str]] = {}
    for k, vs in parse_qs(q, keep_blank_values=True).items():
        kl = k.lower()
        out.setdefault(kl, []).extend(v.lower() for v in vs)
    return out


def _vision_labels(vr: Any) -> list[str]:
    bboxes = getattr(vr, "bboxes", None) or []
    out: list[str] = []
    for b in bboxes:
        lbl = getattr(b, "label", None)
        if isinstance(lbl, str) and lbl:
            out.append(lbl)
    return out


def _vision_active_labels(vr: Any) -> list[str]:
    """Labels of bboxes vision flagged as selected/active. Best-effort —
    falls back to empty when the field isn't present (vision schema varies
    across providers).
    """
    bboxes = getattr(vr, "bboxes", None) or []
    out: list[str] = []
    for b in bboxes:
        lbl = getattr(b, "label", None)
        if not isinstance(lbl, str) or not lbl:
            continue
        for attr in ("is_selected", "is_active", "selected", "active"):
            v = getattr(b, attr, None)
            if v is True:
                out.append(lbl)
                break
    return out


# Common stopwords to drop from token-overlap scoring. Kept tiny on
# purpose — agressively dropping words hurts label-vs-label matching
# (e.g. "Pairs with fish" vs "Fish pairing" — we want both "with" and
# "pairs"/"pairing" to contribute).
_LABEL_STOPWORDS = frozenset({
    "a", "an", "the", "of", "for", "to", "in", "on", "at", "by",
    "is", "be", "are", "was", "were",
})


def _tokenize_label(s: str) -> set[str]:
    """Lowercase + strip non-alphanumerics + drop tiny stopwords.

    Keeps hyphenated words split ("white-wine" → {"white", "wine"})
    so labels like "White Wine" and "white-wine" overlap fully.
    """
    if not s:
        return set()
    out: set[str] = set()
    cur: list[str] = []
    for ch in s.lower():
        if ch.isalnum() or ch == "$":
            cur.append(ch)
        else:
            if cur:
                tok = "".join(cur)
                if len(tok) >= 2 and tok not in _LABEL_STOPWORDS:
                    out.add(tok)
                cur = []
    if cur:
        tok = "".join(cur)
        if len(tok) >= 2 and tok not in _LABEL_STOPWORDS:
            out.add(tok)
    return out


def _label_match_score(focus_label: str, vbox_label: str) -> float:
    """Return a 0.0–1.0 similarity score between two short labels.

    Combines:
      * Token overlap (Jaccard) over the focus's tokens.
      * A substring boost when one label appears verbatim inside the
        other (e.g. focus="Oregon region", vbox="Oregon" → boost).

    Asymmetric: scoring uses focus tokens as the baseline. A vbox
    label that happens to contain ALL the focus tokens scores high
    even if it has extra words ("Oregon Pinot Noir" vs focus
    "Oregon region" → 0.5 from Jaccard + substring boost).
    """
    f_tokens = _tokenize_label(focus_label)
    v_tokens = _tokenize_label(vbox_label)
    if not f_tokens or not v_tokens:
        return 0.0
    overlap = f_tokens & v_tokens
    if not overlap:
        return 0.0
    # Jaccard-ish over the focus tokens (we care that the focus is
    # covered, not that the vbox label is concise).
    coverage = len(overlap) / len(f_tokens)
    # Substring boost: if focus_label or vbox_label is a substring of
    # the other (case-insensitive), bump by 0.2.
    fl = focus_label.lower().strip()
    vl = vbox_label.lower().strip()
    boost = 0.0
    if fl and vl and (fl in vl or vl in fl):
        boost = 0.2
    return min(1.0, coverage + boost)


_NUMERIC_RANGE_HINTS = (
    "price", "cost", "year", "rating", "score", "stars",
    "minute", "hour", "day", "month",
    "under", "below", "above", "over", "between", "from",
    "less than", "more than", "at least", "no more than",
    "≤", "≥", "<", ">", "$",
)


def _looks_numeric_range(label: str) -> bool:
    """Cheap predicate for ``[FOCUS_EXHAUSTED]`` routing — does the
    constraint label hint at a numeric / range filter? When True, the
    directive recommends slider tools first; otherwise it recommends
    scroll + click.
    """
    if not label:
        return False
    l = label.lower()
    if any(h in l for h in _NUMERIC_RANGE_HINTS):
        return True
    # Bare digits in the label ("400 calories", "3 stars") also hint.
    return any(ch.isdigit() for ch in l)


def _slug(s: str) -> str:
    s = (s or "").lower()
    out = []
    for ch in s:
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
        if len(out) >= 24:
            break
    return "".join(out).strip("-") or "item"
