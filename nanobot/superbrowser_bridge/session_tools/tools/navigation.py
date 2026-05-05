"""Navigation tools — navigate / scroll / scroll-until / wait-for."""

from __future__ import annotations

from ._common import *  # noqa: F401,F403


def _record_nav_refusal(state: Any, url: str, reason: str) -> None:
    """Mark the current focus as nav-locked + log the attempt on the brief.

    Called from every refusal site inside ``BrowserNavigateTool.execute``
    so the next navigate against the same focus is short-circuited by
    the per-focus lockout (see top of execute()) and the brief's
    per-focus attempt ledger sees the refusal toward the FOCUS_EXHAUSTED
    threshold. Defensive — silently no-ops when there's no brief.
    """
    brief = getattr(state, "task_brief", None)
    if brief is None:
        return
    focus = brief.next_focus()
    if focus is None:
        return
    state.last_navigate_refusal_focus_id = focus.id
    try:
        brief.record_attempt(
            tool="browser_navigate",
            target=url,
            result=reason,
            iteration=getattr(state, "_brain_turn_counter", 0),
        )
    except Exception:
        pass

@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID from browser_open"),
        url=StringSchema("URL to navigate to"),
        intent=StringSchema(
            "Optional hint for the vision agent (e.g. 'verify navigation "
            "succeeded', 'find sign-up button'). Only used when "
            "VISION_ENABLED=1.",
            nullable=True,
        ),
        force_detail=BooleanSchema(
            description=(
                "Override the [DETAIL_NAV_REFUSED] guard that refuses "
                "navigation to product / article detail pages while filter "
                "constraints are still open. Pass true ONLY when you have "
                "deliberately decided to open a detail page after marking "
                "the remaining filter constraints as done / "
                "not_applicable / failed. Default false."
            ),
            default=False,
        ),
        required=["session_id", "url"],
    )
)
class BrowserNavigateTool(Tool):
    name = "browser_navigate"
    description = "Navigate to a URL in an open browser session."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, url: str, intent: str | None = None, **kw: Any) -> Any:
        print(f"\n>> browser_navigate({url})")
        gate = await _feedback_gate("browser_navigate")
        if gate:
            return gate
        sync_block = await self.s.ensure_vision_synced(reason="browser_navigate")
        if sync_block:
            return sync_block

        # --- Known-bad URL lockout ---------------------------------------
        # Fire BEFORE any other guard. If this exact URL (or its
        # normalized variant) returned 4xx/5xx earlier in this session,
        # refuse without a roundtrip. Eliminates the "404 → re-navigate
        # to same URL" loop observed in the wineaccess.com trace.
        try:
            _norm_check = self.s._normalize_url(url)
        except Exception:
            _norm_check = url
        _prior_failure = self.s.failed_navigation_urls.get(_norm_check)
        if _prior_failure:
            self.s.record_step(
                "browser_navigate", url,
                f"BLOCKED: url_known_bad (prior HTTP {_prior_failure['status']})",
            )
            return (
                f"[URL_KNOWN_BAD] Refused navigate to {url} — this URL "
                f"returned HTTP {_prior_failure['status']} earlier in "
                f"this session. Re-navigating will hit the same error. "
                f"The URL path is likely guessed (e.g. "
                f"/store/regions/<x>/ when the site uses "
                f"/store/?region=<x>). Apply the filter on the current "
                f"page via browser_click_at(V_n) on the actual filter "
                f"chip, OR call browser_brief_mark(constraint_id=<n>, "
                f"status='not_applicable', evidence='<reason>') if the "
                f"filter genuinely doesn't exist on the site."
            )

        # --- Per-focus navigate lockout ----------------------------------
        # If a previous navigate on the same focus was refused
        # (filter_hack / detail_nav / deliberation), refuse this one too
        # — but with a louder message that points the brain at brief_mark
        # or a screenshot+click. The lockout clears when the brain calls
        # browser_screenshot / get_markdown / brief_mark (handled in
        # worker_hook.py).
        _brief = getattr(self.s, "task_brief", None)
        _lock_focus = getattr(self.s, "last_navigate_refusal_focus_id", None)
        if _brief is not None and _lock_focus is not None:
            _cur_focus = _brief.next_focus()
            _cur_id = _cur_focus.id if _cur_focus else None
            if _cur_id == _lock_focus:
                self.s.record_step(
                    "browser_navigate", url,
                    f"BLOCKED: nav_locked_for_focus #{_lock_focus}",
                )
                try:
                    _brief.record_attempt(
                        tool="browser_navigate",
                        target=url,
                        result="NAV_LOCKED_FOR_FOCUS",
                        iteration=getattr(self.s, "_brain_turn_counter", 0),
                    )
                except Exception:
                    pass
                _focus_label = _cur_focus.label if _cur_focus else "?"
                return (
                    f"[NAV_LOCKED_FOR_FOCUS] Refused navigate to {url}. "
                    f"A previous navigate on the same focus constraint "
                    f"(#{_lock_focus} {_focus_label!r}) was already "
                    f"refused. Trying another URL variant for the same "
                    f"focus is the same hallucination loop. Pick ONE:\n"
                    f"  1) browser_screenshot to re-observe — clears "
                    f"this lockout.\n"
                    f"  2) browser_brief_mark(constraint_id={_lock_focus}, "
                    f"status='not_applicable', evidence='<reason>') if "
                    f"this constraint genuinely can't be satisfied via "
                    f"the page UI — clears the lockout and advances the "
                    f"focus.\n"
                    f"  3) browser_get_markdown to read the page content "
                    f"in case the filter UI uses non-obvious labels — "
                    f"clears the lockout."
                )

        # --- Domain-pinning guard -----------------------------------------
        # When pinned_domain is set, only allow navigation to the target
        # domain (+ subdomains) and a small safe-list. Prevents the worker
        # LLM from visiting alternative sites when the target blocks it.
        if self.s.pinned_domain:
            from urllib.parse import urlparse as _urlparse
            # Safe-list = OAuth + CDN only. google.com stays on the list
            # (OAuth flow needs `accounts.google.com`, `accounts.youtube.com`,
            # etc.) but SEARCH paths on it are blocked below — observed
            # 2026-04-19: LLM would pivot to google.com/search whenever
            # the real target was slow, turning every task into a Google
            # scrape that 429'd and poisoned the session.
            _SAFE_DOMAINS = ("google.com", "googleapis.com", "gstatic.com", "google.co")
            try:
                _parsed = _urlparse(url)
                _target_host = (_parsed.hostname or "").lower().replace("www.", "")
                _target_path = _parsed.path or ""
                _target_query = _parsed.query or ""
            except Exception:
                _target_host = ""
                _target_path = ""
                _target_query = ""
            _pinned = self.s.pinned_domain
            _is_pinned = _target_host == _pinned or _target_host.endswith("." + _pinned)
            _is_safe = any(
                _target_host == sd or _target_host.endswith("." + sd)
                for sd in _SAFE_DOMAINS
            )
            # Block Google Search as an escape hatch — `google.com/search`,
            # `google.com/?q=`, `google.com/images`, etc. The LLM must stay
            # on the pinned domain even when it's frustrated.
            _is_google = _target_host == "google.com" or _target_host.endswith(".google.com") or _target_host.endswith(".google.co")
            _looks_like_search = _is_google and (
                _target_path.startswith("/search")
                or _target_path.startswith("/images")
                or _target_path.startswith("/maps")
                or "q=" in _target_query
            )
            if _target_host and (not (_is_pinned or _is_safe) or _looks_like_search):
                reason = "search_escape" if _looks_like_search else "outside_pin"
                self.s.record_step("browser_navigate", url, f"BLOCKED: {reason} (pinned={_pinned})")
                print(f"   [DOMAIN_PINNED] blocked navigation to {_target_host}{_target_path} ({reason}, pinned={_pinned})")
                _record_nav_refusal(self.s, url, f"DOMAIN_PINNED:{reason}")
                return (
                    f"[DOMAIN_PINNED] Navigation to {url} is BLOCKED. "
                    f"You MUST stay on {_pinned} (and its subdomains). "
                    f"Do NOT pivot to Google Search or other sites when the "
                    f"target is slow or annoying — fix the problem on "
                    f"{_pinned} itself. If {_pinned} is hard-blocked, call "
                    f"browser_escalate (to Tier 3) or browser_solve_captcha "
                    f"or browser_ask_user, or report failure via "
                    f"done(success=False)."
                )

        # --- Filter-hack URL refusal -------------------------------------
        # Pattern observed in long traces: brain decides "I can apply
        # all filters in one navigate" and constructs a URL like
        #     /store/search/?category__in=white-wine&regions=oregon&
        #         food_pairings=fish,dessert&price=0,40
        # Most retail sites' real filter param names are NOT what the
        # brain guessed (e.g. `region_slug` vs `regions`, `min_price`
        # vs `price__gte`), so the URL either 404s, returns empty
        # results, or — as in the wineaccess.com trace — redirects
        # forever. Refuse when the URL has too many filter-style query
        # params, or when ANY param is multi-value comma-joined.
        # Cross-domain navigations (e.g. real OAuth redirect URLs with
        # legitimate ?state=&code=&scope=) are exempt — the heuristic
        # only fires on same-domain (or same as pinned) navigations.
        try:
            from urllib.parse import urlparse as _urlparse_fh, parse_qsl as _parse_qsl
            _parsed_fh = _urlparse_fh(url)
            _params_fh = _parse_qsl(_parsed_fh.query or "", keep_blank_values=False)
            _target_host_fh = (_parsed_fh.hostname or "").lower().replace("www.", "")
        except Exception:
            _params_fh = []
            _target_host_fh = ""
        # Only police same-domain or pinned-domain navigations. OAuth
        # redirects to other origins legitimately use opaque keys.
        _same_domain = bool(self.s.pinned_domain) and (
            _target_host_fh == self.s.pinned_domain
            or _target_host_fh.endswith("." + self.s.pinned_domain)
        )
        if _params_fh and (_same_domain or not self.s.pinned_domain):
            # Multi-value comma-joined filter — almost always wrong.
            _multi_value = any(
                "," in v for (k, v) in _params_fh
                if k.lower() not in ("scope", "code", "state", "redirect_uri", "state_token")
            )
            # Filter-style key names. Conservative list — keys like
            # `q`, `page`, `sort` aren't filters (they're search/pagination).
            _filter_key_patterns = (
                "category", "region", "country", "type", "kind",
                "color", "size", "brand", "price", "min_", "max_",
                "from_", "to_", "before_", "after_", "in_",
                "filter", "tag", "feature", "amenity", "pairing",
                "rating", "score", "year", "date_",
            )
            _filter_keys_seen = sum(
                1 for (k, _v) in _params_fh
                if any(p in k.lower() for p in _filter_key_patterns)
            )
            # Path-style filter hallucination — same intent as the
            # query-string version but the brain encoded the filter into
            # the path: /store/regions/oregon/, /products/colors/red/,
            # etc. Almost always wrong: real sites use query strings or
            # specific slug-only paths, not freely-composable segments.
            # Trigger when ≥1 path segment matches our filter dictionary
            # AND the brain has marked zero filter constraints done so
            # far (= the brain is guessing the URL, not following a real
            # link).
            _path_lower = (_parsed_fh.path or "").lower()
            _path_filter_segments = (
                "/regions/", "/region/",
                "/categories/", "/category/",
                "/collections/",
                "/colors/", "/color/",
                "/types/", "/type/",
                "/brands/", "/brand/",
                "/tags/", "/tag/",
                "/filters/", "/filter/",
            )
            _path_segments_seen = sum(
                1 for seg in _path_filter_segments if seg in _path_lower
            )
            _filter_brief_progress = 0
            _brief_check = getattr(self.s, "task_brief", None)
            if _brief_check is not None:
                try:
                    _filter_brief_progress = sum(
                        1 for c in _brief_check.constraints
                        if c.kind == "filter" and c.status == "done"
                    )
                except Exception:
                    _filter_brief_progress = 0
            _path_hack = (
                _path_segments_seen >= 1
                and _filter_brief_progress == 0
                and bool(_brief_check)
            )

            if _multi_value or _filter_keys_seen >= 2 or _path_hack:
                self.s.record_step(
                    "browser_navigate", url,
                    f"BLOCKED: filter_hack "
                    f"(multi_value={_multi_value} filter_keys={_filter_keys_seen} path_segments={_path_segments_seen})",
                )
                _record_nav_refusal(self.s, url, "navigate_filter_hack_refused")
                if _path_hack and not (_multi_value or _filter_keys_seen >= 2):
                    hint = (
                        f"{_path_segments_seen} filter-style path segment(s) "
                        f"(e.g. /regions/, /categories/) with zero filter "
                        f"constraints marked done — looks like a guessed URL "
                        f"path, not one you reached by clicking"
                    )
                else:
                    hint = (
                        "multi-value comma param" if _multi_value
                        else f"{_filter_keys_seen} filter-style query params"
                    )
                return (
                    f"[navigate_filter_hack_refused] Refused {url} — "
                    f"contains {hint}. Sites' filter param names are "
                    f"almost never what you guessed (`region_slug` vs "
                    f"`regions`, `price__gte` vs `price=0,40`, etc.) — "
                    f"the URL will 404, return empty results, or "
                    f"redirect-loop. Apply filters by clicking the "
                    f"actual filter chips on the page (browser_click_at "
                    f"on V_n bboxes). Use browser_screenshot to find "
                    f"them; expand collapsed filter accordions if "
                    f"needed. Filter clicks ALSO trigger reconcile_from_url "
                    f"on the brief checklist — your URL hack would have "
                    f"bypassed that and left [CHECKLIST] in a confused "
                    f"state."
                )

        # --- Same-path query-string mutation guard -----------------------
        # Brain-constructed URL pattern: navigate to the SAME host+path
        # the page already loaded but with a different query string —
        # `?ordering=-expert_rating`, `?sort=price`, `?page=3`. The site's
        # actual sort/filter UI almost never accepts the param name the
        # brain guessed, and the trace ends in 404 / ERR_TOO_MANY_REDIRECTS
        # / blank results. The fix is the on-page control (sort dropdown,
        # filter chip, pagination button) reached via browser_click_at(V_n).
        try:
            from urllib.parse import urlparse as _urlparse_qg
            _new = _urlparse_qg(url)
            _cur = _urlparse_qg(self.s.current_url or "")
            _same_target = (
                bool(_new.netloc) and bool(_cur.netloc)
                and _new.netloc.lower() == _cur.netloc.lower()
                and (_new.path or "/") == (_cur.path or "/")
            )
            if _same_target and _new.query and _new.query != _cur.query:
                self.s.record_step(
                    "browser_navigate", url,
                    "BLOCKED: same_path_query_mutation",
                )
                _record_nav_refusal(self.s, url, "navigate_param_mutation_refused")
                return (
                    f"[navigate_param_mutation_refused] Refused {url} — "
                    f"you're navigating to the SAME path you're already on "
                    f"({_cur.path or '/'}) with a different query string "
                    f"({_new.query!r} vs current {_cur.query!r}). "
                    f"That's almost always a guessed param: sites use "
                    f"different param names than the obvious ones (e.g. "
                    f"`?ordering=-expert_rating` when the site really uses "
                    f"`?sort=critic_score_desc`), and the wrong name 404s, "
                    f"redirects, or returns the unsorted list with the "
                    f"param silently dropped. Use the on-page control "
                    f"instead: browser_screenshot to find the sort "
                    f"dropdown / filter chip / pagination button, then "
                    f"browser_click_at(vision_index=V_n) on it. The "
                    f"resulting URL change is authoritative because the "
                    f"site itself constructed it."
                )
        except Exception:
            pass

        # --- Pre-nav deliberation gate ----------------------------------
        # The brain's failure mode in long traces: click→click→navigate
        # without ever pausing to see what changed. We require recent
        # context-gathering — a screenshot, markdown read, or brief
        # mark within the last 3 brain turns — before any navigate
        # while a task_brief is active. Skipped when no brief is set
        # (legacy single-condition behaviour).
        # Also skipped when the brain explicitly passes intent= (the
        # explicit intent argument is treated as the brain's commitment
        # to the navigation reason).
        if (
            getattr(self.s, "task_brief", None) is not None
            and not intent
        ):
            turns_since_delib = (
                self.s._brain_turn_counter - self.s.last_deliberation_turn
            )
            # ≥ 3 turns since last screenshot/markdown/mark = rushing.
            if (
                self.s.last_deliberation_turn > 0  # avoid first-turn false trip
                and turns_since_delib >= 3
            ):
                self.s.record_step(
                    "browser_navigate", url,
                    f"BLOCKED: deliberation_gate "
                    f"({turns_since_delib} turns since last look)",
                )
                _record_nav_refusal(self.s, url, "NAV_NEEDS_DELIBERATION")
                return (
                    f"[NAV_NEEDS_DELIBERATION] Refused navigate to {url}. "
                    f"You haven't taken a browser_screenshot, called "
                    f"browser_get_markdown, or marked a brief constraint "
                    f"in {turns_since_delib} brain turns — that's the "
                    f"'click-click-navigate-blindly' pattern. Pick ONE:\n"
                    f"  1) browser_screenshot to see the current page "
                    f"and its V_n bbox list — your next click is more "
                    f"likely to advance the [FOCUS] constraint.\n"
                    f"  2) browser_get_markdown if you only need text.\n"
                    f"  3) browser_brief_mark to flip a constraint you "
                    f"have evidence for, then retry the nav.\n"
                    f"  4) Re-call browser_navigate WITH an intent= arg "
                    f"explaining which constraint this nav advances "
                    f"and why now is the right moment."
                )

        # --- Detail-page nav refusal during open-filter constraints ----
        # When the orchestrator decomposed the query into a checklist
        # and the brain still has open `filter` constraints, refuse
        # navigations to product/article/detail-page URLs. The trace
        # pattern this catches: brain applies one filter, types a price,
        # then bails out by clicking through to a single product page —
        # losing every other unfilled constraint. Real "open one item
        # for extraction" flows happen AFTER the listing is filtered;
        # by definition the filter checklist is done by then. Override
        # via explicit force_detail=true for the rare legitimate case.
        force_detail = bool(kw.get("force_detail"))
        brief = getattr(self.s, "task_brief", None)
        if brief is not None and not force_detail:
            open_filters = [
                c for c in brief.constraints
                if c.is_open() and c.kind == "filter"
            ]
            if open_filters:
                from urllib.parse import urlparse as _urlparse2
                try:
                    _path = (_urlparse2(url).path or "").lower()
                except Exception:
                    _path = ""
                # Common detail-page path roots, ordered by frequency.
                _detail_roots = (
                    "/catalog/", "/product/", "/products/",
                    "/p/", "/item/", "/items/",
                    "/article/", "/articles/", "/post/", "/posts/",
                    "/listing/", "/listings/",
                    "/recipe/", "/recipes/",
                )
                _looks_detail = any(
                    _path.startswith(r) and len(_path.rstrip("/").split("/")) >= 3
                    for r in _detail_roots
                )
                if _looks_detail:
                    self.s.record_step(
                        "browser_navigate", url,
                        f"BLOCKED: detail-page nav with "
                        f"{len(open_filters)} open filter constraints",
                    )
                    _record_nav_refusal(self.s, url, "DETAIL_NAV_REFUSED")
                    open_labels = ", ".join(
                        f"#{c.id} {c.label[:40]}" for c in open_filters[:5]
                    )
                    return (
                        f"[DETAIL_NAV_REFUSED] Refused navigation to {url} "
                        f"because {len(open_filters)} filter constraints "
                        f"are still [open]: {open_labels}. Apply the "
                        f"remaining filters on the listing page FIRST — "
                        f"opening a single item now drops the other "
                        f"constraints from the checklist. If you "
                        f"genuinely need a detail page (e.g. for "
                        f"extraction after all filters are done), mark "
                        f"each filter constraint via "
                        f"browser_brief_mark(constraint_id, "
                        f"status='not_applicable', evidence=<reason>) "
                        f"first, OR pass force_detail=true to override."
                    )

        self.s.actions_since_screenshot += 1
        self.s.consecutive_click_calls = 0
        # Action-delta capture — navigate is the only mutating tool that
        # commonly changes URL, so the [ACTION_DELTA] block will tell
        # the brain "page navigated → re-screenshot" loud and clear.
        self.s._brain_turn_counter += 1
        self.s.capture_action_snapshot(target_index=None)
        await self.s.inter_action_pause()

        # CF-interstitial nav guard: if the last navigate to THIS URL was
        # Cloudflare-blocked and nothing has been done to resolve it, a
        # fresh page.goto will just re-trigger the same interstitial and
        # burn budget. Tell the agent to call browser_solve_captcha first.
        _norm_target = self.s._normalize_url(url)
        if (
            self.s.last_nav_cf_blocked_url
            and _norm_target == self.s.last_nav_cf_blocked_url
            and not self.s.nav_solve_called_since_block
        ):
            self.s.record_step(
                "browser_navigate", url,
                "BLOCKED: last navigate to this URL hit CF interstitial; "
                "call browser_solve_captcha first",
            )
            return (
                f"[CF_INTERSTITIAL_PENDING] The last navigate to {url} "
                f"landed on a Cloudflare Managed Challenge "
                f"('Performing security verification'). Re-navigating "
                f"before solving will just re-trigger the same challenge. "
                f"Call browser_solve_captcha(session_id='{session_id}', "
                f"method='auto') to wait for the interstitial to auto-"
                f"clear, THEN retry this navigate. If the solver also "
                f"fails, call browser_ask_user to hand off to a human."
            )

        # Detect regression before navigating
        regression = self.s.is_regression(url)
        if regression:
            self.s.regression_count += 1

        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/navigate",
            json={"url": url},
            timeout=30.0,
        )
        r.raise_for_status()
        data = r.json()

        actual_url = data.get("url", url)
        self.s.log_activity(f"navigate({url})", f"title={data.get('title', '?')}")
        self.s.record_url(actual_url)
        # Drop the prior epoch — it belongs to the old page. The next
        # click will fall back to `_last_vision_response` (blank or
        # post-nav prefetch) via `vision_for_target_resolution`, and
        # the very next `browser_screenshot` re-freezes the epoch.
        self.s._vision_epoch_response = None

        # Set/clear the CF nav-guard based on what came back. `block_class`
        # is populated by interactive_session.py after the challenge wait
        # loop fails to clear. A navigate to any OTHER URL clears the
        # guard regardless — progress elsewhere means the stuck state is
        # gone.
        _block_class = (
            str(data.get("block_class") or data.get("blockClass") or "")
            .lower()
        )
        if _block_class == "cloudflare":
            self.s.last_nav_cf_blocked_url = self.s._normalize_url(actual_url)
            self.s.nav_solve_called_since_block = False
        elif _norm_target != self.s.last_nav_cf_blocked_url:
            # Navigated to a different URL that isn't CF-blocked — guard off.
            self.s.last_nav_cf_blocked_url = ""
            self.s.nav_solve_called_since_block = False

        caption = _format_state(data, self.s)

        # Network-layer block detection — same logic as browser_open. Exit
        # early so the worker doesn't try to interact with a 403/429 shell.
        # CF interstitial gets the solve-captcha routing caption and the
        # nav-guard block set above.
        status_code = data.get("statusCode")
        if isinstance(status_code, int):
            self.s.last_network_status = status_code
            if status_code >= 400 and status_code != 404:
                self.s.network_blocked = True
                caption += _build_network_block_message(
                    status_code, actual_url, block_class=_block_class,
                )
                # Cloudflare interstitials shouldn't poison the URL —
                # they often clear after solve_captcha. Other 4xx/5xx
                # genuinely won't change without a different URL, so
                # add to the known-bad ledger.
                if _block_class != "cloudflare":
                    self.s.record_failed_navigation(actual_url, status_code)
                if _block_class == "cloudflare":
                    self.s.record_step(
                        "browser_navigate", url,
                        f"CF_INTERSTITIAL status={status_code}",
                    )
                else:
                    self.s.record_step(
                        "browser_navigate", url,
                        f"NETWORK_BLOCKED status={status_code}",
                    )
                return caption
            elif status_code == 404:
                caption += _build_network_block_message(404, actual_url)
                self.s.record_failed_navigation(actual_url, 404)
                self.s.record_step("browser_navigate", url, f"HTTP 404 at {actual_url}")
                return caption

        self.s.record_step("browser_navigate", url, f"title={data.get('title', '?')}")
        # Prefetch vision so the LLM's next browser_screenshot finds the
        # bboxes already cached.
        _schedule_vision_prefetch(self.s, session_id)

        if regression:
            caption += "\n[WARNING: You already visited this URL. Fix your approach on the CURRENT page instead of going backward. Do NOT restart from the beginning.]"

        # Surface captcha detection from the server
        if data.get("captchaDetected"):
            ct = data["captchaDetected"]["type"]
            caption += (
                f"\n\n[CAPTCHA DETECTED: {ct}] "
                f"Call browser_solve_captcha(session_id='{session_id}', method='auto') to solve it."
            )

        if data.get("screenshot") and self.s.screenshot_budget > 0:
            self.s.screenshot_budget -= 1
            if actual_url:
                self.s.mark_screenshot_taken(
                    actual_url,
                    self.s.hash_page_content(data.get("elements", "") or data.get("title", "")),
                )
            return await self.s.build_tool_result_blocks(
                data["screenshot"],
                caption,
                intent=intent or "verify navigation succeeded",
                url=actual_url,
                elements=data.get("elements"),
            )
        return caption


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        direction=StringSchema("Scroll direction: up or down", nullable=True),
        percent=NumberSchema(description="Scroll to exact percentage 0-100", nullable=True),
        required=["session_id"],
    )
)
class BrowserScrollTool(Tool):
    name = "browser_scroll"
    description = "Scroll the page up or down, or to a specific percentage."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, direction: str | None = None, percent: float | None = None, **kw: Any) -> Any:
        print(f"\n>> browser_scroll({direction or f'{percent}%'})")
        gate = await _feedback_gate("browser_scroll")
        if gate:
            return gate
        sync_block = await self.s.ensure_vision_synced(reason="browser_scroll")
        if sync_block:
            return sync_block
        self.s._brain_turn_counter += 1
        self.s.capture_action_snapshot(target_index=None)
        await self.s.inter_action_pause()
        payload: dict[str, Any] = {}
        if percent is not None:
            payload["percent"] = percent
        else:
            payload["direction"] = direction or "down"
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/scroll",
            json=payload,
            timeout=15.0,
        )
        r.raise_for_status()
        data = r.json()
        # Fetch updated elements after scroll (new elements may be visible)
        if not data.get("elements"):
            elements = await _fetch_elements(session_id, self.s)
            if elements:
                data["elements"] = elements
        action = f"Scrolled to {percent}%" if percent is not None else f"Scrolled {direction or 'down'}"
        self.s.record_step("browser_scroll", action, "ok")
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        return await _append_fresh_vision(
            _vision_task,
            self.s.build_text_only(data, action),
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        text=StringSchema("Text to wait for on the page", nullable=True),
        selector=StringSchema("CSS selector to wait for", nullable=True),
        timeout=IntegerSchema(description="Max wait time in seconds (default: 10)", nullable=True),
        required=["session_id"],
    )
)
class BrowserWaitForTool(Tool):
    name = "browser_wait_for"
    description = (
        "Wait for text or a CSS selector to appear on the page. "
        "Much better than blind helpers.sleep() — polls efficiently until the condition is met. "
        "Provide either 'text' or 'selector' (not both). FREE — no screenshot cost."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        text: str | None = None,
        selector: str | None = None,
        timeout: int | None = None,
        **kw: Any,
    ) -> str:
        if not text and not selector:
            return "Error: provide either 'text' or 'selector' parameter."

        timeout_s = timeout or 10
        label = f'text="{text}"' if text else f'selector="{selector}"'
        print(f"\n>> browser_wait_for({label}, timeout={timeout_s}s)")

        if text:
            script = f"""
                const deadline = Date.now() + {timeout_s * 1000};
                while (Date.now() < deadline) {{
                    if (document.body.innerText.includes({json.dumps(text)})) {{
                        return {{found: true, title: document.title, url: location.href}};
                    }}
                    await new Promise(r => setTimeout(r, 500));
                }}
                return {{found: false, title: document.title, url: location.href, bodyPreview: document.body.innerText.substring(0, 200)}};
            """
        else:
            script = f"""
                const deadline = Date.now() + {timeout_s * 1000};
                while (Date.now() < deadline) {{
                    if (document.querySelector({json.dumps(selector)})) {{
                        return {{found: true, title: document.title, url: location.href}};
                    }}
                    await new Promise(r => setTimeout(r, 500));
                }}
                return {{found: false, title: document.title, url: location.href, bodyPreview: document.body.innerText.substring(0, 200)}};
            """

        client_timeout = max(30.0, timeout_s + 10)
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/script",
            json={"code": script, "timeout": timeout_s * 1000 + 5000},
            timeout=client_timeout,
        )
        r.raise_for_status()
        data = r.json()

        if not data.get("success"):
            self.s.log_activity(f"wait_for({label})", f"script error: {data.get('error', '?')[:60]}")
            return f"Wait failed (script error): {data.get('error', 'unknown')}"

        result = data.get("result", {})
        if result.get("found"):
            self.s.log_activity(f"wait_for({label})", "found")
            # Fetch updated elements
            elements = await _fetch_elements(session_id, self.s)
            response = f"Found! Page: {result.get('url', '?')} | Title: {result.get('title', '?')}"
            if elements:
                response += f"\n\nInteractive elements:\n{elements}"
            return response
        else:
            self.s.log_activity(f"wait_for({label})", f"timeout after {timeout_s}s")
            return (
                f"Not found after {timeout_s}s (selector/text did NOT match). "
                f"This is a RENDERING-SPEED or SELECTOR issue — NOT a network "
                f"block. DO NOT escalate to Tier 3.\n"
                f"Page: {result.get('url', '?')} | Title: {result.get('title', '?')}\n"
                f"Page preview: {result.get('bodyPreview', 'N/A')}\n"
                f"Next steps:\n"
                f"  - browser_screenshot to see the actual rendered state.\n"
                f"  - Retry browser_wait_for with a longer timeout (20-30s) "
                f"or a different selector (e.g. try 'form', 'button[type=submit]' "
                f"instead of generic 'input').\n"
                f"  - browser_run_script with `return document.body.innerText.length` "
                f"to confirm the page has actually rendered content."
            )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        target_text=StringSchema(
            "Text or regex of the element you want to scroll to. Substring "
            "match if it's not a valid regex. Optional if target_role given.",
            nullable=True,
        ),
        target_role=StringSchema(
            "ARIA role / tagName to filter on (e.g. 'button', 'h2'). "
            "Optional if target_text given.",
            nullable=True,
        ),
        direction=StringSchema(
            "'down' (default) or 'up'.",
            nullable=True,
        ),
        max_iterations=IntegerSchema(
            "Safety cap on scroll steps. Default 10, max 40.",
            nullable=True,
        ),
        step_ratio=NumberSchema(
            description="Fraction of viewport to scroll per step (0.1–1.0). Default 0.8.",
            nullable=True,
        ),
        required=["session_id"],
    )
)
class BrowserScrollUntilTool(Tool):
    name = "browser_scroll_until"
    description = (
        "Closed-loop scroll. Walks the page in `direction` until an "
        "element matching `target_text` (substring or regex) and/or "
        "`target_role` becomes visible, the page can't scroll further, "
        "or `max_iterations` elapses. Cheap — uses interactive-element "
        "polling between steps, no screenshot per iteration. Returns a "
        "structured outcome with `reason` ('matched' | 'page_end' | "
        "'page_start' | 'max_iterations') so the brain knows whether "
        "to act, retreat, or give up. Prefer this over browser_scroll "
        "when you know what you're scrolling toward — it stops at the "
        "right place AND tells you when content runs out, instead of "
        "blindly scrolling and re-screenshotting."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        target_text: str | None = None,
        target_role: str | None = None,
        direction: str | None = None,
        max_iterations: int | None = None,
        step_ratio: float | None = None,
        **kw: Any,
    ) -> Any:
        gate = await _feedback_gate("browser_scroll_until")
        if gate:
            return gate
        sync_block = await self.s.ensure_vision_synced(reason="browser_scroll_until")
        if sync_block:
            return sync_block
        self.s._brain_turn_counter += 1
        self.s.capture_action_snapshot(target_index=None)
        await self.s.inter_action_pause()

        if not (target_text and target_text.strip()) and not (target_role and target_role.strip()):
            return (
                "[scroll_until_failed:no_target] Provide target_text or "
                "target_role. Substring match works for most cases — pass "
                "the visible text of the element you want to find."
            )

        payload: dict[str, Any] = {
            "direction": direction or "down",
        }
        if target_text and target_text.strip():
            payload["targetText"] = target_text.strip()
        if target_role and target_role.strip():
            payload["targetRole"] = target_role.strip()
        # Filter-keyword targets — Price, Region, Sort, etc. — usually
        # live in collapsed sidebar accordions on dense product pages.
        # If the brain didn't bump max_iterations, give them more rope
        # so the closed-loop walk reaches the section header before
        # bailing. Empirically max=3 (the brain's typical cap) returns
        # page_end on most retail listings; max>=10 catches the section
        # header reliably.
        FILTER_KEYWORDS = (
            "price", "region", "sort", "filter", "color", "size",
            "brand", "category", "type", "rating", "year",
            "country", "vintage", "varietal", "pairing",
        )
        tt_lower = (target_text or "").strip().lower()
        looks_like_filter = any(kw in tt_lower for kw in FILTER_KEYWORDS) and len(tt_lower) <= 30
        if max_iterations is not None:
            payload["maxIterations"] = int(max_iterations)
        elif looks_like_filter:
            # Brain didn't pick a cap; for filter words, default to 12.
            payload["maxIterations"] = 12
        if step_ratio is not None:
            payload["stepRatio"] = float(step_ratio)

        target_disp = target_text or f"role={target_role}"
        print(
            f"\n>> browser_scroll_until({target_disp!r}, dir={payload['direction']})"
        )

        try:
            r = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/scroll-until",
                json=payload,
                timeout=30.0,  # closed-loop can take 10 iterations × ~300ms
            )
        except Exception as exc:
            return f"[scroll_until_failed] request error: {exc}"

        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            return f"[scroll_until_failed] HTTP {r.status_code}: {err}"

        data = r.json()
        outcome = data.get("outcome") or {}
        reason = str(outcome.get("reason") or "unknown")
        iters = int(outcome.get("iterations") or 0)
        scrolled = int(outcome.get("scrolledPx") or 0)

        # Update scroll telemetry so the next vision pass sees a fresh
        # [SCROLL_STATE …] line including reached_bottom/reached_top hints
        # that came from this closed-loop call.
        _update_scroll_telemetry(
            self.s,
            data.get("scrollInfo"),
            payload["direction"],
            extra={
                "last_scroll_reason": reason,
                "reached_bottom": reason == "page_end",
                "reached_top": reason == "page_start",
            },
        )

        # Mirror the BrowserDragSliderUntilTool record convention so
        # step_history shows a clear summary line for downstream
        # loop-detection and task-graph signal evaluation.
        self.s.record_step(
            "browser_scroll_until",
            f"{target_disp!r} → {reason} in {iters} iters ({scrolled}px)",
            data.get("url", ""),
        )

        lines: list[str] = []
        if outcome.get("found"):
            matched = outcome.get("matchedText") or ""
            sel = outcome.get("matchedSelector") or ""
            lines.append(
                f"FOUND {target_disp!r} after {iters} iter(s), "
                f"scrolled {scrolled}px. matched={matched[:80]!r} "
                f"selector={sel}"
            )
        else:
            tag = (
                "page_end" if reason == "page_end"
                else "page_start" if reason == "page_start"
                else reason
            )
            lines.append(
                f"[scroll_until_failed:{tag}] target {target_disp!r} not "
                f"found after {iters} iter(s) ({scrolled}px). "
                f"reason={reason}."
            )
            if reason == "page_end":
                if looks_like_filter:
                    # Sidebar-accordion recovery recipe inline. Soul.md
                    # documents this but the brain often doesn't follow
                    # it when staring at a `[scroll_until_failed]` line;
                    # putting the recipe in the failure caption itself is
                    # much harder to ignore.
                    lines.append(
                        "  Window scroll hit page bottom but the filter "
                        "control didn't appear — it's almost certainly "
                        "inside a collapsed sidebar accordion that lives "
                        "in its own scroll container the window scroll "
                        "doesn't touch. Recovery (in this exact order):\n"
                        f"    1) browser_screenshot — vision will surface "
                        f"every visible filter section header (Region, "
                        f"Type, Price, Food Pairings, etc.) as V_n bboxes. "
                        f"The collapsed +/▸ icons become separate bboxes "
                        f"too.\n"
                        f"    2) browser_click_at(V_n) on the section "
                        f"header whose label MATCHES {target_disp!r} (or "
                        f"the closest synonym) — this expands the "
                        f"accordion.\n"
                        f"    3) Re-call browser_scroll_until "
                        f"(target_text={target_disp!r}) — now the option "
                        f"will be visible. If it's still not, the page "
                        f"genuinely doesn't have this filter; mark the "
                        f"corresponding constraint via "
                        f"browser_brief_mark(status='not_applicable').\n"
                        "  Do NOT call browser_eval to hunt for the "
                        "accordion DOM — vision already surfaces it. Do "
                        "NOT call browser_run_script to expand it — the "
                        "JS click is isTrusted=false and many sites "
                        "won't fire the underlying React handler."
                    )
                else:
                    lines.append(
                        "  Page can't scroll further down. The target may "
                        "be above (try direction='up') or may not exist on "
                        "this page — verify by checking the elements list "
                        "below."
                    )
            elif reason == "page_start":
                lines.append(
                    "  Already at top of page. Try direction='down' or "
                    "verify the target text/role is correct."
                )
            elif reason == "max_iterations":
                lines.append(
                    "  Hit iteration cap. If you believe the target exists "
                    "further on, raise max_iterations (cap is 40) or "
                    "use a more specific target_text."
                )

        if data.get("elements"):
            lines.append(str(data["elements"]))

        # Schedule a vision prefetch so the next browser_screenshot is
        # cached — same convention as the other scroll tools.
        self.s.advance_observation_token("scroll_until")
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        return await _append_fresh_vision(
            _vision_task, "\n".join(lines),
            state=self.s,
        )


