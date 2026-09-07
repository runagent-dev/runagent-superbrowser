# e6_subelement — Sub-element targeting inside a merged bounding box (offline, no LLM)

**Question.** When vision returns one merged box for a compound row (the paper's "United States ▼" case:
label + chevron in a single box), does the snapper's target-selection strategy determine whether the click
lands on the intended small control? This is the only experiment that isolates the grounding step from
the model: it drives the TypeScript click endpoint directly on local HTML fixtures and costs no credits.

**Arms (TS-side, `SUPERBROWSER_SNAP_STRATEGY`).** `chevron` (production: pinpoint + label/chevron
weights), `center` (raw bbox centre / largest-area candidate), `dom_alt` (pinpoint + nearest interactive
DOM ancestor, no chevron/label weights). Each strategy needs its own server process, so the runner starts
one per strategy (`--manage-server`, on a free port; your `:3100` server is not touched).

**Fixtures.** `fixtures/*.html` — 6 families × 5–6 variants (35 items in `fixtures/manifest.json`):
`label_chevron`, `row_checkbox`, `nested_card_action`, `split_button`, `datepicker_arrow`,
`disclosure_triangle`, plus 4 **control** items where the large label *is* the intended target (catches
chevron-boost false positives). `recorder.js` records which element received the click.

**Conditions.** `merged_row` = the snapper receives the rect of the whole row plus the vision-style label
(the failure case); `exact` = the rect of the intended sub-element (sanity baseline: must be ~100 %).

**Metric.** Hit = the recorded click target is the intended element or inside it; accuracy per
family × strategy with Wilson 95 % CIs; paired exact McNemar between strategies (`paired.csv`).

**Committed result (2026-09-06, `eval/artifacts/e6_subelement/`).** `exact`: 35/35 for all three
strategies. `merged_row`, non-control items: chevron 20/31, center 21/31, dom_alt 21/31; every strategy
scores 0/5 on `split_button` and `datepicker_arrow` (the merged box carries no signal about the small
control) and 100 % on controls. Reading for the paper: the sub-element mechanism is only demonstrable for
chevron/disclosure-style rows — narrow the claim to those rather than "compound rows" in general.

```bash
python -m eval.experiments.e6_subelement.run --manage-server            # ~1 min, deterministic
python -m eval.experiments.e6_subelement.run --strategies chevron --assume-server  # against a server you started
python -m eval.experiments.e6_subelement.analyze                        # -> accuracy.{csv,tex,png}, paired.csv
```
Requires `npm run build` first (the harness launches `node build/index.js`). The managed server is started with
`SUPERBROWSER_ALLOW_LOCAL_FIXTURES=1` so the loopback fixture server passes the SSRF guard; with `--assume-server`
your own server (`SUPERBROWSER_URL`, default `http://localhost:3100`) must have been started with that variable
and, for a non-default strategy, with `SUPERBROWSER_SNAP_STRATEGY` set to match.
