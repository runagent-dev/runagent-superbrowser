"""Python-side live viewer for Tier-3 (patchright) sessions.

Feature-parity with the TS viewer at `:3100/session/:id/view`:

  GET  /t3/session/<sid>/view        → HTML page with overlays
  GET  /t3/session/<sid>/screenshot  → fresh JPEG (polling fallback)
  POST /t3/session/<sid>/click       → {x,y} → dispatches to patchright
  WS   /t3/session/<sid>/ws          → live events: cursor, click target,
                                       vision bboxes, keystrokes, nav,
                                       and (Phase B) CDP screencast frames

When the WS is connected, the viewer stops polling and renders screencast
frames + overlays in real time. When WS drops, polling resumes
automatically — the same fallback mechanic T1 uses.

Started on demand via `ensure_started()` and lives for the process.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from typing import Optional

from aiohttp import WSMsgType, web

from . import interactive_session as _t3
from . import t3_event_bus as _bus

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# HTML template — self-contained viewer with overlay JS.
#
# All overlays (cursor arrow, click-target crosshair, vision-bbox
# layer, keystroke indicator, URL banner) are driven off the WS
# event stream produced by `t3_event_bus.default()`. Polling the
# `/screenshot` endpoint is the fallback frame source — it runs
# unconditionally at start-up and gets paused once a WS screencast
# frame arrives, resuming on WS close.
#
# Kept dependency-free (no framework, no bundler) so the file is
# trivially diffable and no build step is needed in the worker VM.
# ------------------------------------------------------------------

_HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0f1115">
<title>T3 viewer · {sid}</title>
<style>
  :root {{
    color-scheme: light dark;
    --bg: #0f1115; --panel: #181a20; --panel-2: #1f222a;
    --border: #2a2e38; --text: #e9ecf2; --muted: #8b93a7;
    --accent: #4f8cff; --accent-2: #2d6a4f; --ok: #34d399;
    --warn: #ffc857; --warn-bg: #3a2a00; --err: #ff6b6b;
  }}
  @media (prefers-color-scheme: light) {{
    :root {{
      --bg: #f7f8fb; --panel: #ffffff; --panel-2: #f0f2f7;
      --border: #e1e5ee; --text: #11161f; --muted: #5c6577;
      --warn-bg: #fff8e0;
    }}
  }}
  * {{ box-sizing: border-box; -webkit-tap-highlight-color: transparent; }}
  html, body {{ margin: 0; height: 100%; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    overscroll-behavior: none; }}
  header {{
    display: flex; align-items: center; gap: 10px; padding: 10px 14px;
    padding-top: max(10px, env(safe-area-inset-top));
    background: var(--panel); border-bottom: 1px solid var(--border);
    position: sticky; top: 0; z-index: 10;
  }}
  header .title {{ font-weight: 600; font-size: 14px; }}
  header .session-id {{
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    font-size: 11px; color: var(--muted);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    max-width: 28vw;
  }}
  header .url-banner {{
    font-size: 12px; color: var(--muted); flex: 1 1 auto;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }}
  header button {{
    padding: 4px 10px; background: var(--panel-2); color: var(--text);
    border: 1px solid var(--border); border-radius: 4px;
    cursor: pointer; font-size: 12px;
  }}
  header button:hover {{ background: var(--border); }}
  header .status {{ font-size: 11px; color: var(--accent); min-width: 80px; text-align: right; }}
  #screen-wrap {{
    position: relative; display: flex; justify-content: center; padding: 12px;
    touch-action: manipulation;
  }}
  #screen {{
    max-width: 100%; height: auto;
    border: 1px solid var(--border); border-radius: 6px;
    background: #000; cursor: crosshair;
    box-shadow: 0 4px 24px rgba(0,0,0,0.25);
  }}
  /* Cursor SVG arrow — follows emit_cursor_move at 30 FPS. */
  #cursor-overlay {{
    position: absolute; pointer-events: none; z-index: 5;
    transition: left 33ms linear, top 33ms linear;
    display: none;
  }}
  /* Click-target crosshair — pulses briefly after each click */
  .click-target {{
    position: absolute; pointer-events: none; z-index: 6;
    margin-left: -14px; margin-top: -14px; width: 28px; height: 28px;
    animation: click-pulse 0.55s ease-out forwards;
  }}
  .click-target.snapped {{ color: var(--ok); }}
  .click-target.miss {{ color: var(--warn); }}
  .click-target svg {{ width: 28px; height: 28px; }}
  .click-target circle {{ stroke: currentColor; }}
  @keyframes click-pulse {{
    from {{ transform: scale(0.5); opacity: 1; }}
    to   {{ transform: scale(1.8); opacity: 0; }}
  }}
  .click-bbox {{
    position: absolute; pointer-events: none; z-index: 5;
    border: 2px solid var(--ok); border-radius: 3px;
    animation: bbox-fade 0.8s ease-out forwards;
  }}
  .click-bbox.miss {{ border-color: var(--warn); }}
  /* Strategy chip — shows which click ladder rung landed
     (primary/keyboard/js/parent). Helps debug fallback paths. */
  .click-bbox .strategy-chip {{
    position: absolute; top: -16px; right: -1px;
    font: 9px/14px ui-monospace, SFMono-Regular, Consolas, monospace;
    background: rgba(0,0,0,0.7); color: #fff;
    padding: 0 4px; border-radius: 3px 3px 0 0;
    white-space: nowrap;
  }}
  .click-bbox.miss .strategy-chip {{ background: rgba(120,80,0,0.85); }}
  @keyframes bbox-fade {{
    from {{ opacity: 1; }}
    to   {{ opacity: 0; }}
  }}
  /* Vision-bbox layer — persistent. Dimmed when the model reported
     freshness != 'fresh'. No timer-based stale hide; bboxes only
     clear when a new pass arrives or the URL changes. */
  .vision-bbox-layer[data-freshness="uncertain"],
  .vision-bbox-layer[data-freshness="stale"] {{ opacity: 0.5; }}
  .vision-bbox {{
    position: absolute; pointer-events: none;
    border: 2px dashed rgba(79,140,255,0.6);
    border-radius: 2px; transition: opacity 0.4s ease;
  }}
  /* Role-coded colors mirror src/server/captcha-ui-html.ts so both
     viewers feel like the same product. button=red, link=green,
     input/checkbox=cyan, captcha+slider=amber. */
  .vision-bbox[data-role="button"]         {{ border-color: rgba(255,107,107,0.9); }}
  .vision-bbox[data-role="link"]           {{ border-color: rgba(150,206,180,0.9); }}
  .vision-bbox[data-role="input"]          {{ border-color: rgba(78,205,196,0.95); }}
  .vision-bbox[data-role="checkbox"]       {{ border-color: rgba(78,205,196,0.95); }}
  .vision-bbox[data-role="captcha_tile"]   {{ border-color: rgba(255,200,87,0.9); }}
  .vision-bbox[data-role="captcha_widget"] {{ border-color: rgba(255,200,87,0.9); }}
  .vision-bbox[data-role="slider_handle"]  {{ border-color: rgba(255,200,87,0.9); }}
  .vision-bbox[data-relevant="1"] {{ border-style: solid; border-width: 3px; }}
  .vision-bbox .vlabel {{
    position: absolute; left: 0; top: -16px;
    font-size: 10px; font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    padding: 1px 4px; background: rgba(79,140,255,0.9); color: #fff;
    border-radius: 2px 2px 0 0; white-space: nowrap;
    max-width: 160px; overflow: hidden; text-overflow: ellipsis;
  }}
  /* Tap-forward pulse (click originated from the viewer UI) */
  .pulse {{
    position: absolute; pointer-events: none;
    width: 32px; height: 32px;
    margin-left: -16px; margin-top: -16px;
    border-radius: 50%; border: 2px solid var(--accent);
    animation: pulse-anim 0.4s ease-out forwards;
  }}
  @keyframes pulse-anim {{
    from {{ transform: scale(0.4); opacity: 1; }}
    to   {{ transform: scale(1.7); opacity: 0; }}
  }}
  #typing-indicator {{
    display: none;
    padding: 4px 14px; font-size: 12px; color: var(--accent);
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    text-align: center;
    background: var(--panel);
    border-top: 1px solid var(--border);
  }}
  /* Instruction banner — surfaces human-input requests (captcha hints,
     countdown). Visibility toggled by JS when /human-input poll
     returns a pending request. */
  #instruct {{
    padding: 12px 14px; background: var(--warn-bg); color: var(--warn);
    border-bottom: 1px solid var(--border); display: none;
    font-size: 14px; line-height: 1.4;
  }}
  #instruct.visible {{ display: block; }}
  #instruct .label {{ font-weight: 600; margin-right: 6px; }}
  #instruct .countdown {{ float: right; font-variant-numeric: tabular-nums; opacity: 0.85; }}
  /* Status strip between screen and action bar. */
  #status-strip {{
    padding: 4px 14px; font-size: 12px; color: var(--muted);
    min-height: 18px; text-align: center;
  }}
  #status-strip .err {{ color: var(--err); }}
  #status-strip .ok {{ color: var(--ok); }}
  /* Sticky bottom action bar with Type / Stuck / Done buttons. */
  .actions {{
    display: flex; gap: 8px; padding: 10px 14px;
    padding-bottom: max(10px, env(safe-area-inset-bottom));
    background: var(--panel); border-top: 1px solid var(--border);
    position: sticky; bottom: 0; z-index: 10;
  }}
  .actions button {{
    flex: 1; min-height: 44px; /* iOS touch target */
    padding: 10px 14px; border-radius: 8px; border: 1px solid var(--border);
    background: var(--panel-2); color: var(--text);
    font-size: 15px; font-weight: 500;
    cursor: pointer; transition: background 0.12s ease;
  }}
  .actions button:active {{ background: var(--border); }}
  .actions button.primary {{
    background: var(--accent-2); border-color: var(--accent-2); color: white;
  }}
  .actions button.primary:active {{ background: #1f4d39; }}
  .actions button.warn {{ background: transparent; color: var(--muted); }}
  .actions button.hidden {{ display: none; }}
  /* Type modal — bottom sheet on mobile, centered on desktop. */
  #type-modal {{
    display: none;
    position: fixed; top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.55); backdrop-filter: blur(4px);
    align-items: flex-end; justify-content: center;
    z-index: 100;
  }}
  @media (min-width: 600px) {{
    #type-modal {{ align-items: center; }}
  }}
  #type-modal.visible {{ display: flex; }}
  #type-modal .inner {{
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 14px 14px 0 0; padding: 18px 16px;
    padding-bottom: max(18px, env(safe-area-inset-bottom));
    width: 100%; max-width: 480px;
  }}
  @media (min-width: 600px) {{
    #type-modal .inner {{ border-radius: 14px; }}
  }}
  #type-modal h3 {{ margin: 0 0 10px; font-size: 16px; }}
  #type-modal textarea {{
    width: 100%; min-height: 84px;
    background: var(--panel-2); color: var(--text);
    border: 1px solid var(--border); border-radius: 8px; padding: 10px;
    font-family: inherit; font-size: 16px; /* prevents iOS zoom */
    resize: vertical;
  }}
  #type-modal .row {{
    display: flex; gap: 8px; margin-top: 12px;
  }}
  #type-modal .row button {{
    flex: 1; min-height: 44px; padding: 10px;
    border-radius: 8px; border: 1px solid var(--border);
    background: var(--panel-2); color: var(--text);
    font-size: 15px; cursor: pointer;
  }}
  #type-modal .row button.primary {{
    background: var(--accent); border-color: var(--accent); color: white;
  }}
  /* Success overlay — shown when a pending human-input clears. */
  #success {{
    display: none;
    position: fixed; inset: 0;
    background: rgba(15,17,21,0.94);
    align-items: center; justify-content: center;
    flex-direction: column; gap: 14px;
    z-index: 200;
    padding: 24px;
  }}
  #success.visible {{ display: flex; }}
  #success .check {{
    width: 72px; height: 72px; border-radius: 50%;
    background: var(--ok);
    display: flex; align-items: center; justify-content: center;
    font-size: 40px; color: #003322;
  }}
  #success .msg {{ font-size: 17px; text-align: center; max-width: 320px; }}
</style>
</head>
<body>
<header>
  <div class="title">T3 viewer</div>
  <div class="session-id" title="{sid}">{sid}</div>
  <div class="url-banner" id="url-banner">—</div>
  <button id="pause">Pause</button>
  <div class="status" id="status">connecting…</div>
</header>
<div id="instruct">
  <span class="countdown" id="countdown"></span>
  <span class="label" id="instruct-label">Action needed:</span>
  <span id="instruct-msg">waiting…</span>
</div>
<div id="screen-wrap">
  <img id="screen" alt="Live browser view"/>
  <div id="cursor-overlay">
    <svg width="20" height="20" viewBox="0 0 20 20">
      <path d="M0,0 L0,16 L4.5,12 L8,19 L10.5,18 L7,11 L12,11 Z"
            fill="rgba(79,140,255,0.9)" stroke="#fff" stroke-width="1"/>
    </svg>
  </div>
</div>
<div id="typing-indicator"></div>
<div id="status-strip"></div>
<div class="actions">
  <button id="type-btn">Type…</button>
  <button id="stuck-btn" class="warn hidden">I'm stuck</button>
  <button id="done-btn" class="primary hidden">Done</button>
</div>
<div id="type-modal">
  <div class="inner">
    <h3>Type into the focused field</h3>
    <textarea id="type-input" placeholder="Type text, then Send. Use Enter for newline."></textarea>
    <div class="row">
      <button id="type-cancel">Cancel</button>
      <button id="type-send" class="primary">Send</button>
    </div>
  </div>
</div>
<div id="success">
  <div class="check">✓</div>
  <div class="msg" id="success-msg">Captcha cleared. The agent is resuming.<br>You can return to your chat.</div>
</div>
<script>
(() => {{
  const SESSION_ID = {sid_json};
  // Token is empty when TOKEN env unset. When present, it's appended
  // to all fetch URLs and the WS URL so this page works behind the
  // same Bearer-auth gate the TS server uses.
  const TOKEN = {token_json};
  // Parent T1 session id — surfaced as ?parent=<sid> when escalation
  // happened, so this viewer can poll the TS server's human-input
  // endpoint on behalf of the original session.
  const PARENT_SID = {parent_json};
  // T1 base URL — root of the TS server (port 3100 typically). Used
  // only for the human-input GET/POST proxy. Falls back to localhost
  // if SUPERBROWSER_PUBLIC_HOST isn't set; document the limitation
  // for reverse-proxy deployments.
  const T1_BASE = {t1_base_json};
  const POLL_MS = 250;
  const HUMAN_POLL_MS = 1200;

  const qsToken = TOKEN ? ('&token=' + encodeURIComponent(TOKEN)) : '';
  const qsTokenLead = TOKEN ? ('?token=' + encodeURIComponent(TOKEN)) : '';
  const headerToken = TOKEN ? {{ Authorization: 'Bearer ' + TOKEN }} : {{}};

  const $ = (id) => document.getElementById(id);
  const screen = $('screen');
  const screenWrap = $('screen-wrap');
  const cursorEl = $('cursor-overlay');
  const typingEl = $('typing-indicator');
  const statusEl = $('status');
  const statusStripEl = $('status-strip');
  const urlBanner = $('url-banner');
  const pauseBtn = $('pause');
  const instructEl = $('instruct');
  const instructMsg = $('instruct-msg');
  const instructLabel = $('instruct-label');
  const countdownEl = $('countdown');
  const doneBtn = $('done-btn');
  const stuckBtn = $('stuck-btn');
  const typeBtn = $('type-btn');
  const typeModal = $('type-modal');
  const typeInput = $('type-input');
  const successOverlay = $('success');

  // Initial guess; the actual size is read from the first screenshot's
  // naturalWidth/naturalHeight on load. Aligned with the T3 default
  // viewport (1280x1100) so projectPoint math is sane until the first
  // frame lands.
  let viewport = {{ width: 1280, height: 1100 }};
  let paused = false;
  let pollInterval = null;
  let ws = null;
  let wsConnected = false;

  // --- Screenshot polling (fallback source) -------------------------
  function refresh() {{
    if (paused) return;
    screen.src = '/t3/session/' + SESSION_ID + '/screenshot?_=' + Date.now() + qsToken;
  }}
  screen.addEventListener('load', () => {{
    viewport.width = screen.naturalWidth || viewport.width;
    viewport.height = screen.naturalHeight || viewport.height;
  }});
  screen.addEventListener('error', () => {{
    statusEl.textContent = 'screenshot failed';
  }});
  function startPolling() {{
    if (pollInterval) return;
    pollInterval = setInterval(refresh, POLL_MS);
    refresh();
  }}
  function stopPolling() {{
    if (pollInterval) {{ clearInterval(pollInterval); pollInterval = null; }}
  }}
  startPolling();

  pauseBtn.addEventListener('click', () => {{
    paused = !paused;
    pauseBtn.textContent = paused ? 'Resume' : 'Pause';
  }});

  // --- Visibility API: pause polling when tab is hidden -------------
  // Saves screenshot bandwidth on backgrounded tabs. WS screencast
  // continues if it was connected — only the polling fallback pauses.
  document.addEventListener('visibilitychange', () => {{
    if (document.hidden) {{
      stopPolling();
    }} else {{
      if (!wsConnected) startPolling();
      else refresh(); // immediate single fetch to fill any gap
    }}
  }});

  // --- Blob-URL frame setter (CDP screencast path) ------------------
  let prevBlobUrl = null;
  function setScreenFrame(base64Data) {{
    const binary = atob(base64Data);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    const blob = new Blob([bytes], {{ type: 'image/jpeg' }});
    const url = URL.createObjectURL(blob);
    screen.src = url;
    if (prevBlobUrl) URL.revokeObjectURL(prevBlobUrl);
    prevBlobUrl = url;
  }}

  // --- Coord projection: image-pixel → viewer-pixel -----------------
  function projectPoint(vx, vy) {{
    const rect = screen.getBoundingClientRect();
    const wrapRect = screenWrap.getBoundingClientRect();
    const natW = screen.naturalWidth || viewport.width;
    const natH = screen.naturalHeight || viewport.height;
    const sx = rect.width / natW;
    const sy = rect.height / natH;
    const ox = rect.left - wrapRect.left;
    const oy = rect.top - wrapRect.top;
    return {{ x: vx * sx + ox, y: vy * sy + oy }};
  }}

  // --- Cursor overlay -----------------------------------------------
  function updateCursor(vx, vy) {{
    const p = projectPoint(vx, vy);
    cursorEl.style.display = 'block';
    cursorEl.style.left = p.x + 'px';
    cursorEl.style.top = p.y + 'px';
  }}

  // --- Click-target crosshair + (optional) bbox outline --------------
  function showClickTarget(d) {{
    if (!d || typeof d.x !== 'number') return;
    const p = projectPoint(d.x, d.y);
    const el = document.createElement('div');
    el.className = 'click-target ' + (d.snapped ? 'snapped' : 'miss');
    el.style.left = p.x + 'px';
    el.style.top = p.y + 'px';
    el.innerHTML = '<svg viewBox="0 0 28 28" width="28" height="28">'
      + '<circle cx="14" cy="14" r="11" fill="none" stroke-width="3"/>'
      + '<line x1="14" y1="2"  x2="14" y2="8"  stroke="currentColor" stroke-width="2"/>'
      + '<line x1="14" y1="20" x2="14" y2="26" stroke="currentColor" stroke-width="2"/>'
      + '<line x1="2"  y1="14" x2="8"  y2="14" stroke="currentColor" stroke-width="2"/>'
      + '<line x1="20" y1="14" x2="26" y2="14" stroke="currentColor" stroke-width="2"/>'
      + '</svg>';
    screenWrap.appendChild(el);
    setTimeout(() => {{ try {{ el.remove(); }} catch (e) {{}} }}, 650);
    if (d.bbox && typeof d.bbox.x0 === 'number') {{
      const tl = projectPoint(d.bbox.x0, d.bbox.y0);
      const br = projectPoint(d.bbox.x1, d.bbox.y1);
      const box = document.createElement('div');
      box.className = 'click-bbox ' + (d.snapped ? '' : 'miss');
      box.style.left = tl.x + 'px';
      box.style.top = tl.y + 'px';
      box.style.width = Math.max(0, br.x - tl.x) + 'px';
      box.style.height = Math.max(0, br.y - tl.y) + 'px';
      // Strategy chip (when emitted by interactive_session.py click
      // ladder): tells the operator which rung landed the click.
      if (d.strategy) {{
        const chip = document.createElement('span');
        chip.className = 'strategy-chip';
        chip.textContent = d.strategy;
        if (d.target) chip.title = d.target;
        box.appendChild(chip);
      }}
      screenWrap.appendChild(box);
      setTimeout(() => {{ try {{ box.remove(); }} catch (e) {{}} }}, 850);
    }}
  }}

  // --- Vision bbox layer (persistent; URL-bound visibility) ---------
  // Matches the T1 viewer's behaviour:
  //   - Overlay stays until a new vision_bboxes event replaces it OR
  //     the page URL changes (clear on navigation).
  //   - No timer-driven fade. A stale / uncertain freshness flag from
  //     the vision model just dims the layer to 50% opacity.
  //   - vision_pending shows a transient "vision updating…" status
  //     so the user knows a refresh is in flight.
  let visionLayer = null;
  let visionLayerUrl = '';
  let visionPendingTimer = null;
  function clearVisionBboxes() {{
    if (visionLayer) {{ try {{ visionLayer.remove(); }} catch (e) {{}} visionLayer = null; }}
    visionLayerUrl = '';
  }}
  function showVisionBboxes(d) {{
    if (!d || !Array.isArray(d.bboxes)) return;
    // Cancel pending indicator — the pass landed.
    if (visionPendingTimer) {{ clearTimeout(visionPendingTimer); visionPendingTimer = null; }}
    if (visionLayer) {{ try {{ visionLayer.remove(); }} catch (e) {{}} visionLayer = null; }}
    const layer = document.createElement('div');
    layer.className = 'vision-bbox-layer';
    const freshness = d.freshness || 'fresh';
    layer.dataset.freshness = freshness;
    layer.style.cssText = 'position:absolute;left:0;top:0;right:0;bottom:0;pointer-events:none;z-index:4;';
    for (const b of d.bboxes) {{
      if (typeof b.x0 !== 'number') continue;
      const tl = projectPoint(b.x0, b.y0);
      const br = projectPoint(b.x1, b.y1);
      const w = Math.max(0, br.x - tl.x);
      const h = Math.max(0, br.y - tl.y);
      if (w < 4 || h < 4) continue;
      const div = document.createElement('div');
      div.className = 'vision-bbox';
      div.dataset.role = b.role || 'other';
      div.dataset.relevant = b.intent_relevant ? '1' : '0';
      div.style.left = tl.x + 'px';
      div.style.top = tl.y + 'px';
      div.style.width = w + 'px';
      div.style.height = h + 'px';
      if (b.label || b.index) {{
        const lab = document.createElement('span');
        lab.className = 'vlabel';
        const idx = b.index ? '[V' + b.index + '] ' : '';
        lab.textContent = idx + (b.label || b.role || '');
        div.appendChild(lab);
      }}
      layer.appendChild(div);
    }}
    screenWrap.appendChild(layer);
    visionLayer = layer;
    visionLayerUrl = d.url || '';
    // Status strip: show freshness + round-trip so the viewer doesn't
    // need devtools to see vision state.
    const bits = ['vision: ' + freshness];
    if (typeof d.latencyMs === 'number') bits.push(d.latencyMs + 'ms');
    if (wsConnected) statusEl.textContent = 'streaming — ' + bits.join(' · ');
  }}
  function showVisionPending() {{
    if (wsConnected) statusEl.textContent = 'streaming — vision updating…';
    if (visionPendingTimer) clearTimeout(visionPendingTimer);
    visionPendingTimer = setTimeout(() => {{
      visionPendingTimer = null;
      if (wsConnected) statusEl.textContent = 'streaming';
    }}, 10000);
  }}

  // --- Keystroke indicator ------------------------------------------
  let typingBuffer = '';
  let typingTimeout = null;
  function showKeystroke(key) {{
    if (key === 'Backspace') {{
      typingBuffer = typingBuffer.slice(0, -1);
    }} else if (key === 'Enter') {{
      typingBuffer += '\u23CE';
    }} else if (typeof key === 'string') {{
      typingBuffer += key;
    }}
    if (typingBuffer.length > 40) typingBuffer = '…' + typingBuffer.slice(-37);
    typingEl.textContent = 'typing: ' + typingBuffer;
    typingEl.style.display = 'block';
    clearTimeout(typingTimeout);
    typingTimeout = setTimeout(() => {{
      typingEl.style.display = 'none';
      typingBuffer = '';
    }}, 2000);
  }}

  // --- Navigation banner --------------------------------------------
  function showNavigation(d) {{
    const txt = d.title ? (d.title + ' — ' + d.url) : (d.url || '—');
    urlBanner.textContent = txt;
    urlBanner.title = d.url || '';
  }}

  // --- WebSocket subscription --------------------------------------
  function connectWS() {{
    try {{
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      const wsUrl = proto + '//' + location.host + '/t3/session/' + SESSION_ID + '/ws'
        + qsTokenLead;
      ws = new WebSocket(wsUrl);
      ws.onopen = () => {{
        wsConnected = true;
        statusEl.textContent = 'streaming';
      }};
      ws.onmessage = (evt) => {{
        let msg;
        try {{ msg = JSON.parse(evt.data); }} catch (e) {{ return; }}
        switch (msg.type) {{
          case 'screencast':
            if (msg.payload && msg.payload.data) {{
              setScreenFrame(msg.payload.data);
              // Once screencast is flowing, the polling fallback is
              // redundant — stop it to avoid double-fetching frames.
              stopPolling();
            }}
            break;
          case 'cursor_move':
            updateCursor(msg.payload.x, msg.payload.y);
            break;
          case 'click_target':
            // Reposition the cursor SVG to the click site even if no
            // cursor_move arrived first. Late subscribers + js/parent
            // click strategies skip _move_cursor_smooth, so otherwise
            // they'd see click rings with no arrow.
            if (msg.payload && typeof msg.payload.x === 'number') {{
              updateCursor(msg.payload.x, msg.payload.y);
            }}
            showClickTarget(msg.payload);
            break;
          case 'vision_bboxes':
            showVisionBboxes(msg.payload);
            break;
          case 'vision_pending':
            showVisionPending();
            break;
          case 'keystroke':
            showKeystroke(msg.payload.key);
            break;
          case 'navigation':
            // URL changed → the previous pass's bboxes are anchored to
            // a page that no longer exists. Clear them immediately;
            // the next vision pass will replace.
            if (msg.payload && msg.payload.url && msg.payload.url !== visionLayerUrl) {{
              clearVisionBboxes();
            }}
            showNavigation(msg.payload);
            break;
          case 'drag':
            // Render as two rapid click-targets at the endpoints, and
            // glide the cursor SVG between them so the operator sees
            // the drag direction even if no cursor_move events landed.
            if (msg.payload && typeof msg.payload.startX === 'number') {{
              updateCursor(msg.payload.startX, msg.payload.startY);
            }}
            showClickTarget({{ x: msg.payload.startX, y: msg.payload.startY, snapped: true }});
            setTimeout(() => {{
              if (typeof msg.payload.endX === 'number') {{
                updateCursor(msg.payload.endX, msg.payload.endY);
              }}
              showClickTarget({{ x: msg.payload.endX, y: msg.payload.endY, snapped: true }});
            }}, 200);
            break;
        }}
      }};
      ws.onclose = () => {{
        wsConnected = false;
        cursorEl.style.display = 'none';
        statusEl.textContent = 'reconnecting…';
        // Bring polling back; reconnect after a beat.
        startPolling();
        setTimeout(connectWS, 2000);
      }};
      ws.onerror = () => {{ try {{ ws.close(); }} catch (e) {{}} }};
    }} catch (e) {{
      statusEl.textContent = 'no ws';
    }}
  }}
  connectWS();

  // --- Tap → click dispatch (unchanged from pre-overlay version) ----
  function showPulse(clientX, clientY) {{
    const wrapRect = screenWrap.getBoundingClientRect();
    const pulse = document.createElement('div');
    pulse.className = 'pulse';
    pulse.style.left = (clientX - wrapRect.left) + 'px';
    pulse.style.top = (clientY - wrapRect.top) + 'px';
    screenWrap.appendChild(pulse);
    setTimeout(() => pulse.remove(), 380);
  }}
  screen.addEventListener('pointerup', async (e) => {{
    e.preventDefault();
    showPulse(e.clientX, e.clientY);
    const rect = screen.getBoundingClientRect();
    const natW = screen.naturalWidth || viewport.width;
    const natH = screen.naturalHeight || viewport.height;
    const x = Math.round((e.clientX - rect.left) * (natW / rect.width));
    const y = Math.round((e.clientY - rect.top) * (natH / rect.height));
    try {{
      await fetch('/t3/session/' + SESSION_ID + '/click' + qsTokenLead, {{
        method: 'POST',
        headers: Object.assign({{ 'content-type': 'application/json' }}, headerToken),
        body: JSON.stringify({{ x, y }}),
      }});
      statusEl.textContent = 'tap (' + x + ',' + y + ')';
    }} catch (err) {{
      statusEl.textContent = 'tap failed';
    }}
  }});

  // --- Type modal: pipe text via /t3/session/<sid>/keys -------------
  // The viewer already auto-focuses whatever element was clicked, so
  // typing here lands in the field the operator just tapped. Each
  // char is dispatched as one /keys call so the agent's
  // emit_keystroke fires per char (mirrors the T1 pattern at
  // captcha-ui-html.ts:684-700).
  typeBtn.addEventListener('click', () => {{
    typeModal.classList.add('visible');
    setTimeout(() => typeInput.focus(), 50);
  }});
  $('type-cancel').addEventListener('click', () => {{
    typeModal.classList.remove('visible');
    typeInput.value = '';
  }});
  $('type-send').addEventListener('click', async () => {{
    const text = typeInput.value;
    typeModal.classList.remove('visible');
    typeInput.value = '';
    if (!text) return;
    let sent = 0;
    for (const ch of text) {{
      try {{
        await fetch('/t3/session/' + SESSION_ID + '/keys' + qsTokenLead, {{
          method: 'POST',
          headers: Object.assign({{ 'content-type': 'application/json' }}, headerToken),
          body: JSON.stringify({{ keys: [ch === '\n' ? 'Enter' : ch] }}),
        }});
        sent++;
      }} catch (err) {{ /* keep going on transient failures */ }}
      await new Promise(r => setTimeout(r, 40));
    }}
    statusStripEl.textContent = 'typed ' + sent + ' chars';
  }});

  // --- Human-input UX (poll the TS server cross-port) ---------------
  // Captcha challenges and other human-input requests are handled by
  // the T1 server's HumanInputManager. The T3 viewer reaches it via
  // T1_BASE+/session/<parent_sid>/human-input. When PARENT_SID is
  // empty (no escalation, or the URL caller didn't pass ?parent=),
  // the human-input UI stays hidden.
  let pendingRequest = null;
  let pendingSeenAt = 0;
  let everSeenPending = false;
  let countdownInterval = null;

  function captchaTypeFromMessage(msg) {{
    if (!msg) return null;
    const m = msg.match(/Auto-solve exhausted for (\w+) captcha/i);
    return m ? m[1] : null;
  }}
  function instructionFor(captchaType) {{
    switch ((captchaType || '').toLowerCase()) {{
      case 'turnstile': return 'Click the Cloudflare checkbox.';
      case 'recaptcha': return 'Click "I\'m not a robot", then any image grid that appears.';
      case 'hcaptcha':  return 'Click the hCaptcha checkbox, then any image grid that appears.';
      case 'slider':    return 'Drag the slider to match the puzzle.';
      case 'image':
      case 'visual_puzzle': return 'Tap the image tiles that match the prompt.';
      default: return 'Solve the challenge shown above.';
    }}
  }}
  function startCountdown(timeoutMs) {{
    const deadline = Date.now() + timeoutMs;
    if (countdownInterval) clearInterval(countdownInterval);
    const tick = () => {{
      const remaining = Math.max(0, deadline - Date.now());
      const m = Math.floor(remaining / 60000);
      const s = Math.floor((remaining % 60000) / 1000);
      countdownEl.textContent = (remaining > 0)
        ? (m + ':' + String(s).padStart(2, '0'))
        : 'expired';
    }};
    tick();
    countdownInterval = setInterval(tick, 1000);
  }}
  function stopCountdown() {{
    if (countdownInterval) {{ clearInterval(countdownInterval); countdownInterval = null; }}
    countdownEl.textContent = '';
  }}

  async function pollHumanInput() {{
    if (!PARENT_SID || !T1_BASE) return;
    try {{
      const r = await fetch(
        T1_BASE + '/session/' + PARENT_SID + '/human-input',
        {{ headers: headerToken }},
      );
      if (!r.ok) return;
      const data = await r.json();
      const newPending = data.pending;
      if (newPending) {{
        pendingRequest = newPending;
        everSeenPending = true;
        const captchaType = captchaTypeFromMessage(newPending.message);
        instructLabel.textContent = captchaType
          ? captchaType.toUpperCase() + ':'
          : 'Action needed:';
        instructMsg.textContent = instructionFor(captchaType);
        instructEl.classList.add('visible');
        // Reveal Done/Stuck buttons. They're hidden by default so a
        // viewer with no human-input flow doesn't show dead buttons.
        doneBtn.classList.remove('hidden');
        stuckBtn.classList.remove('hidden');
        if (!countdownInterval && newPending.id !== pendingSeenAt) {{
          pendingSeenAt = newPending.id;
          startCountdown(5 * 60 * 1000);
        }}
      }} else {{
        if (everSeenPending && pendingRequest) {{
          pendingRequest = null;
          instructEl.classList.remove('visible');
          stopCountdown();
          successOverlay.classList.add('visible');
          doneBtn.classList.add('hidden');
          stuckBtn.classList.add('hidden');
        }}
      }}
    }} catch (err) {{ /* network blip; keep polling */ }}
  }}
  if (PARENT_SID && T1_BASE) {{
    setInterval(pollHumanInput, HUMAN_POLL_MS);
    pollHumanInput();
  }}

  doneBtn.addEventListener('click', async () => {{
    if (!pendingRequest) {{
      statusStripEl.textContent = 'no pending request — already resumed?';
      return;
    }}
    try {{
      const r = await fetch(
        T1_BASE + '/session/' + PARENT_SID + '/human-input',
        {{
          method: 'POST',
          headers: Object.assign({{ 'Content-Type': 'application/json' }}, headerToken),
          body: JSON.stringify({{ id: pendingRequest.id, data: {{ done: 'true' }} }}),
        }},
      );
      if (r.ok) {{
        statusStripEl.textContent = 'marked done; agent will resume';
      }} else {{
        statusStripEl.innerHTML = '<span class="err">done failed: '
          + (await r.text()) + '</span>';
      }}
    }} catch (err) {{
      statusStripEl.innerHTML = '<span class="err">done errored: ' + err + '</span>';
    }}
  }});

  stuckBtn.addEventListener('click', async () => {{
    if (!pendingRequest) {{
      statusStripEl.textContent = 'no pending request to cancel';
      return;
    }}
    try {{
      const r = await fetch(
        T1_BASE + '/session/' + PARENT_SID + '/human-input',
        {{
          method: 'POST',
          headers: Object.assign({{ 'Content-Type': 'application/json' }}, headerToken),
          body: JSON.stringify({{ id: pendingRequest.id, cancelled: true }}),
        }},
      );
      if (r.ok) {{
        statusStripEl.textContent = 'cancelled — agent will try alternative path';
      }} else {{
        statusStripEl.innerHTML = '<span class="err">cancel failed: '
          + (await r.text()) + '</span>';
      }}
    }} catch (err) {{
      statusStripEl.innerHTML = '<span class="err">cancel errored: ' + err + '</span>';
    }}
  }});

  document.title = 'T3 viewer · ' + SESSION_ID.slice(0, 8);
}})();
</script>
</body>
</html>
"""


def _t1_base_for_template() -> str:
    """Public origin of the TS server (T1) for cross-port human-input
    polling from the T3 viewer. Reads SUPERBROWSER_PUBLIC_HOST (set in
    container deployments) and falls back to http://localhost:3100 for
    local dev. Behind a reverse proxy that mounts the T1 server on a
    non-default origin, callers must set SUPERBROWSER_PUBLIC_HOST to
    the externally reachable URL.
    """
    host = os.environ.get("SUPERBROWSER_PUBLIC_HOST", "http://localhost:3100")
    if "://" not in host:
        host = f"http://{host}"
    # Strip trailing slash so JS can concatenate "/session/<sid>/...".
    return host.rstrip("/")


def _check_token(request: web.Request) -> Optional[web.Response]:
    """Token auth mirroring the TS server pattern at
    src/server/websocket.ts:83-100. When TOKEN env is set, every
    request must present `Authorization: Bearer <t>` or `?token=<t>`,
    EXCEPT loopback origins (127.0.0.1, ::1) when
    TOKEN_AUTH_LOOPBACK_BYPASS is not "false". Returns None on success;
    a 401 Response on failure.
    """
    token = os.environ.get("TOKEN")
    if not token:
        return None
    # Loopback bypass: lets local dev / curl / docker host-network
    # callers reach the viewer without juggling the token.
    bypass = os.environ.get("TOKEN_AUTH_LOOPBACK_BYPASS", "true") != "false"
    if bypass:
        peer = request.transport.get_extra_info("peername") if request.transport else None
        peer_host = peer[0] if peer else ""
        if peer_host in ("127.0.0.1", "::1", "::ffff:127.0.0.1") or peer_host.startswith("127."):
            return None
    auth = request.headers.get("Authorization", "")
    if auth == f"Bearer {token}":
        return None
    if request.query.get("token") == token:
        return None
    return web.Response(status=401, text="Unauthorized")


class _Server:
    def __init__(self) -> None:
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._lock = asyncio.Lock()
        self._port = int(os.environ.get("SUPERBROWSER_T3_VIEWER_PORT", "3101"))

    @property
    def port(self) -> int:
        return self._port

    async def ensure_started(self) -> None:
        if self._site is not None:
            return
        async with self._lock:
            if self._site is not None:
                return
            app = web.Application()
            app.router.add_get("/t3/session/{sid}/view", self._view)
            app.router.add_get("/t3/session/{sid}/screenshot", self._screenshot)
            app.router.add_post("/t3/session/{sid}/click", self._click)
            app.router.add_post("/t3/session/{sid}/keys", self._keys)
            app.router.add_get("/t3/session/{sid}/ws", self._ws)
            self._runner = web.AppRunner(app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, "0.0.0.0", self._port)
            try:
                await self._site.start()
                logger.info("t3 viewer listening on :%d", self._port)
            except OSError as exc:
                logger.warning(
                    "t3 viewer port %d busy (%s); falling back to ephemeral",
                    self._port, exc,
                )
                self._site = web.TCPSite(self._runner, "0.0.0.0", 0)
                await self._site.start()

    async def _view(self, request: web.Request) -> web.Response:
        unauth = _check_token(request)
        if unauth is not None:
            return unauth
        sid = request.match_info["sid"]
        # `?parent=<t1_sid>` lets the viewer poll the TS server's
        # human-input endpoint on behalf of the original session that
        # escalated. Empty when the session never had a T1 parent
        # (started directly on T3) — the human-input UI then stays
        # hidden in the JS.
        parent = request.query.get("parent", "") or ""
        token = os.environ.get("TOKEN", "") or ""
        html = _HTML_TEMPLATE.format(
            sid=sid,
            sid_json=json.dumps(sid),
            token_json=json.dumps(token),
            parent_json=json.dumps(parent),
            t1_base_json=json.dumps(_t1_base_for_template()),
        )
        return web.Response(text=html, content_type="text/html")

    async def _screenshot(self, request: web.Request) -> web.Response:
        unauth = _check_token(request)
        if unauth is not None:
            return unauth
        sid = request.match_info["sid"]
        try:
            png = await _t3.default().screenshot(sid)
        except KeyError:
            return web.Response(status=404, text="session not found")
        except Exception as exc:
            return web.Response(status=500, text=f"{type(exc).__name__}: {exc}")
        return web.Response(
            body=png,
            content_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    async def _click(self, request: web.Request) -> web.Response:
        unauth = _check_token(request)
        if unauth is not None:
            return unauth
        sid = request.match_info["sid"]
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        x = float(payload.get("x", 0))
        y = float(payload.get("y", 0))
        try:
            await _t3.default().click_at(sid, x, y)
        except KeyError:
            return web.json_response({"ok": False, "error": "session not found"}, status=404)
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500,
            )
        return web.json_response({"ok": True, "x": x, "y": y})

    async def _keys(self, request: web.Request) -> web.Response:
        """Forward keystrokes from the type modal to the live patchright
        page. Body is `{keys: [str, ...]}` matching the shape
        interactive_session.keys() expects. The type modal sends one
        char per call so each keystroke also fires emit_keystroke for
        the typing indicator.
        """
        unauth = _check_token(request)
        if unauth is not None:
            return unauth
        sid = request.match_info["sid"]
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        keys = payload.get("keys") or []
        if isinstance(keys, str):
            keys = [keys]
        if not isinstance(keys, list):
            return web.json_response(
                {"ok": False, "error": "keys must be a string or list of strings"},
                status=400,
            )
        try:
            result = await _t3.default().keys(sid, [str(k) for k in keys])
        except KeyError:
            return web.json_response({"ok": False, "error": "session not found"}, status=404)
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500,
            )
        return web.json_response({"ok": True, **result})

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        """Subscribe the connecting client to the event bus for this
        session. Pump JSON events as text frames until either side
        disconnects. Heartbeat ping keeps idle connections alive
        through any intermediate proxy.
        """
        # Auth check fires before the upgrade so a 401 response is a
        # plain HTTP reply, not a half-open WS that the client has to
        # interpret. Mirrors websocket.ts:84-100.
        unauth = _check_token(request)
        if unauth is not None:
            return unauth
        sid = request.match_info["sid"]
        ws = web.WebSocketResponse(heartbeat=20.0)
        await ws.prepare(request)
        q = _bus.default().subscribe(sid)
        logger.debug("t3 viewer WS connected: sid=%s", sid)
        # Pump loop: await events from the bus, forward as JSON. Also
        # listen to inbound frames on the same ws so a client close
        # tears the pump down promptly. Two tasks, whichever finishes
        # first wins.
        try:
            async def pump_out() -> None:
                while True:
                    event = await q.get()
                    if ws.closed:
                        return
                    try:
                        await ws.send_str(json.dumps(event))
                    except Exception:
                        return

            async def pump_in() -> None:
                async for msg in ws:
                    if msg.type == WSMsgType.CLOSE:
                        return
                    if msg.type == WSMsgType.ERROR:
                        return

            out_task = asyncio.create_task(pump_out())
            in_task = asyncio.create_task(pump_in())
            done, pending = await asyncio.wait(
                {out_task, in_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            # Await the cancelled tasks so their teardown completes
            # before we close the WS — otherwise tests (and noisy
            # debug logs) see "Task was destroyed but it is pending".
            for task in pending:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        finally:
            _bus.default().unsubscribe(sid, q)
            if not ws.closed:
                await ws.close()
            logger.debug("t3 viewer WS closed: sid=%s", sid)
        return ws


_SERVER: Optional[_Server] = None


def default() -> _Server:
    global _SERVER
    if _SERVER is None:
        _SERVER = _Server()
    return _SERVER


async def ensure_started() -> str:
    """Start the viewer if not already running; return the base URL."""
    srv = default()
    await srv.ensure_started()
    host = os.environ.get("SUPERBROWSER_PUBLIC_HOST", "http://localhost")
    if "://" not in host:
        host = f"http://{host}"
    return f"{host.rstrip('/')}:{srv.port}"


def view_url(session_id: str, *, parent: Optional[str] = None) -> str:
    """Build the public URL of the T3 viewer for `session_id`.

    `parent` (optional) is the T1 session id this T3 session escalated
    from. When set, it's appended as `?parent=<t1_sid>` so the viewer's
    JS knows which session to poll for human-input requests on the
    TS server. Omit `parent` for sessions that started directly on T3.
    """
    srv = default()
    host = os.environ.get("SUPERBROWSER_PUBLIC_HOST", "http://localhost")
    if "://" not in host:
        host = f"http://{host}"
    base = f"{host.rstrip('/')}:{srv.port}/t3/session/{session_id}/view"
    if parent:
        # Use simple concat — session ids are URL-safe (uuid-like) so a
        # full quote() round-trip is overkill.
        return f"{base}?parent={parent}"
    return base
