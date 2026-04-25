# SpotHero (spothero.com) — playbook

Known automation-hostile patterns on spothero.com. Internalize these
before the first mutation; do not "explore" the date/time picker.

## Address search (homepage)

- The search input is a React autocomplete. After typing, the
  suggestions list renders ~300-600ms later.
- ALWAYS use `browser_semantic_type(target='Enter destination', text=...)`
  followed by `browser_screenshot` before picking a suggestion. The
  suggestion list items appear as new `[V_n]` only after the
  screenshot.
- Click the suggestion via `browser_click_at(vision_index=V_n)` where
  V_n is the one labelled with the place name + street. Do NOT press
  Enter in the input — SpotHero occasionally routes Enter to the
  form submit without a destination, landing on `/search` with no
  bounds set.

## Date / time picker (the dangerous part)

SpotHero's date/time picker is a custom React widget. It does NOT
accept free-form typing. Clicking calendar cells via
`browser_click_at` works intermittently because the widget re-renders
on every focus/blur and [V_n] indices shift mid-sequence.

**Use keyboard-only navigation:**

1. Click the "Start" (entry) time field once via
   `browser_click_at(V_n)` — opens the picker.
2. Use `browser_keys(keys='Tab')` to move between day / month / year /
   hour / minute subfields inside the picker.
3. For each subfield, use `browser_keys(keys='ArrowUp')` or
   `ArrowDown` to change the value, OR type the digits directly via
   `browser_keys(keys='4,2,5')` for Apr 25.
4. After setting all fields, press `browser_keys(keys='Enter')` to
   commit.
5. Do the same for the "End" time field.
6. Only THEN click the search/filter/submit button.

**Anti-patterns for this widget:**
- Do NOT `browser_type_at(V_n, 'Apr 26, 2026')` — the input rejects
  free-form date strings and the failure is silent.
- Do NOT click individual calendar cells (`browser_click_at(V_n)`
  on "27") — the V_n index for "27" changes every time the picker
  re-renders, and re-rendering happens on focus/blur.
- Do NOT `browser_run_script` to set `input.value` — the sandbox
  rejects it, and even if it didn't the React controlled input would
  ignore the change.

## Search submission

The "Search parking" button is usually `V_n` with role="button" and
a label containing "Search" or "Find parking". Prefer
`browser_semantic_click(target='Find parking')` over
`browser_click_at` because the button's V_n index shifts based on
whether date/time are set.

## Pricing extraction

Prices come from the results list after a successful search. Use
`browser_get_markdown` (free) to extract; don't try to scrape via
`run_script`. The list items contain `$NN.NN / total` or
`$NN.NN · X hours` formats; extract the numeric `value`, `unit`,
and `currency` per the orchestrator's extraction contract.

## Escalation

If the date/time picker STILL won't accept the keyboard flow after
3 attempts, call `browser_request_help` with a concrete hint: "date
picker keyboard input not committing — human, please set Start=<date>
Entry=<time> End=<date> Exit=<time> and click Search". Do NOT give up
silently.
