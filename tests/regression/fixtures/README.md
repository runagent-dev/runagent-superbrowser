# Click-resolution regression fixtures

Captured from production runs to A/B-benchmark v3 click accuracy
against v2's pipeline restoration (Phase 2 of the refactor).

## Schema

Each `*.json` file is either a single capture object or a list of
captures. Each capture must include:

```json
{
  "name": "amazon_search_2026_05_05",
  "url": "https://www.amazon.com/s?k=headphones",
  "image_width": 2560,
  "image_height": 1600,
  "dpr": 2.0,
  "box_2d": [120, 200, 180, 480],
  "expected_target": "input#twotabsearchtextbox",
  "expected_coords": [217, 112],
  "tolerance_px": 5,
  "notes": "Search bar; v2 hits, v3 misses by 18px (probably SoM overlay drift)"
}
```

Required fields: `image_width`, `image_height`, `box_2d`,
`expected_coords`. Everything else is optional.

`expected_coords` is the *actual* CSS-pixel center the production run
clicked (recorded by the cursor tool when `CLICK_CAPTURE_DIR` is set).
`tolerance_px` defaults to 5px.

## Capturing in production

Set `CLICK_CAPTURE_DIR=/path/to/captures` before launching the worker.
The cursor tool will write one JSON per click. Copy the captures into
this folder and run:

```bash
python tests/regression/replay_clicks.py
```

Use `--strict` in CI to fail on any mismatch.

## When to add fixtures

- Whenever a production run shows a click missing the visual target by
  more than ~5px on a labelled bbox.
- Cover the failure modes the user reported: small/dense controls,
  retina viewports, calculator widgets, search bars, sort dropdowns.
- Aim for 20+ captures across 5+ sites before the Phase 2 bbox-pipeline
  restoration ships, so we have a real benchmark.
