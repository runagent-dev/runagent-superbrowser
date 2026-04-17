/**
 * HTML template for the remote captcha-solve UI.
 *
 * Served by `GET /session/:id/view`. A single self-contained, mobile-first
 * page that:
 *   1. Polls GET /session/:id/screenshot at 4 FPS for a live view (cheap
 *      fallback for the not-yet-built CDP screencast WebSocket).
 *   2. Forwards user taps/clicks to POST /session/:id/click {x, y} with
 *      image-pixel → viewport-pixel scaling; shows a transient pulse ring
 *      where the user tapped so they get immediate feedback even while the
 *      next screenshot is in flight.
 *   3. Provides a "Type…" modal that sends characters via /session/:id/keys.
 *   4. Polls GET /session/:id/human-input to show the agent's instruction
 *      and surface the "Done" / "I'm stuck" buttons. Captcha-type prefix
 *      is rendered as an instruction badge so the user sees what they need
 *      to do at a glance.
 *   5. Auto-detects when the captcha clears (no more pending request after
 *      one was active) and shows a success overlay so the user knows they
 *      can return to WhatsApp/CLI.
 *
 * Kept dependency-free (no React, no build step) so changes are diffable
 * and serving needs no bundler wiring.
 */

interface ViewParams {
  sessionId: string;
  token?: string;
}

export function renderCaptchaViewHtml({ sessionId, token }: ViewParams): string {
  const safeId = escapeHtml(sessionId);
  const safeToken = token ? escapeHtml(token) : '';
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#0f1115">
  <title>SuperBrowser — Solve Captcha</title>
  <style>
    :root {
      color-scheme: light dark;
      --bg: #0f1115;
      --panel: #181a20;
      --panel-2: #1f222a;
      --border: #2a2e38;
      --text: #e9ecf2;
      --muted: #8b93a7;
      --accent: #4f8cff;
      --accent-2: #2d6a4f;
      --warn: #ffc857;
      --warn-bg: #3a2a00;
      --err: #ff6b6b;
      --ok: #34d399;
    }
    @media (prefers-color-scheme: light) {
      :root {
        --bg: #f7f8fb; --panel: #ffffff; --panel-2: #f0f2f7;
        --border: #e1e5ee; --text: #11161f; --muted: #5c6577;
        --warn-bg: #fff8e0;
      }
    }
    * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
    html, body { height: 100%; }
    body {
      margin: 0; padding: 0;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
      background: var(--bg); color: var(--text);
      overscroll-behavior: none;
    }
    header {
      display: flex; align-items: center; gap: 8px;
      padding: 10px 14px;
      padding-top: max(10px, env(safe-area-inset-top));
      background: var(--panel); border-bottom: 1px solid var(--border);
      position: sticky; top: 0; z-index: 10;
    }
    header .title {
      font-weight: 600; font-size: 14px;
    }
    header .session-id {
      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
      font-size: 11px; color: var(--muted);
      margin-left: auto;
      max-width: 36vw;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    /* Instruction banner — what to do, how long left */
    #instruct {
      padding: 12px 14px; background: var(--warn-bg); color: var(--warn);
      border-bottom: 1px solid var(--border); display: none;
      font-size: 14px; line-height: 1.4;
    }
    #instruct.visible { display: block; }
    #instruct .label { font-weight: 600; margin-right: 6px; }
    #instruct .countdown { float: right; font-variant-numeric: tabular-nums; opacity: 0.85; }
    #screen-wrap {
      position: relative;
      display: flex; justify-content: center; padding: 12px;
      touch-action: manipulation;
    }
    #screen {
      max-width: 100%; height: auto;
      border: 1px solid var(--border); border-radius: 6px;
      background: #000; cursor: crosshair;
      image-rendering: -webkit-optimize-contrast;
      box-shadow: 0 4px 24px rgba(0,0,0,0.25);
    }
    /* Tap-pulse overlay */
    .pulse {
      position: absolute; pointer-events: none;
      width: 32px; height: 32px;
      margin-left: -16px; margin-top: -16px;
      border-radius: 50%; border: 2px solid var(--accent);
      animation: pulse-anim 0.35s ease-out forwards;
    }
    @keyframes pulse-anim {
      from { transform: scale(0.4); opacity: 1; }
      to { transform: scale(1.6); opacity: 0; }
    }
    /* Action bar */
    .actions {
      display: flex; gap: 8px; padding: 10px 14px;
      padding-bottom: max(10px, env(safe-area-inset-bottom));
      background: var(--panel); border-top: 1px solid var(--border);
      position: sticky; bottom: 0; z-index: 10;
    }
    .actions button {
      flex: 1;
      min-height: 44px; /* iOS touch target */
      padding: 10px 14px; border-radius: 8px; border: 1px solid var(--border);
      background: var(--panel-2); color: var(--text);
      font-size: 15px; font-weight: 500;
      cursor: pointer;
      transition: background 0.12s ease;
    }
    .actions button:active { background: var(--border); }
    .actions button.primary { background: var(--accent-2); border-color: var(--accent-2); color: white; }
    .actions button.primary:active { background: #1f4d39; }
    .actions button.warn { background: transparent; color: var(--muted); }
    #status {
      padding: 4px 14px; font-size: 12px; color: var(--muted);
      min-height: 18px; text-align: center;
    }
    /* Type modal */
    #type-modal {
      display: none;
      position: fixed; top: 0; left: 0; right: 0; bottom: 0;
      background: rgba(0,0,0,0.55); backdrop-filter: blur(4px);
      align-items: flex-end; justify-content: center;
      z-index: 100;
    }
    @media (min-width: 600px) {
      #type-modal { align-items: center; }
    }
    #type-modal.visible { display: flex; }
    #type-modal .inner {
      background: var(--panel); border: 1px solid var(--border);
      border-radius: 14px 14px 0 0; padding: 18px 16px;
      padding-bottom: max(18px, env(safe-area-inset-bottom));
      width: 100%; max-width: 480px;
    }
    @media (min-width: 600px) {
      #type-modal .inner { border-radius: 14px; }
    }
    #type-modal h3 { margin: 0 0 10px; font-size: 16px; }
    #type-modal textarea {
      width: 100%; min-height: 84px;
      background: var(--panel-2); color: var(--text);
      border: 1px solid var(--border); border-radius: 8px; padding: 10px;
      font-family: inherit; font-size: 16px; /* prevents iOS zoom */
      resize: vertical;
    }
    #type-modal .row {
      display: flex; gap: 8px; margin-top: 12px;
    }
    #type-modal .row button {
      flex: 1; min-height: 44px; padding: 10px;
      border-radius: 8px; border: 1px solid var(--border);
      background: var(--panel-2); color: var(--text);
      font-size: 15px; cursor: pointer;
    }
    #type-modal .row button.primary { background: var(--accent); border-color: var(--accent); color: white; }
    /* Success overlay */
    #success {
      display: none;
      position: fixed; inset: 0;
      background: rgba(15,17,21,0.94);
      align-items: center; justify-content: center;
      flex-direction: column; gap: 14px;
      z-index: 200;
      padding: 24px;
    }
    #success.visible { display: flex; }
    #success .check {
      width: 72px; height: 72px; border-radius: 50%;
      background: var(--ok);
      display: flex; align-items: center; justify-content: center;
      font-size: 40px; color: #003322;
    }
    #success .msg { font-size: 17px; text-align: center; max-width: 320px; }
    .err { color: var(--err); }
    .ok { color: var(--ok); }
    /* Click-target crosshair: shown for ~600ms after each click so a
       missed click is visible without diff-ing successive frames. */
    @keyframes click-pulse {
      0%   { opacity: 0; transform: translate(-50%, -50%) scale(0.4); }
      30%  { opacity: 1; transform: translate(-50%, -50%) scale(1); }
      100% { opacity: 0; transform: translate(-50%, -50%) scale(2.0); }
    }
    .click-target {
      position: absolute; pointer-events: none; z-index: 6;
      width: 28px; height: 28px;
      animation: click-pulse 600ms ease-out forwards;
    }
    .click-target.snapped svg circle { stroke: var(--ok); }
    .click-target.miss   svg circle { stroke: var(--warn); }
    /* Bounding-box outline drawn at the resolved click site. Pulses
       once then fades — long enough to register, short enough to clear
       before the next vision pass. */
    @keyframes bbox-pulse {
      0%   { opacity: 0; }
      20%  { opacity: 1; }
      100% { opacity: 0; }
    }
    .click-bbox {
      position: absolute; pointer-events: none; z-index: 5;
      border: 2px solid var(--ok); border-radius: 4px;
      box-shadow: 0 0 0 1px rgba(0,0,0,0.35) inset;
      animation: bbox-pulse 800ms ease-out forwards;
    }
    .click-bbox.miss { border-color: var(--warn); }
    /* Vision-pass bboxes — persistent overlay. All detected
       interactive regions stay visible until the next vision pass
       replaces them, so the user can read labels and correlate click
       accuracy with what the model actually detected. Color-coded by
       role; intent-relevant ones get a thicker border. The .stale class
       dims the layer after ~5s of no new pass so long-idle sessions don't
       keep showing a frozen overlay at full opacity. */
    .vision-bbox {
      position: absolute; pointer-events: none; z-index: 4;
      border: 1.5px dashed rgba(255,255,255,0.55);
      border-radius: 3px;
      opacity: 0.85;
      transition: opacity 300ms ease, filter 300ms ease;
    }
    .vision-bbox.stale {
      opacity: 0.45;
      filter: grayscale(0.4);
    }
    .vision-bbox[data-role="button"]        { border-color: rgba(255,107,107,0.9); }
    .vision-bbox[data-role="link"]          { border-color: rgba(150,206,180,0.9); }
    .vision-bbox[data-role="input"]         { border-color: rgba(78,205,196,0.95); }
    .vision-bbox[data-role="checkbox"]      { border-color: rgba(78,205,196,0.95); }
    .vision-bbox[data-role="captcha_tile"]  { border-color: rgba(255,200,87,0.9); }
    .vision-bbox[data-role="captcha_widget"]{ border-color: rgba(255,200,87,0.9); }
    .vision-bbox[data-role="slider_handle"] { border-color: rgba(255,200,87,0.9); }
    .vision-bbox[data-relevant="1"] { border-width: 3px; }
    .vision-bbox .vlabel {
      position: absolute; top: -16px; left: -1px;
      font: 10px/14px ui-monospace, SFMono-Regular, Consolas, monospace;
      background: rgba(0,0,0,0.7); color: #fff;
      padding: 0 4px; border-radius: 3px 3px 0 0;
      white-space: nowrap; max-width: 180px;
      overflow: hidden; text-overflow: ellipsis;
    }
  </style>
</head>
<body>
  <header>
    <div class="title">SuperBrowser</div>
    <div class="session-id" id="session-id">${safeId}</div>
  </header>

  <div id="instruct">
    <span class="countdown" id="countdown"></span>
    <span class="label" id="instruct-label">Action needed:</span>
    <span id="instruct-msg">waiting…</span>
  </div>

  <div id="screen-wrap">
    <img id="screen" alt="Live browser view" />
    <div id="cursor-overlay" style="display:none; position:absolute; pointer-events:none; z-index:5; transition:left 33ms linear,top 33ms linear;">
      <svg width="20" height="20" viewBox="0 0 20 20">
        <path d="M0,0 L0,16 L4.5,12 L8,19 L10.5,18 L7,11 L12,11 Z"
              fill="rgba(79,140,255,0.9)" stroke="#fff" stroke-width="1"/>
      </svg>
    </div>
  </div>

  <div id="typing-indicator" style="display:none; padding:4px 14px; font-size:13px; color:var(--accent); font-family:ui-monospace,SFMono-Regular,Consolas,monospace; text-align:center;"></div>
  <div id="status"></div>

  <div class="actions">
    <button id="type-btn">Type…</button>
    <button id="stuck-btn" class="warn">I'm stuck</button>
    <button id="done-btn" class="primary">Done</button>
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
    (() => {
      const SESSION_ID = ${JSON.stringify(sessionId)};
      const TOKEN = ${JSON.stringify(safeToken)};

      const REFRESH_MS = 250;  // Fallback polling cadence (4 FPS)
      const HUMAN_POLL_MS = 1200;

      const qsToken = TOKEN ? ('&token=' + encodeURIComponent(TOKEN)) : '';
      const headerToken = TOKEN ? { Authorization: 'Bearer ' + TOKEN } : {};

      const $ = (id) => document.getElementById(id);
      const screen = $('screen');
      const screenWrap = $('screen-wrap');
      const cursorEl = $('cursor-overlay');
      const typingEl = $('typing-indicator');
      const instructEl = $('instruct');
      const instructMsg = $('instruct-msg');
      const instructLabel = $('instruct-label');
      const countdownEl = $('countdown');
      const statusEl = $('status');
      const doneBtn = $('done-btn');
      const stuckBtn = $('stuck-btn');
      const typeBtn = $('type-btn');
      const typeModal = $('type-modal');
      const typeInput = $('type-input');
      const successOverlay = $('success');

      let viewport = { width: 1280, height: 1100 };

      // --- Screenshot refresh (HTTP polling fallback) ----------------
      let pollInterval = null;
      function refresh() {
        screen.src = '/session/' + SESSION_ID + '/screenshot?_=' + Date.now() + qsToken;
      }
      screen.addEventListener('load', () => {
        viewport.width = screen.naturalWidth || viewport.width;
        viewport.height = screen.naturalHeight || viewport.height;
      });
      screen.addEventListener('error', () => {
        statusEl.innerHTML = '<span class="err">screenshot fetch failed (session closed?)</span>';
      });
      // Start polling immediately; WebSocket will stop it once connected.
      pollInterval = setInterval(refresh, REFRESH_MS);
      refresh();

      // --- Blob URL frame setter (avoids data URI overhead at 15 FPS) ---
      let prevBlobUrl = null;
      function setScreenFrame(base64Data) {
        const binary = atob(base64Data);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
        const blob = new Blob([bytes], { type: 'image/jpeg' });
        const url = URL.createObjectURL(blob);
        screen.src = url;
        if (prevBlobUrl) URL.revokeObjectURL(prevBlobUrl);
        prevBlobUrl = url;
      }

      // --- Cursor overlay --------------------------------------------
      function updateCursor(vx, vy) {
        const rect = screen.getBoundingClientRect();
        const wrapRect = screenWrap.getBoundingClientRect();
        const natW = screen.naturalWidth || viewport.width;
        const natH = screen.naturalHeight || viewport.height;
        const px = (vx / natW) * rect.width + (rect.left - wrapRect.left);
        const py = (vy / natH) * rect.height + (rect.top - wrapRect.top);
        cursorEl.style.display = 'block';
        cursorEl.style.left = px + 'px';
        cursorEl.style.top = py + 'px';
      }

      // --- Coord projection ------------------------------------------
      // Map a page CSS pixel (vx, vy) to a position inside #screen-wrap
      // so overlays line up with the rendered <img>. The screen image
      // can be scaled (CSS) and centered (margin) — both are folded in.
      function projectPoint(vx, vy) {
        const rect = screen.getBoundingClientRect();
        const wrapRect = screenWrap.getBoundingClientRect();
        const natW = screen.naturalWidth || viewport.width;
        const natH = screen.naturalHeight || viewport.height;
        const sx = rect.width / natW;
        const sy = rect.height / natH;
        const ox = rect.left - wrapRect.left;
        const oy = rect.top - wrapRect.top;
        return { x: vx * sx + ox, y: vy * sy + oy, sx: sx, sy: sy };
      }

      // --- Click-target crosshair + bbox outline ---------------------
      // Shown briefly after each click so the user can see *where* the
      // cursor landed. Green when we snapped to a real interactive
      // element, amber when we fell back to the raw bbox centre. When
      // the click came from a vision bbox, the bbox rect is drawn at
      // the same time so a miss vs a hit is visible at a glance.
      function showClickTarget(data) {
        if (!data || typeof data.x !== 'number' || typeof data.y !== 'number') return;
        const p = projectPoint(data.x, data.y);
        // Crosshair
        const el = document.createElement('div');
        el.className = 'click-target ' + (data.snapped ? 'snapped' : 'miss');
        el.style.left = p.x + 'px';
        el.style.top = p.y + 'px';
        el.innerHTML = '<svg viewBox="0 0 28 28" width="28" height="28">'
          + '<circle cx="14" cy="14" r="11" fill="none" stroke-width="3"/>'
          + '<line x1="14" y1="2" x2="14" y2="8" stroke="currentColor" stroke-width="2"/>'
          + '<line x1="14" y1="20" x2="14" y2="26" stroke="currentColor" stroke-width="2"/>'
          + '<line x1="2" y1="14" x2="8" y2="14" stroke="currentColor" stroke-width="2"/>'
          + '<line x1="20" y1="14" x2="26" y2="14" stroke="currentColor" stroke-width="2"/>'
          + '</svg>';
        screenWrap.appendChild(el);
        setTimeout(() => { try { el.remove(); } catch (e) {} }, 650);
        // Bbox rect (when present)
        if (data.bbox && typeof data.bbox.x0 === 'number') {
          const tl = projectPoint(data.bbox.x0, data.bbox.y0);
          const br = projectPoint(data.bbox.x1, data.bbox.y1);
          const box = document.createElement('div');
          box.className = 'click-bbox ' + (data.snapped ? '' : 'miss');
          box.style.left = tl.x + 'px';
          box.style.top = tl.y + 'px';
          box.style.width = Math.max(0, br.x - tl.x) + 'px';
          box.style.height = Math.max(0, br.y - tl.y) + 'px';
          screenWrap.appendChild(box);
          setTimeout(() => { try { box.remove(); } catch (e) {} }, 850);
        }
      }

      // --- Vision-pass overlay (persistent) --------------------------
      // Draws the entire bbox set returned by the vision agent and
      // KEEPS it on screen. Only replaced when the next vision_bboxes
      // event arrives. After ~5s of no new event, the .stale class is
      // added so the layer dims — a visual signal that what you're
      // looking at is the last pass, not the current one.
      let visionBboxLayer = null;
      let visionBboxLayerAt = 0;
      const VISION_STALE_AFTER_MS = 5000;

      function showVisionBboxes(data) {
        if (!data || !Array.isArray(data.bboxes)) return;
        // Replace any prior layer so two overlays don't stack.
        if (visionBboxLayer) {
          try { visionBboxLayer.remove(); } catch (e) {}
          visionBboxLayer = null;
        }
        const layer = document.createElement('div');
        layer.style.position = 'absolute';
        layer.style.left = '0';
        layer.style.top = '0';
        layer.style.right = '0';
        layer.style.bottom = '0';
        layer.style.pointerEvents = 'none';
        layer.style.zIndex = '4';
        for (const b of data.bboxes) {
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
          if (b.index || b.label) {
            const lab = document.createElement('span');
            lab.className = 'vlabel';
            const idx = b.index ? '[V' + b.index + '] ' : '';
            lab.textContent = idx + (b.label || b.role || '');
            div.appendChild(lab);
          }
          layer.appendChild(div);
        }
        screenWrap.appendChild(layer);
        visionBboxLayer = layer;
        visionBboxLayerAt = Date.now();
      }

      // Cheap 1 Hz staleness watcher — toggles the .stale class on
      // every bbox in the current layer so CSS fades them together.
      // No-op when no layer is mounted.
      setInterval(function() {
        if (!visionBboxLayer) return;
        const stale = (Date.now() - visionBboxLayerAt) > VISION_STALE_AFTER_MS;
        const children = visionBboxLayer.children || [];
        for (let i = 0; i < children.length; i++) {
          children[i].classList.toggle('stale', stale);
        }
      }, 1000);

      // --- Typing indicator ------------------------------------------
      let typingBuffer = '';
      let typingTimeout = null;
      function showKeystroke(key) {
        if (key === 'Backspace') {
          typingBuffer = typingBuffer.slice(0, -1);
        } else if (key === 'Enter') {
          typingBuffer += '\\u23CE';
        } else if (key.length === 1) {
          typingBuffer += key;
        }
        if (typingBuffer.length > 40) typingBuffer = '...' + typingBuffer.slice(-37);
        typingEl.textContent = 'typing: ' + typingBuffer;
        typingEl.style.display = 'block';
        clearTimeout(typingTimeout);
        typingTimeout = setTimeout(() => {
          typingEl.style.display = 'none';
          typingBuffer = '';
        }, 2000);
      }

      // --- WebSocket streaming (upgrades from polling when available) --
      let ws = null;
      let wsConnected = false;

      function connectWebSocket() {
        try {
          const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
          const wsUrl = proto + '//' + location.host + '/ws/session/' + SESSION_ID
            + (TOKEN ? '?token=' + encodeURIComponent(TOKEN) : '');
          ws = new WebSocket(wsUrl);

          ws.onopen = () => {
            wsConnected = true;
            statusEl.textContent = 'streaming live';
            // Stop HTTP polling — WS screencast is now the frame source.
            if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
          };

          ws.onmessage = (evt) => {
            try {
              const msg = JSON.parse(evt.data);
              switch (msg.event) {
                case 'screencast_frame':
                  setScreenFrame(msg.data.data);
                  break;
                case 'cursor_move':
                  updateCursor(msg.data.x, msg.data.y);
                  break;
                case 'cursor_target':
                  showClickTarget(msg.data);
                  break;
                case 'vision_bboxes':
                  showVisionBboxes(msg.data);
                  break;
                case 'keystroke':
                  showKeystroke(msg.data.key);
                  break;
                case 'feedback':
                  // Could handle captcha/error events here in the future
                  break;
              }
            } catch {}
          };

          ws.onclose = () => {
            wsConnected = false;
            cursorEl.style.display = 'none';
            // Fall back to HTTP polling
            if (!pollInterval) {
              pollInterval = setInterval(refresh, REFRESH_MS);
              refresh();
            }
            statusEl.textContent = 'reconnecting...';
            setTimeout(connectWebSocket, 2000);
          };

          ws.onerror = () => { ws.close(); };
        } catch {
          // WebSocket not supported or blocked — polling continues.
        }
      }
      connectWebSocket();

      // --- Visibility API: pause/resume screencast -------------------
      document.addEventListener('visibilitychange', () => {
        if (!ws || ws.readyState !== WebSocket.OPEN) return;
        if (document.hidden) {
          ws.send(JSON.stringify({ action: 'screencast_pause' }));
        } else {
          ws.send(JSON.stringify({ action: 'screencast_resume' }));
          // Immediate refresh in case we missed frames while hidden.
          if (!pollInterval) refresh();
        }
      });

      // --- Tap → click forwarding with pulse feedback -----------------
      function showPulse(clientX, clientY) {
        const wrapRect = screenWrap.getBoundingClientRect();
        const pulse = document.createElement('div');
        pulse.className = 'pulse';
        pulse.style.left = (clientX - wrapRect.left) + 'px';
        pulse.style.top = (clientY - wrapRect.top) + 'px';
        screenWrap.appendChild(pulse);
        setTimeout(() => pulse.remove(), 380);
      }
      async function forwardClick(clientX, clientY) {
        showPulse(clientX, clientY);
        const rect = screen.getBoundingClientRect();
        const natW = screen.naturalWidth || viewport.width;
        const natH = screen.naturalHeight || viewport.height;
        const x = Math.round((clientX - rect.left) * (natW / rect.width));
        const y = Math.round((clientY - rect.top) * (natH / rect.height));
        try {
          const r = await fetch('/session/' + SESSION_ID + '/click', {
            method: 'POST',
            headers: Object.assign({ 'Content-Type': 'application/json' }, headerToken),
            body: JSON.stringify({ x, y }),
          });
          statusEl.textContent = r.ok
            ? 'tapped (' + x + ', ' + y + ')'
            : 'tap failed: ' + (await r.text());
        } catch (err) {
          statusEl.textContent = 'tap errored: ' + err;
        }
      }
      // Use pointerup so a single handler covers mouse + touch + pen.
      screen.addEventListener('pointerup', (e) => {
        e.preventDefault();
        forwardClick(e.clientX, e.clientY);
      });

      // --- Type modal ------------------------------------------------
      typeBtn.addEventListener('click', () => {
        typeModal.classList.add('visible');
        setTimeout(() => typeInput.focus(), 50);
      });
      $('type-cancel').addEventListener('click', () => {
        typeModal.classList.remove('visible');
        typeInput.value = '';
      });
      $('type-send').addEventListener('click', async () => {
        const text = typeInput.value;
        typeModal.classList.remove('visible');
        typeInput.value = '';
        if (!text) return;
        for (const ch of text) {
          try {
            await fetch('/session/' + SESSION_ID + '/keys', {
              method: 'POST',
              headers: Object.assign({ 'Content-Type': 'application/json' }, headerToken),
              body: JSON.stringify({ keys: ch === '\\n' ? 'Enter' : ch }),
            });
          } catch {}
          await new Promise(r => setTimeout(r, 40));
        }
        statusEl.textContent = 'typed ' + text.length + ' chars';
      });

      // --- Human-input poll (instruction banner + countdown) ----------
      let pendingRequest = null;
      let pendingSeenAt = 0;
      let everSeenPending = false;

      function captchaTypeFromMessage(msg) {
        if (!msg) return null;
        const m = msg.match(/Auto-solve exhausted for (\\w+) captcha/i);
        return m ? m[1] : null;
      }
      function instructionFor(captchaType) {
        switch ((captchaType || '').toLowerCase()) {
          case 'turnstile': return 'Click the Cloudflare checkbox.';
          case 'recaptcha': return 'Click "I\\'m not a robot", then any image grid that appears.';
          case 'hcaptcha': return 'Click the hCaptcha checkbox, then any image grid that appears.';
          case 'slider': return 'Drag the slider to match the puzzle.';
          case 'image':
          case 'visual_puzzle': return 'Tap the image tiles that match the prompt.';
          default: return 'Solve the challenge shown above.';
        }
      }

      let countdownInterval = null;
      function startCountdown(timeoutMs) {
        const deadline = Date.now() + timeoutMs;
        if (countdownInterval) clearInterval(countdownInterval);
        const tick = () => {
          const remaining = Math.max(0, deadline - Date.now());
          const m = Math.floor(remaining / 60000);
          const s = Math.floor((remaining % 60000) / 1000);
          countdownEl.textContent = (remaining > 0)
            ? (m + ':' + String(s).padStart(2, '0'))
            : 'expired';
        };
        tick();
        countdownInterval = setInterval(tick, 1000);
      }
      function stopCountdown() {
        if (countdownInterval) { clearInterval(countdownInterval); countdownInterval = null; }
        countdownEl.textContent = '';
      }

      async function pollHumanInput() {
        try {
          const r = await fetch('/session/' + SESSION_ID + '/human-input', { headers: headerToken });
          if (!r.ok) return;
          const data = await r.json();
          const newPending = data.pending;
          if (newPending) {
            pendingRequest = newPending;
            everSeenPending = true;
            const captchaType = captchaTypeFromMessage(newPending.message);
            instructLabel.textContent = captchaType
              ? captchaType.toUpperCase() + ':'
              : 'Action needed:';
            instructMsg.textContent = instructionFor(captchaType);
            instructEl.classList.add('visible');
            // The agent surfaces a 5-min timeout; show it counting down.
            if (!countdownInterval && newPending.id !== pendingSeenAt) {
              pendingSeenAt = newPending.id;
              startCountdown(5 * 60 * 1000);
            }
          } else {
            // Pending cleared. If we ever saw a request, this means the
            // agent has resumed — show success overlay.
            if (everSeenPending && pendingRequest) {
              pendingRequest = null;
              instructEl.classList.remove('visible');
              stopCountdown();
              successOverlay.classList.add('visible');
            }
          }
        } catch {}
      }
      setInterval(pollHumanInput, HUMAN_POLL_MS);
      pollHumanInput();

      // --- Done button: positively confirm the user solved it --------
      doneBtn.addEventListener('click', async () => {
        if (!pendingRequest) {
          statusEl.textContent = 'no pending request — already resumed?';
          return;
        }
        try {
          const r = await fetch('/session/' + SESSION_ID + '/human-input', {
            method: 'POST',
            headers: Object.assign({ 'Content-Type': 'application/json' }, headerToken),
            body: JSON.stringify({ id: pendingRequest.id, data: { done: 'true' } }),
          });
          if (r.ok) {
            statusEl.textContent = 'marked done; agent will resume';
            // Optimistic: success overlay will show on next poll when the
            // server confirms pending cleared.
          } else {
            statusEl.innerHTML = '<span class="err">done failed: ' + (await r.text()) + '</span>';
          }
        } catch (err) {
          statusEl.innerHTML = '<span class="err">done errored: ' + err + '</span>';
        }
      });

      // --- Stuck button: cancel so the agent can try another path ----
      stuckBtn.addEventListener('click', async () => {
        if (!pendingRequest) {
          statusEl.textContent = 'no pending request to cancel';
          return;
        }
        try {
          const r = await fetch('/session/' + SESSION_ID + '/human-input', {
            method: 'POST',
            headers: Object.assign({ 'Content-Type': 'application/json' }, headerToken),
            body: JSON.stringify({ id: pendingRequest.id, cancelled: true }),
          });
          if (r.ok) {
            statusEl.textContent = 'cancelled — agent will try alternative path';
          } else {
            statusEl.innerHTML = '<span class="err">cancel failed: ' + (await r.text()) + '</span>';
          }
        } catch (err) {
          statusEl.innerHTML = '<span class="err">cancel errored: ' + err + '</span>';
        }
      });

      document.title = 'SuperBrowser — ' + SESSION_ID.slice(0, 8);
    })();
  </script>
</body>
</html>`;
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
