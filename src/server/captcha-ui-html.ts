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
