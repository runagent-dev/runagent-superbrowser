/**
 * WebSocket server for real-time session control.
 *
 * Enables bidirectional communication between SuperBrowser and external
 * gateways (OpenClaw, PicoClaw, ZeroClaw, or any WebSocket client).
 *
 * Connection: ws://superbrowser:3100/ws/session/:id
 *
 * Server → Client (push events):
 *   { event: "state",          data: { screenshot, elements, url, title, ... } }
 *   { event: "human_input",    data: { id, type, message, screenshot, fields, options } }
 *   { event: "step",           data: { step, maxSteps, details } }
 *   { event: "task_complete",  data: { success, finalAnswer, error } }
 *   { event: "captcha",        data: { type, siteKey } }
 *   { event: "error",          data: { message } }
 *   { event: "connected",      data: { sessionId } }
 *
 * Client → Server (commands):
 *   { action: "navigate",     data: { url } }
 *   { action: "click",        data: { index?, x?, y? } }
 *   { action: "type",         data: { index, text, clear? } }
 *   { action: "keys",         data: { keys } }
 *   { action: "scroll",       data: { direction?, percent? } }
 *   { action: "select",       data: { index, value } }
 *   { action: "evaluate",     data: { script } }
 *   { action: "script",       data: { code, context?, timeout? } }
 *   { action: "screenshot" }
 *   { action: "state" }
 *   { action: "markdown" }
 *   { action: "human_input",  data: { id, data: { field: value, ... }, cancelled? } }
 *   { action: "dialog",       data: { accept, text? } }
 *   { action: "close" }
 */

import { WebSocketServer, WebSocket } from 'ws';
import type { Server as HttpServer } from 'http';
import type { PageWrapper } from '../browser/page.js';
import type { HumanInputManager } from '../agent/human-input.js';
import { validateUrl } from './auth.js';
import { runPuppeteerScript } from '../browser/script-runner.js';
import { detectCaptcha, solveCaptchaFull } from '../browser/captcha.js';
import { feedbackBus, type FeedbackEvent } from '../agent/feedback-bus.js';

interface SessionBinding {
  page: PageWrapper;
  humanInput?: HumanInputManager;
  ws: WebSocket;
}

// Track WebSocket connections per session at module scope so
// `broadcastToSession` (exported for the HTTP layer) can look up the
// right client without iterating every open socket.
const bindings = new Map<string, Set<SessionBinding>>();

export function attachWebSocketServer(
  httpServer: HttpServer,
  getSessions: () => Map<string, { page: PageWrapper; createdAt: number; lastAccessed: number }>,
): WebSocketServer {
  const wss = new WebSocketServer({ noServer: true });

  // Handle HTTP upgrade to WebSocket
  httpServer.on('upgrade', (req, socket, head) => {
    const url = new URL(req.url || '', `http://${req.headers.host}`);
    const match = url.pathname.match(/^\/ws\/session\/(.+)$/);

    if (!match) {
      socket.destroy();
      return;
    }

    // Token auth on upgrade
    const token = process.env.TOKEN;
    if (token) {
      const authHeader = req.headers.authorization;
      const queryToken = url.searchParams.get('token');
      if (authHeader !== `Bearer ${token}` && queryToken !== token) {
        socket.write('HTTP/1.1 401 Unauthorized\r\n\r\n');
        socket.destroy();
        return;
      }
    }

    const sessionId = match[1];

    wss.handleUpgrade(req, socket, head, (ws) => {
      wss.emit('connection', ws, req, sessionId);
    });
  });

  // Handle new WebSocket connections
  wss.on('connection', (ws: WebSocket, _req: unknown, sessionId: string) => {
    const sessions = getSessions();
    const session = sessions.get(sessionId);

    if (!session) {
      sendEvent(ws, 'error', { message: 'Session not found' });
      ws.close(1008, 'Session not found');
      return;
    }

    const binding: SessionBinding = { page: session.page, ws };
    const sessionSet = bindings.get(sessionId) ?? new Set<SessionBinding>();
    sessionSet.add(binding);
    bindings.set(sessionId, sessionSet);

    // Touch session
    session.lastAccessed = Date.now();

    sendEvent(ws, 'connected', { sessionId });
    // Replay current feedback state on connect so a late subscriber (e.g.
    // a WhatsApp bot reconnecting mid-handoff) catches up on in-flight
    // captcha / error / awaiting_human state.
    const snap = feedbackBus.getState();
    if (snap.captchaActive) {
      sendEvent(ws, 'feedback', {
        kind: 'captcha_active',
        host: '(snapshot)',
        strategy: snap.captchaStrategy ?? 'unknown',
      });
    }
    if (snap.awaitingHuman) {
      sendEvent(ws, 'feedback', {
        kind: 'awaiting_human',
        detail: snap.awaitingHuman,
      });
    }
    if (snap.errorPage) {
      sendEvent(ws, 'feedback', { kind: 'error_page', detail: snap.errorPage });
    }

    // Subscribe this WS to the process-wide feedback bus. Every event
    // becomes a `feedback` WS message — the Python bridge mirrors state
    // into a local dict it consults before dispatching tools.
    const onFeedback = (event: FeedbackEvent) => {
      sendEvent(ws, 'feedback', event);
    };
    feedbackBus.on('event', onFeedback);

    // Handle incoming commands
    ws.on('message', async (raw) => {
      try {
        const msg = JSON.parse(raw.toString());
        session.lastAccessed = Date.now();
        await handleCommand(binding, sessionId, msg);
      } catch (err) {
        const errMsg = err instanceof Error ? err.message : String(err);
        sendEvent(ws, 'error', { message: errMsg });
      }
    });

    const cleanup = () => {
      const set = bindings.get(sessionId);
      if (set) {
        set.delete(binding);
        if (set.size === 0) bindings.delete(sessionId);
      }
      feedbackBus.off('event', onFeedback);
    };
    ws.on('close', cleanup);
    ws.on('error', cleanup);
  });

  return wss;
}

/**
 * Broadcast an event to every WebSocket client subscribed to a session.
 * Uses the module-scoped `bindings` map so the lookup is O(1) and no other
 * session's clients see the message.
 */
export function broadcastToSession(
  _wss: WebSocketServer,
  sessionId: string,
  event: string,
  data: unknown,
): void {
  const set = bindings.get(sessionId);
  if (!set) return;
  for (const b of set) sendEvent(b.ws, event, data);
}

// --- Internal helpers ---

function sendEvent(ws: WebSocket, event: string, data: unknown): void {
  if (ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ event, data, timestamp: Date.now() }));
  }
}

async function sendStateEvent(ws: WebSocket, page: PageWrapper): Promise<void> {
  const state = await page.getState({ useVision: true, includeConsole: true });
  sendEvent(ws, 'state', {
    url: state.url,
    title: state.title,
    screenshot: state.screenshot,
    elements: state.elementTree.clickableElementsToString(),
    scrollInfo: {
      scrollY: state.scrollY,
      scrollHeight: state.scrollHeight,
      viewportHeight: state.viewportHeight,
    },
    consoleErrors: state.consoleErrors,
    pendingDialogs: state.pendingDialogs,
  });
}

async function handleCommand(
  binding: SessionBinding,
  sessionId: string,
  msg: { action: string; data?: Record<string, unknown> },
): Promise<void> {
  const { page, ws } = binding;
  const data = msg.data || {};

  switch (msg.action) {
    case 'navigate': {
      const url = data.url as string;
      if (!url) { sendEvent(ws, 'error', { message: 'url required' }); return; }
      const check = validateUrl(url);
      if (!check.valid) { sendEvent(ws, 'error', { message: check.error }); return; }
      await page.navigate(url);
      await sendStateEvent(ws, page);
      break;
    }

    case 'click': {
      if (data.x !== undefined && data.y !== undefined) {
        await page.clickAt(data.x as number, data.y as number, {
          button: data.button as 'left' | 'right' | 'middle' | undefined,
        });
      } else if (data.index !== undefined) {
        const state = await page.getState({ useVision: false });
        const element = state.selectorMap.get(data.index as number);
        if (!element) { sendEvent(ws, 'error', { message: `Element [${data.index}] not found` }); return; }
        const r = await page.clickElement(element);
        if (!r.success) {
          sendEvent(ws, 'error', {
            message: r.error ?? 'click failed',
            reason: r.reason,
            tried: r.tried,
            alternatives: r.alternatives,
          });
          return;
        }
      } else {
        sendEvent(ws, 'error', { message: 'index or x,y required' });
        return;
      }
      await sendStateEvent(ws, page);
      break;
    }

    case 'type': {
      const index = data.index as number;
      const text = data.text as string;
      if (index === undefined || !text) { sendEvent(ws, 'error', { message: 'index and text required' }); return; }
      const state = await page.getState({ useVision: false });
      const element = state.selectorMap.get(index);
      if (!element) { sendEvent(ws, 'error', { message: `Element [${index}] not found` }); return; }
      const r = await page.typeText(element, text, data.clear !== false);
      if (!r.success) {
        sendEvent(ws, 'error', {
          message: r.error ?? 'type failed',
          reason: r.reason,
          tried: r.tried,
          alternatives: r.alternatives,
        });
        return;
      }
      await sendStateEvent(ws, page);
      break;
    }

    case 'keys': {
      const keys = data.keys as string;
      if (!keys) { sendEvent(ws, 'error', { message: 'keys required' }); return; }
      await page.sendKeys(keys);
      await new Promise((r) => setTimeout(r, 500));
      await sendStateEvent(ws, page);
      break;
    }

    case 'scroll': {
      if (data.percent !== undefined) {
        await page.scrollToPercent(data.percent as number);
      } else {
        await page.scrollPage((data.direction as 'up' | 'down') || 'down');
      }
      await sendStateEvent(ws, page);
      break;
    }

    case 'select': {
      const index = data.index as number;
      const value = data.value as string;
      const state = await page.getState({ useVision: false });
      const element = state.selectorMap.get(index);
      if (!element) { sendEvent(ws, 'error', { message: `Element [${index}] not found` }); return; }
      await page.selectOption(element, value);
      await sendStateEvent(ws, page);
      break;
    }

    case 'evaluate': {
      const script = data.script as string;
      if (!script) { sendEvent(ws, 'error', { message: 'script required' }); return; }
      const result = await page.evaluateScript(script);
      sendEvent(ws, 'eval_result', { result });
      break;
    }

    case 'script': {
      if (!process.env.TOKEN) {
        sendEvent(ws, 'error', { message: 'script command requires TOKEN to be set' });
        return;
      }
      const code = data.code as string;
      if (!code) { sendEvent(ws, 'error', { message: 'code required' }); return; }
      const rawPage = page.getRawPage();
      const scriptResult = await runPuppeteerScript(
        rawPage,
        code,
        data.context as Record<string, unknown> | undefined,
        data.timeout as number | undefined,
      );
      sendEvent(ws, 'script_result', scriptResult);
      break;
    }

    case 'solve_captcha': {
      const rawPage = page.getRawPage();
      const captcha = await detectCaptcha(rawPage);
      if (!captcha) {
        sendEvent(ws, 'error', { message: 'No captcha detected' });
        return;
      }
      const solveConfig = {
        provider: (data.provider as string) || process.env.CAPTCHA_PROVIDER,
        apiKey: (data.apiKey as string) || process.env.CAPTCHA_API_KEY,
        timeout: (data.timeout as number) || 60000,
      };
      const solveResult = await solveCaptchaFull(
        rawPage,
        captcha,
        solveConfig,
        null, // LLM provider not available via WebSocket currently
        (data.method as string) || 'auto',
      );
      sendEvent(ws, 'captcha_solved', { ...solveResult, captcha });
      break;
    }

    case 'screenshot': {
      await sendStateEvent(ws, page);
      break;
    }

    case 'state': {
      await sendStateEvent(ws, page);
      break;
    }

    case 'markdown': {
      const content = await page.getMarkdownContent();
      sendEvent(ws, 'markdown', { content });
      break;
    }

    case 'dialog': {
      await page.handleDialog(data.accept as boolean, data.text as string | undefined);
      sendEvent(ws, 'dialog_handled', { accept: data.accept });
      break;
    }

    case 'human_input': {
      // Response to a human input request from the executor
      if (binding.humanInput && data.id) {
        binding.humanInput.provideInput({
          id: data.id as string,
          data: (data.data || {}) as Record<string, string>,
          cancelled: data.cancelled as boolean | undefined,
        });
        sendEvent(ws, 'human_input_received', { id: data.id });
      } else {
        sendEvent(ws, 'error', { message: 'No pending human input request' });
      }
      break;
    }

    case 'close': {
      await page.close();
      sendEvent(ws, 'closed', { sessionId });
      ws.close(1000, 'Session closed');
      break;
    }

    default:
      sendEvent(ws, 'error', { message: `Unknown action: ${msg.action}` });
  }
}

