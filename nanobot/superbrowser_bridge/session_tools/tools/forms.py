"""Form-related tools — select / option / form-plan / dialog and the
form-fill begin/status/commit triplet."""

from __future__ import annotations

from ._common import *  # noqa: F401,F403

@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        index=IntegerSchema(description="Element index of the select/dropdown"),
        value=StringSchema("Option value or visible text to select"),
        required=["session_id", "index", "value"],
    )
)
class BrowserSelectTool(Tool):
    name = "browser_select"
    description = "Select an option in a dropdown by value."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, index: int, value: str, **kw: Any) -> Any:
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/select",
            json={"index": index, "value": value},
            timeout=15.0,
        )
        r.raise_for_status()
        data = r.json()
        # Fetch updated elements after selection (may trigger form changes)
        if not data.get("elements"):
            elements = await _fetch_elements(session_id, self.s)
            if elements:
                data["elements"] = elements
        return self.s.build_text_only(data, f'Selected "{value}" in [{index}]')


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        label=StringSchema(
            "Human-readable label of the dropdown trigger (e.g. 'Brand', "
            "'Processor Brand', 'Year of Release'). Matched via accessible-name "
            "/ <label for=> / aria-labelledby / visible text."
        ),
        value=StringSchema(
            "Visible text or value of the option to pick (e.g. 'Dell', 'Intel', "
            "'2017'). Matching is exact-ci → startsWith → contains → fuzzy."
        ),
        fuzzy=BooleanSchema(
            description="Allow fuzzy match (Levenshtein ≥0.7). Default true.",
            default=True,
        ),
        timeout=IntegerSchema(
            description="Max ms to wait for listbox/options to render (default 4000).",
            nullable=True,
        ),
        extra_option_selectors=ArraySchema(
            description=(
                "Optional CSS selectors to add to the option-discovery list, "
                "for bespoke widgets that don't expose [role=option]."
            ),
            items=StringSchema(""),
            nullable=True,
        ),
        required=["session_id", "label", "value"],
    )
)
class BrowserSelectOptionTool(Tool):
    """Pick a dropdown option by *label* + *value*, hiding DOM-index churn.

    Use this for ANY dropdown/listbox/combobox — native <select>, ARIA
    combobox+listbox, Headless-UI Listbox, etc. You never pass an index or
    vision V-index, so re-renders between cascade steps don't matter.

    On ambiguity (no exact/fuzzy match) the tool returns the candidate list
    instead of guessing — retry with a corrected `value`. For ≥2 dependent
    dropdowns prefer `browser_form_plan` so progress is tracked structurally.
    """

    name = "browser_select_option"
    description = (
        "Pick a dropdown option by label+value. Works on native <select> "
        "AND custom listbox/combobox widgets. Returns {ok, picked_text, "
        "verified, candidates?} — on ambiguity, retry with one of the "
        "candidates instead of clicking blindly."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        label: str,
        value: str,
        fuzzy: bool = True,
        timeout: int | None = None,
        extra_option_selectors: list[str] | None = None,
        **kw: Any,
    ) -> str:
        print(f"\n>> browser_select_option(label={label!r}, value={value!r})")
        payload: dict[str, Any] = {"label": label, "value": value, "fuzzy": bool(fuzzy)}
        if timeout is not None:
            payload["timeout"] = int(timeout)
        if extra_option_selectors:
            payload["extra_option_selectors"] = list(extra_option_selectors)
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/select_option",
            json=payload,
            timeout=20.0,
        )
        try:
            r.raise_for_status()
        except Exception as e:
            self.s.record_step("browser_select_option", f"{label}={value}", f"HTTP error: {e}")
            return f"[select_option_http_error] {e}"
        data = r.json() or {}
        ok = bool(data.get("ok"))
        picked = data.get("picked_text") or value
        verified = bool(data.get("verified"))
        reason = data.get("reason")
        candidates = data.get("candidates") or []

        # Cursor strategy ledger — counts as a real cursor attempt for the
        # cursor-first lockout in browser_run_script.
        try:
            self.s.cursor_failure_strategies  # type: ignore[attr-defined]
        except Exception:
            pass
        else:
            if not ok and reason:
                self.s.cursor_failure_strategies.add(f"select_option:{reason}")

        if ok:
            note = f"Picked '{picked}' for '{label}'" + ("" if verified else " (verify pending)")
            print(f"   [select_option] ok -> {picked!r} (verified={verified})")
            self.s.log_activity(f"select_option({label})", picked[:40])
            self.s.record_step("browser_select_option", f"{label}={picked}", "ok")
            return self.s.build_text_only(data, note)

        # Ambiguity / failure path — surface candidates so the LLM corrects
        # the value rather than re-screenshotting and click-looping.
        cand_preview = (
            (" candidates=" + str([c[:30] for c in candidates[:6]]))
            if candidates else ""
        )
        print(f"   [select_option] FAIL reason={reason or '?'}{cand_preview}")

        msg_parts = [f"[select_option_failed] reason={reason or 'unknown'} label={label!r} value={value!r}"]
        if reason == "trigger_not_found":
            msg_parts.append(
                "The label was not found on this page. Two common causes:\n"
                "  (a) the cascading dropdown stage is over and the page "
                "transitioned to a results grid / model picker — in which "
                "case STOP using browser_form_plan / browser_select_option "
                "and instead browser_get_markdown to inspect the result list, "
                "then browser_click on the matching item.\n"
                "  (b) the label text in the page is different from what "
                "you passed — call browser_screenshot to read actual labels, "
                "then retry with the exact text."
            )
        if candidates:
            shown = ", ".join(repr(c) for c in candidates[:15])
            msg_parts.append(f"candidates: {shown}")
            msg_parts.append(
                "Retry browser_select_option with one of the candidates above. "
                "Do NOT fall back to raw clicking — DOM indices change after "
                "each pick; this tool re-anchors on the label."
            )
        elif reason and reason != "trigger_not_found":
            msg_parts.append(
                "No options were collected. The listbox may use a non-ARIA "
                "pattern — retry with a more specific label, or pass "
                "extra_option_selectors=[...] (e.g. ['li.option', '.dropdown-item'])."
            )
        self.s.log_activity(f"select_option({label}, FAIL)", (reason or "")[:60])
        self.s.record_step("browser_select_option", f"{label}={value}", f"FAIL:{reason or '?'}")
        return "\n".join(msg_parts)


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        intent=StringSchema(
            "Short description of what this filter form is for "
            "(e.g. 'Best Buy laptop trade-in valuation')."
        ),
        fields=ArraySchema(
            description=(
                "Ordered list of dropdowns to fill. Each entry: "
                "{label, value, kind?}. Order is the cascade order — later "
                "fields are filled only after earlier ones succeed. Use "
                "the *visible label text* (e.g. 'Brand', 'Processor Brand') "
                "and the *visible option text* (e.g. 'Dell', 'Intel')."
            ),
            items=ObjectSchema(
                label=StringSchema("Visible label text of the dropdown"),
                value=StringSchema("Visible option text to pick"),
                kind=StringSchema(
                    "Optional: 'select' | 'cascade_select' (default).",
                    nullable=True,
                ),
                required=["label", "value"],
            ),
        ),
        per_step_timeout=IntegerSchema(
            description="Per-field listbox-render timeout (ms, default 4000).",
            nullable=True,
        ),
        stop_on_failure=BooleanSchema(
            description=(
                "If true (default), stop and return on first failed field "
                "with the candidate list. If false, continue past failures "
                "to fill what's possible."
            ),
            default=True,
        ),
        required=["session_id", "intent", "fields"],
    )
)
class BrowserFormPlanTool(Tool):
    """Plan + execute a cascading filter form in one tool call.

    The LLM declares the *whole* form once — Brand=Dell, Processor=Intel,
    RAM=8GB, Year=2017, etc. — and the runtime fills each field by
    label-anchored selection (browser_select_option), settling between
    steps so the next dropdown's options can populate. Removes the
    "stale V-index, regress, retry" loop on multi-step filter forms.

    Returns a structured progress string. On per-field failure (no match,
    listbox didn't render) the tool surfaces the candidate list so the
    LLM can retry with a corrected value.
    """

    name = "browser_form_plan"
    description = (
        "Fill a cascading filter form (≥2 dependent dropdowns) in one "
        "call. Pass an ordered list of {label, value} pairs. The runtime "
        "label-anchors each pick (no DOM index, no V-index) and settles "
        "between steps. Strongly preferred over manual click-loops on "
        "trade-in / search-filter / quote forms."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        intent: str,
        fields: list[dict[str, Any]],
        per_step_timeout: int | None = None,
        stop_on_failure: bool = True,
        **kw: Any,
    ) -> str:
        if not isinstance(fields, list) or not fields:
            return "[form_plan_failed] `fields` must be a non-empty list."
        # Defensive: coerce to plain dicts; reject malformed entries early.
        clean: list[dict[str, Any]] = []
        for i, f in enumerate(fields):
            if not isinstance(f, dict):
                return f"[form_plan_failed] fields[{i}] is not a dict."
            label = (f.get("label") or "").strip()
            value = (f.get("value") or "").strip()
            if not label or not value:
                return (
                    f"[form_plan_failed] fields[{i}] missing label or value: "
                    f"{f!r}"
                )
            clean.append({"label": label, "value": value, "kind": (f.get("kind") or "cascade_select")})

        try:
            from superbrowser_bridge.form_session import FormFillSession, FieldStatus
        except ImportError as exc:
            return f"[form_plan_failed:import] {exc}"

        sess = FormFillSession.begin_cascade(
            intent=intent, fields=clean,
            started_at_turn=self.s._brain_turn_counter,
        )
        self.s.form_session = sess

        print(f"\n>> browser_form_plan({len(clean)} fields)")
        progress: list[str] = [
            f"[form_plan] intent={intent!r} planning {len(clean)} fields"
        ]
        failures: list[str] = []

        # Best-effort: close any open dropdown / modal before we start.
        # If the previous tool call left a listbox/menu open, the next
        # trigger lookup will land inside the overlay rather than on the
        # field's own combobox button.
        try:
            await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/keys",
                json={"keys": "Escape"},
                timeout=5.0,
            )
            await asyncio.sleep(0.2)
        except Exception:
            pass

        for entry in clean:
            label = entry["label"]
            value = entry["value"]
            payload = {"label": label, "value": value, "fuzzy": True}
            if per_step_timeout is not None:
                payload["timeout"] = int(per_step_timeout)
            print(f"   [form_plan] -> {label}={value!r}")
            r = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/select_option",
                json=payload,
                timeout=20.0,
            )
            try:
                r.raise_for_status()
            except Exception as e:
                failures.append(f"{label}={value} → HTTP error: {e}")
                if stop_on_failure:
                    break
                continue
            data = r.json() or {}
            ok = bool(data.get("ok"))
            picked = data.get("picked_text") or value
            reason = data.get("reason")
            candidates = data.get("candidates") or []

            if ok:
                sess.mark_picked(label, picked)
                progress.append(f"  [+] {label} = {picked!r}")
                print(f"   [form_plan]   ok -> picked {picked!r}")
                # Settle so dependent dropdown's options can populate
                # before the next iteration. 350ms covers most React
                # state-update + listbox-render flows; tune via per_step_timeout.
                await asyncio.sleep(0.35)
                # And close any lingering listbox before the next trigger lookup.
                try:
                    await _request_with_backoff(
                        "POST",
                        f"{SUPERBROWSER_URL}/session/{session_id}/keys",
                        json={"keys": "Escape"},
                        timeout=5.0,
                    )
                    await asyncio.sleep(0.15)
                except Exception:
                    pass
            else:
                cand_str = ""
                if candidates:
                    cand_str = (
                        " candidates=" + ", ".join(repr(c) for c in candidates[:10])
                    )
                msg = f"{label}={value} → {reason or 'no_match'}{cand_str}"
                failures.append(msg)
                progress.append(f"  [!] {msg}")
                print(f"   [form_plan]   FAIL -> {reason or '?'} {('cands=' + str(candidates[:6])) if candidates else ''}")
                if stop_on_failure:
                    break

        # Final progress summary
        progress.append("")
        progress.append(sess.cascade_progress())
        if failures and stop_on_failure:
            progress.append(
                "Stopped on first failure. Retry browser_form_plan with "
                "corrected values for the remaining fields, OR retry just "
                "the failed field with browser_select_option using one of "
                "the listed candidates."
            )
        elif failures:
            progress.append(
                f"Continued past {len(failures)} failure(s). Use "
                "browser_select_option to retry each."
            )

        self.s.log_activity(
            f"form_plan({intent[:30]})",
            f"verified {sum(1 for fs in sess.fields.values() if fs.status == FieldStatus.VERIFIED)}/{len(sess.fields)}",
        )
        return "\n".join(progress)


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        accept=BooleanSchema(description="Accept (true) or dismiss (false)"),
        text=StringSchema("Text for prompt dialogs", nullable=True),
        required=["session_id", "accept"],
    )
)
class BrowserDialogTool(Tool):
    name = "browser_dialog"
    description = "Accept or dismiss a pending JavaScript dialog."

    async def execute(self, session_id: str, accept: bool, text: str | None = None, **kw: Any) -> str:
        payload: dict[str, Any] = {"accept": accept}
        if text:
            payload["text"] = text
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/dialog",
            json=payload,
            timeout=10.0,
        )
        r.raise_for_status()
        return f"Dialog {'accepted' if accept else 'dismissed'}"


# ─── Phase 2: form-fill orchestration tools ──────────────────────────────


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        intent=StringSchema(
            "What this form does — e.g. 'apartment search filters', "
            "'flight booking', 'signup'. Used by the worker hook to "
            "phrase the per-turn checklist."
        ),
        fields=ArraySchema(
            description=(
                "Ordered list of fields to fill. Each entry is an object "
                "with `label` (human-readable name to match against vision "
                "bboxes), `value` (target text to type), and optional "
                "`autocomplete` (true if this field opens a suggestions "
                "overlay that must be picked from)."
            ),
            items=ObjectSchema(
                label=StringSchema("Field name shown to the user"),
                value=StringSchema("Value to type"),
                autocomplete=BooleanSchema(
                    description="Whether this field opens an autocomplete dropdown",
                    nullable=True,
                ),
                required=["label", "value"],
            ),
        ),
        submit_label=StringSchema(
            "Optional label of the submit button (e.g. 'Search'). "
            "If provided, browser_form_commit will look for it in vision.",
            nullable=True,
        ),
        required=["session_id", "intent", "fields"],
    )
)
class BrowserFormBeginTool(Tool):
    """Phase 2.1: open a form-fill session.

    Tracks pending/filled/verified state for each declared field. While
    a session is active the worker hook injects a remaining-fields
    checklist into every tool result, and browser_form_commit refuses
    to dispatch the submit click until every field's typed value is
    visible on the page.

    Use this on dense filter/booking/signup forms where the brain
    routinely loses track of fields below an autocomplete dropdown.
    """

    name = "browser_form_begin"
    description = (
        "Open a tracked form-fill session. After calling, fill each "
        "field with browser_type_at — the session tracks progress and "
        "warns when a field is missed. Conclude with browser_form_commit "
        "to verify all values stuck before submitting."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        intent: str,
        fields: list[dict[str, Any]],
        submit_label: str | None = None,
        **kw: Any,
    ) -> str:
        if os.environ.get("FORM_SESSION_ENABLED", "1") in ("0", "false", "no"):
            return (
                "[form_begin_disabled] FORM_SESSION_ENABLED=0 — fall "
                "back to ad-hoc filling. Track remaining fields yourself."
            )
        if not isinstance(fields, list) or not fields:
            return "[form_begin_failed] `fields` must be a non-empty list."
        try:
            from superbrowser_bridge.form_session import FormFillSession
        except ImportError as exc:
            return f"[form_begin_failed:import] {exc}"
        sess = FormFillSession.begin(
            intent=intent,
            fields=fields,
            started_at_turn=self.s._brain_turn_counter,
            submit_label=submit_label,
        )
        self.s.form_session = sess
        labels = ", ".join(fs.label for fs in sess.fields.values())
        return (
            f"[form_begin] intent={intent!r} fields=[{labels}]\n"
            f"Now: call browser_screenshot once to anchor every field's "
            f"bbox, then for each field call browser_type_at(vision_index="
            f"V_n, text=...). After typing into a field that opens "
            f"autocomplete, click the matching suggestion (or press "
            f"Escape) BEFORE moving on. When every field is filled, "
            f"call browser_form_commit to verify."
        )


@tool_parameters(
    tool_parameters_schema(session_id=StringSchema("Session ID"), required=["session_id"])
)
class BrowserFormStatusTool(Tool):
    """Phase 2.1: report the current form-fill checklist."""

    name = "browser_form_status"
    description = (
        "Report status of the active form-fill session: which fields are "
        "still pending, which are filled, which need autocomplete picks. "
        "Cheap / no screenshot. Returns [no_form_session] if none active."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, **kw: Any) -> str:
        sess = self.s.form_session
        if sess is None:
            return (
                "[no_form_session] No form-fill session active. Call "
                "browser_form_begin to start tracking a multi-field form."
            )
        return sess.remaining_checklist(max_lines=20)


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        force=BooleanSchema(
            description=(
                "Skip the verify-all-fields check and close the session anyway. "
                "Use only when you intentionally want to submit a partial form."
            ),
            nullable=True,
        ),
        required=["session_id"],
    )
)
class BrowserFormCommitTool(Tool):
    """Phase 2.1: verify and close a form-fill session.

    Forces a fresh screenshot, then for each tracked field checks that
    the typed value appears in the page's relevant_text. Returns a
    structured pass/fail report — the brain decides whether to refill
    mismatched fields or submit.
    """

    name = "browser_form_commit"
    description = (
        "Verify every tracked field's typed value appears on screen, "
        "then close the form-fill session. Returns the per-field "
        "verdict so the brain can refill any mismatches before "
        "clicking submit."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        force: bool = False,
        **kw: Any,
    ) -> str:
        sess = self.s.form_session
        if sess is None:
            return (
                "[form_commit_failed:no_session] No form session is "
                "active. Call browser_form_begin first."
            )
        # Refresh vision so we verify against the latest screenshot.
        resp = self.s._last_vision_response
        text_hay = ""
        if resp is not None:
            text_hay = (getattr(resp, "relevant_text", "") or "").lower()
        for fs in sess.fields.values():
            if fs.status == FieldStatus.SKIPPED:
                continue
            target_lower = (fs.target_value or "").strip().lower()
            if not target_lower:
                continue
            if target_lower in text_hay:
                sess.mark_verified(fs.label, fs.target_value)
            else:
                # Don't overwrite VERIFIED set during typing flow.
                if fs.status not in (FieldStatus.VERIFIED,):
                    fs.status = FieldStatus.MISMATCH
        summary = sess.commit_summary()
        if not force and not sess.is_complete():
            return (
                f"[form_commit_incomplete] {summary}\n"
                f"{sess.remaining_checklist(max_lines=10)}\n"
                f"Refill the mismatched / pending fields, then call "
                f"browser_form_commit again. Use force=true ONLY if you "
                f"intentionally want to submit a partial form."
            )
        # Success — clear the session so the next form starts clean.
        result = (
            f"[form_commit_ok] {summary}\n"
            f"You may now click the submit button "
            + (f"('{sess.submit_label}') " if sess.submit_label else "")
            + "via browser_click_at(vision_index=V_n)."
        )
        self.s.form_session = None
        return result


# Re-export FieldStatus into module scope so the commit tool can refer to
# it without importing locally on every call.
try:
    from superbrowser_bridge.form_session import FieldStatus  # noqa: F401
except ImportError:
    class FieldStatus:  # type: ignore[no-redef]
        VERIFIED = "verified"
        SKIPPED = "skipped"
        MISMATCH = "mismatch"


