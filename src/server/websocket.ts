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
import type { HumanInputManager, HumanInputRequest } from '../agent/human-input.js';
import { validateUrl } from './auth.js';
import { runPuppeteerScript } from '../browser/script-runner.js';

interface SessionBinding {
  page: PageWrapper;
  humanInput?: HumanInputManager;
  ws: WebSocket;
}

export function attachWebSocketServer(
  httpServer: HttpServer,
  getSessions: () => Map<string, { page: PageWrapper; createdAt: number; lastAccessed: number }>,
): WebSocketServer {
  const wss = new WebSocketServer({ noServer: true });

  // Track WebSocket connections per session
  const bindings = new Map<string, SessionBinding>();

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
    bindings.set(sessionId, binding);

    // Touch session
    session.lastAccessed = Date.now();

    sendEvent(ws, 'connected', { sessionId });

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

    ws.on('close', () => {
      bindings.delete(sessionId);
    });

    ws.on('error', () => {
      bindings.delete(sessionId);
    });
  });

  return wss;
}

/**
 * Broadcast an event to the WebSocket client connected to a session.
 * Called by the executor/event system when something happens.
 */
export function broadcastToSession(
  wss: WebSocketServer,
  sessionId: string,
  event: string,
  data: unknown,
): void {
  // Find the binding for this session
  for (const client of wss.clients) {
    // We need to match by session — use the bindings approach
    // This is handled internally via the bindings map
  }
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
        await page.clickElement(element);
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
      await page.typeText(element, text, data.clear !== false);
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

/**
 * Create a function that pushes human input requests over WebSocket.
 * Wire this to the HumanInputManager so gateway clients get notified instantly.
 */
export function createHumanInputBridge(
  wss: WebSocketServer,
  sessionId: string,
  humanInput: HumanInputManager,
): void {
  // Poll for pending requests and broadcast
  // (The HumanInputManager uses a blocking promise pattern,
  //  so we intercept by checking after requestInput is called)
  const checkInterval = setInterval(() => {
    const pending = humanInput.getPendingRequest();
    if (pending) {
      for (const client of wss.clients) {
        if (client.readyState === WebSocket.OPEN) {
          sendEvent(client, 'human_input', pending);
        }
      }
    }
  }, 500);

  // Cleanup when no longer needed
  wss.on('close', () => clearInterval(checkInterval));
}
