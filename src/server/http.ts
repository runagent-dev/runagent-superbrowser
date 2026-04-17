/**
 * HTTP API server for direct browser control.
 *
 * Full browserless-style API with all page setup options,
 * request interception, wait conditions, scraping, and function execution.
 */

import express from 'express';
import crypto from 'crypto';
import { Stream } from 'stream';
import type { BrowserEngine } from '../browser/engine.js';
import type { PageWrapper } from '../browser/page.js';
import type { LLMProvider } from '../llm/provider.js';
import { BrowserExecutor } from '../agent/executor.js';
import { goto, type NavigationOptions } from '../browser/goto.js';
import { scrapeElements, setupScrapeDebug, type ScrapeElementSelector } from '../browser/scraper.js';
import { Limiter } from '../browser/limiter.js';
import { BrowserError } from '../browser/errors.js';
import { detectCaptcha, solveWithExternalApi, waitForCaptchaSolution, screenshotCaptchaArea, solveCaptchaFull } from '../browser/captcha.js';
import { captchaWatchdog } from '../browser/captcha-watchdog.js';
import { verifyCaptchaSolve, captureJpegB64 } from '../agent/judge.js';
import { tokenAuth, validateUrl, isValidSessionId, RateLimiter } from './auth.js';
import { runPuppeteerScript } from '../browser/script-runner.js';
import { ProxyPool } from '../browser/proxy-pool.js';
import { HumanInputManager, type HumanInputType } from '../agent/human-input.js';
import { renderCaptchaViewHtml } from './captcha-ui-html.js';
import { feedbackBus } from '../agent/feedback-bus.js';
import { fingerprintMap, invertFingerprintMap, fingerprintElement } from '../browser/fingerprint.js';
import { getDomainStats, hostKey } from '../browser/captcha/domain-stats.js';
import { loadDomainCookies } from '../browser/captcha/cookie-jar.js';
import { coordBand } from '../agent/step-observation.js';

/** Session with TTL tracking. */
interface ManagedSession {
  page: PageWrapper;
  createdAt: number;
  lastAccessed: number;
}

const SESSION_IDLE_TIMEOUT = 30 * 60 * 1000;   // 30 minutes idle
const SESSION_MAX_LIFETIME = 2 * 60 * 60 * 1000; // 2 hours max

export function createHttpServer(
  engine: BrowserEngine,
  llm: LLMProvider | null,
  limiterConfig?: { maxConcurrent?: number; maxQueued?: number; defaultTimeout?: number },
): express.Application {
  const app = express();
  app.use(express.json({ limit: '5mb' }));

  const proxyPool = new ProxyPool(engine, {
    headless: process.env.HEADLESS !== 'false',
    downloadDir: process.env.DOWNLOAD_DIR || '/tmp/superbrowser/downloads',
    executablePath: process.env.PUPPETEER_EXECUTABLE_PATH || undefined,
  });

  const limiter = new Limiter(limiterConfig);
  // Default lifted to 600/min — a single agent run with vision + action
  // emits easily 3-5 requests/sec. 200 was sized for a standalone HTTP
  // client, not the nanobot bridge. Loopback traffic bypasses entirely
  // (see RateLimiter.isLoopback), so this ceiling only matters for
  // external clients.
  const rateLimiter = new RateLimiter(
    parseInt(process.env.RATE_LIMIT || '600', 10),
    60000,
  );

  // Security middleware
  app.use(tokenAuth);
  app.use(rateLimiter.middleware());

  // CORS support (from browserless config)
  app.use((_req, res, next) => {
    if (process.env.CORS === 'true' || process.env.ENABLE_CORS === 'true') {
      const allowOrigin = process.env.CORS_ALLOW_ORIGIN || '';
      if (allowOrigin) {
        res.header('Access-Control-Allow-Origin', allowOrigin);
      }
      res.header('Access-Control-Allow-Methods', process.env.CORS_ALLOW_METHODS || 'OPTIONS, POST, GET, DELETE');
      res.header('Access-Control-Allow-Headers', process.env.CORS_ALLOW_HEADERS || 'Content-Type, Authorization');
      if (process.env.CORS_ALLOW_CREDENTIALS === 'true') {
        res.header('Access-Control-Allow-Credentials', 'true');
      }
    }
    next();
  });

  // --- Health & Metrics ---

  app.get('/health', (_req, res) => {
    res.json({
      status: 'ok',
      browser: engine.isRunning(),
      sessions: limiter.runningCount,
      queued: limiter.queuedCount,
      metrics: limiter.getMetrics(),
    });
  });

  app.get('/metrics', (_req, res) => {
    res.json(limiter.getMetrics());
  });

  // --- Feedback state ---
  // Python bridge polls this before dispatching a browser tool to check
  // whether a captcha is mid-solve (in which case the tool should be
  // deferred) or whether the page is stuck on an error state.
  app.get('/feedback', (_req, res) => {
    res.json(feedbackBus.getState());
  });

  // --- Browser Task (agentic) ---
  // Gated: this endpoint runs a full Navigator+Planner LLM loop (10-20+ vision calls).
  // When nanobot is the brain, use session APIs instead. Enable only for standalone use.

  app.post('/task', async (req, res) => {
    if (!process.env.ENABLE_TASK_ENDPOINT) {
      res.status(403).json({
        error: 'The /task endpoint is disabled. It runs a separate LLM loop that duplicates cost. '
             + 'Use session APIs (/session/create, /session/:id/click, etc.) with nanobot as the brain. '
             + 'Set ENABLE_TASK_ENDPOINT=true to override.',
      });
      return;
    }

    const { task, url, options } = req.body;
    if (!task) {
      res.status(400).json({ error: 'task is required' });
      return;
    }

    if (!llm) {
      res.status(400).json({ error: '/task requires LLM config (OPENAI_API_KEY or ANTHROPIC_API_KEY). Use session APIs instead when nanobot is the brain.' });
      return;
    }

    try {
      await limiter.submit(async () => {
        const page = await engine.newPage();
        if (url) await page.navigate(url);

        const executor = new BrowserExecutor(page, llm!, options);
        const result = await executor.executeTask(task);

        await page.close();
        res.json(result);
      }, req.body.timeout);
    } catch (err) {
      handleError(res, err);
    }
  });

  // --- Screenshot (browserless-compatible) ---

  app.post('/screenshot', async (req, res) => {
    const body = req.body as NavigationOptions & {
      options?: { type?: string; quality?: number; fullPage?: boolean; encoding?: string };
      selector?: string;
    };

    if (!body.url && !body.html) {
      res.status(400).json({ error: 'url or html is required' });
      return;
    }

    try {
      await limiter.submit(async () => {
        const page = await engine.newPage();
        const rawPage = page.getRawPage();

        // Navigate with full options
        const navResult = await goto(rawPage, body);

        // Set response headers with navigation metadata
        res.set('X-Response-Code', String(navResult.statusCode));
        res.set('X-Response-URL', navResult.url);

        // Determine screenshot target
        const screenshotOptions = body.options || {};
        let target: typeof rawPage | Awaited<ReturnType<typeof rawPage.$>> = rawPage;
        if (body.selector) {
          const el = await rawPage.$(body.selector);
          if (el) target = el;
        }

        // Take screenshot
        const buffer = await (target as any).screenshot({
          type: screenshotOptions.type || 'jpeg',
          quality: screenshotOptions.quality || 75,
          fullPage: screenshotOptions.fullPage || false,
          encoding: 'binary',
        });

        await page.close();

        // Stream binary response
        const contentType = screenshotOptions.type === 'png' ? 'image/png' : 'image/jpeg';
        res.set('Content-Type', contentType);
        const stream = new Stream.PassThrough();
        stream.end(buffer);
        stream.pipe(res);
      }, body.gotoOptions?.timeout);
    } catch (err) {
      handleError(res, err);
    }
  });

  // --- PDF (browserless-compatible) ---

  app.post('/pdf', async (req, res) => {
    const body = req.body as NavigationOptions & {
      options?: Record<string, unknown>;
    };

    if (!body.url && !body.html) {
      res.status(400).json({ error: 'url or html is required' });
      return;
    }

    try {
      await limiter.submit(async () => {
        const page = await engine.newPage();
        const rawPage = page.getRawPage();

        await goto(rawPage, body);

        // Handle full-page PDF with auto-height calculation
        const pdfOptions = body.options || {};
        if (pdfOptions.fullPage) {
          const height = await rawPage.evaluate(() =>
            Math.max(
              document.body.scrollHeight,
              document.body.offsetHeight,
              document.documentElement.clientHeight,
              document.documentElement.scrollHeight,
              document.documentElement.offsetHeight,
            ),
          );
          pdfOptions.height = `${height}px`;
          delete pdfOptions.format;
          delete pdfOptions.fullPage;
        }

        const buffer = await rawPage.pdf(pdfOptions as Parameters<typeof rawPage.pdf>[0]);
        await page.close();

        res.set('Content-Type', 'application/pdf');
        const stream = new Stream.PassThrough();
        stream.end(buffer);
        stream.pipe(res);
      }, body.gotoOptions?.timeout);
    } catch (err) {
      handleError(res, err);
    }
  });

  // --- Content (rendered HTML) ---

  app.post('/content', async (req, res) => {
    const body = req.body as NavigationOptions;

    if (!body.url && !body.html) {
      res.status(400).json({ error: 'url or html is required' });
      return;
    }

    try {
      await limiter.submit(async () => {
        const page = await engine.newPage();
        const rawPage = page.getRawPage();

        const navResult = await goto(rawPage, body);
        const content = await rawPage.content();
        await page.close();

        res.set('Content-Type', 'text/html');
        res.set('X-Response-Code', String(navResult.statusCode));
        res.set('X-Response-URL', navResult.url);
        res.send(content);
      }, body.gotoOptions?.timeout);
    } catch (err) {
      handleError(res, err);
    }
  });

  // --- Scrape (elements + debug data) ---

  app.post('/scrape', async (req, res) => {
    const body = req.body as NavigationOptions & {
      elements: ScrapeElementSelector[];
      debugOpt?: {
        console?: boolean;
        network?: boolean;
        cookies?: boolean;
        html?: boolean;
        screenshot?: boolean;
      };
    };

    if (!body.url && !body.html) {
      res.status(400).json({ error: 'url or html is required' });
      return;
    }
    if (!body.elements || body.elements.length === 0) {
      res.status(400).json({ error: 'elements array is required' });
      return;
    }

    try {
      await limiter.submit(async () => {
        const page = await engine.newPage();
        const rawPage = page.getRawPage();

        // Setup debug capture before navigation
        const debug = body.debugOpt ? setupScrapeDebug(rawPage) : null;

        await goto(rawPage, body);

        // Scrape elements
        const data = await scrapeElements(rawPage, body.elements, body.bestAttempt);

        // Collect debug info
        const debugInfo = debug ? await debug.getDebugInfo() : undefined;

        await page.close();

        res.json({ data, debug: debugInfo });
      }, body.gotoOptions?.timeout);
    } catch (err) {
      handleError(res, err);
    }
  });

  // --- Function execution (run puppeteer code — requires TOKEN auth) ---

  app.post('/function', async (req, res) => {
    // Double-check auth — this endpoint is dangerous
    if (!process.env.TOKEN) {
      res.status(403).json({ error: '/function endpoint requires TOKEN to be set for security' });
      return;
    }

    const { code, context, url } = req.body;
    if (!code || typeof code !== 'string') {
      res.status(400).json({ error: 'code (string) is required' });
      return;
    }
    if (code.length > 50000) {
      res.status(400).json({ error: 'code exceeds 50KB limit' });
      return;
    }

    // Validate URL if provided
    if (url) {
      const urlCheck = validateUrl(url);
      if (!urlCheck.valid) {
        res.status(403).json({ error: urlCheck.error });
        return;
      }
    }

    try {
      await limiter.submit(async () => {
        const page = await engine.newPage();
        const rawPage = page.getRawPage();

        if (url) {
          await rawPage.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
        }

        // Execute Puppeteer code with full page API access (goto, click, type, etc.)
        const scriptResult = await runPuppeteerScript(rawPage, code, context, req.body.timeout);

        await page.close();

        res.set('X-Script-Duration', String(scriptResult.duration));
        res.json(scriptResult);
      }, req.body.timeout);
    } catch (err) {
      handleError(res, err);
    }
  });

  // --- Navigate and return page state (agentic) ---

  app.post('/state', async (req, res) => {
    const body = req.body as NavigationOptions;

    if (!body.url && !body.html) {
      res.status(400).json({ error: 'url or html is required' });
      return;
    }

    try {
      const page = await engine.newPage();
      const rawPage = page.getRawPage();
      await goto(rawPage, body);
      const state = await page.getState({ useVision: false });
      await page.close();

      res.json({
        url: state.url,
        title: state.title,
        elements: state.elementTree.clickableElementsToString(),
        scrollY: state.scrollY,
        scrollHeight: state.scrollHeight,
        viewportHeight: state.viewportHeight,
      });
    } catch (err) {
      handleError(res, err);
    }
  });

  // ==========================================================================
  // SESSION-BASED APIs — persistent page for step-by-step control
  //
  // This is what enables the "Claude Code + browserless" workflow:
  //   1. Create session → get session ID
  //   2. Navigate → get screenshot + DOM state
  //   3. Click/type/scroll → get screenshot + DOM state
  //   4. Repeat until done
  //   5. Close session
  //
  // The nanobot super agent sees every screenshot and decides next steps.
  // ==========================================================================

  const sessions = new Map<string, ManagedSession>();
  const MAX_SESSIONS = parseInt(process.env.MAX_SESSIONS || '20', 10);

  // Per-session human-in-the-loop input manager. One per Puppeteer session.
  // Populated on /session/create, cleaned up on close/expire. Exposed to the
  // captcha orchestrator (via /session/:id/captcha/solve) and to the Python
  // browser_ask_user tool (via the endpoints below).
  const sessionHumanInput = new Map<string, HumanInputManager>();

  // Per-session human-handoff budget. Default 1 per session so the agent
  // can't mechanical-turk the user repeatedly. Override via env at server
  // start; per-session override via enableHumanHandoff on /session/create.
  const DEFAULT_HUMAN_HANDOFF_BUDGET = parseInt(
    process.env.SUPERBROWSER_MAX_HUMAN_HANDOFFS || '1',
    10,
  );
  const sessionHandoffBudget = new Map<string, number>();

  /** Clean up human-input state for a session id. Idempotent. */
  function disposeSessionHumanInput(id: string): void {
    const mgr = sessionHumanInput.get(id);
    if (mgr) {
      try { mgr.cancel(); } catch { /* ignore */ }
      sessionHumanInput.delete(id);
    }
    sessionHandoffBudget.delete(id);
  }

  // Session cleanup loop — expire idle and old sessions
  setInterval(() => {
    const now = Date.now();
    for (const [id, session] of sessions) {
      const idle = now - session.lastAccessed > SESSION_IDLE_TIMEOUT;
      const expired = now - session.createdAt > SESSION_MAX_LIFETIME;
      if (idle || expired) {
        session.page.close().catch(() => {});
        sessions.delete(id);
        disposeSessionHumanInput(id);
      }
    }
  }, 60000);

  /** Get session with access tracking. */
  function getSession(id: string): PageWrapper | null {
    const session = sessions.get(id);
    if (!session) return null;
    session.lastAccessed = Date.now();
    return session.page;
  }

  /** Create a new persistent session. Accepts optional proxy/region for geo-restricted sites. */
  app.post('/session/create', async (req, res) => {
    if (sessions.size >= MAX_SESSIONS) {
      res.status(429).json({ error: `Max sessions (${MAX_SESSIONS}) reached` });
      return;
    }

    try {
      // Support proxy/region selection for geo-restricted sites
      const page = await proxyPool.newPage({
        proxy: req.body.proxy,
        region: req.body.region,
        url: req.body.url,
      });
      await page.setupDialogHandler();
      await page.enableConsoleCapture();

      const id = `session-${crypto.randomUUID().split('-')[0]}`;
      page.sessionId = id;
      sessions.set(id, { page, createdAt: Date.now(), lastAccessed: Date.now() });

      // Per-session human-input manager + handoff budget.
      sessionHumanInput.set(id, new HumanInputManager());
      const handoffBudget = req.body.enableHumanHandoff
        ? (typeof req.body.humanHandoffBudget === 'number'
            ? Math.max(0, Math.floor(req.body.humanHandoffBudget))
            : DEFAULT_HUMAN_HANDOFF_BUDGET)
        : 0;
      sessionHandoffBudget.set(id, handoffBudget);

      let navStatusCode: number | null = null;
      if (req.body.url) {
        // Restore any previously persisted bot-protection cookies for this
        // (task, domain) BEFORE the first navigation so the site sees a
        // "verified" request on load and we skip the captcha entirely.
        // No-op when SUPERBROWSER_COOKIE_JAR!=1 or no entry exists.
        try {
          const restored = await loadDomainCookies(page.getRawPage(), req.body.url);
          if (restored > 0) {
            process.stderr.write(
              `[cookie-jar] restored ${restored} cookie(s) for ${hostKey(req.body.url)} ` +
              `on session ${id}\n`,
            );
          }
        } catch { /* best-effort */ }
        const nav = await page.navigate(req.body.url);
        navStatusCode = nav.statusCode;
      }

      const wantVision = req.body.vision !== false;
      const state = await page.getState({ useVision: wantVision, includeConsole: true });

      res.json({
        sessionId: id,
        url: state.url,
        title: state.title,
        statusCode: navStatusCode,
        ...(wantVision && state.screenshot ? { screenshot: state.screenshot } : {}),
        elements: state.elementTree.clickableElementsToString(),
        scrollInfo: { scrollY: state.scrollY, scrollHeight: state.scrollHeight, viewportHeight: state.viewportHeight },
      });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Navigate within a session. Returns screenshot + state. */
  app.post('/session/:id/navigate', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    const { url } = req.body;
    if (!url || typeof url !== 'string') {
      res.status(400).json({ error: 'url is required' });
      return;
    }
    const urlCheck = validateUrl(url);
    if (!urlCheck.valid) {
      res.status(403).json({ error: urlCheck.error });
      return;
    }

    try {
      // If the navigation crosses into a domain we previously cleared with
      // a human, restore those bot-protection cookies first so the site
      // recognizes us on the way in.
      try {
        const restored = await loadDomainCookies(page.getRawPage(), url);
        if (restored > 0) {
          process.stderr.write(
            `[cookie-jar] restored ${restored} cookie(s) for ${hostKey(url)} ` +
            `on session ${req.params.id} (navigate)\n`,
          );
        }
      } catch { /* best-effort */ }
      const nav = await page.navigate(url);
      const wantVision = req.body.vision !== false;
      const state = await page.getState({ useVision: wantVision, includeConsole: true });

      res.json({
        url: state.url,
        title: state.title,
        statusCode: nav.statusCode,
        ...(wantVision && state.screenshot ? { screenshot: state.screenshot } : {}),
        elements: state.elementTree.clickableElementsToString(),
        scrollInfo: { scrollY: state.scrollY, scrollHeight: state.scrollHeight, viewportHeight: state.viewportHeight },
        consoleErrors: state.consoleErrors,
        pendingDialogs: state.pendingDialogs,
      });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Take a screenshot of the current session state. */
  app.get('/session/:id/screenshot', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    try {
      const buffer = await page.screenshot();
      res.set('Content-Type', 'image/jpeg');
      res.send(buffer);
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Get current state (DOM tree + screenshot). */
  app.get('/session/:id/state', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    try {
      const useVision = req.query.vision !== 'false';
      const includeBounds = req.query.bounds === 'true' || req.query.highlight === 'true';
      const state = await page.getState({
        useVision,
        includeConsole: true,
        includeAccessibility: req.query.accessibility === 'true',
        includeCursorElements: req.query.cursor === 'true',
      });

      // Fetch device pixel ratio so the client can scale CSS bounds to
      // device pixels when drawing bbox overlays on the screenshot.
      let devicePixelRatio = 1;
      if (includeBounds) {
        try {
          devicePixelRatio = await page.getRawPage().evaluate(() => window.devicePixelRatio || 1);
        } catch {
          devicePixelRatio = 1;
        }
      }

      res.json({
        url: state.url,
        title: state.title,
        screenshot: useVision ? state.screenshot : undefined,
        elements: state.elementTree.clickableElementsToString(),
        scrollInfo: { scrollY: state.scrollY, scrollHeight: state.scrollHeight, viewportHeight: state.viewportHeight },
        accessibilityTree: state.accessibilityTree,
        consoleErrors: state.consoleErrors,
        pendingDialogs: state.pendingDialogs,
        // Optional bbox overlay payload — only included when requested to
        // keep the normal /state response compact.
        selectorEntries: includeBounds ? state.selectorEntries : undefined,
        devicePixelRatio: includeBounds ? devicePixelRatio : undefined,
        // Per-index identity fingerprint. Used by the Python bridge to
        // catch stale-index clicks when the DOM re-renders between
        // state-fetch and click. Small payload (~16 hex chars × N).
        fingerprints: fingerprintMap(state.selectorMap),
      });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Click an element by index. Returns updated screenshot + state. */
  app.post('/session/:id/click', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    try {
      const { index, x, y, button, clickCount, expected_fingerprint } = req.body as {
        index?: number;
        x?: number;
        y?: number;
        button?: 'left' | 'right' | 'middle';
        clickCount?: number;
        /** Fingerprint the LLM was targeting (from the last state fetch).
         *  When provided and index is used, we reject if current[index]
         *  has a different fingerprint — prevents clicking a different
         *  element than the LLM intended after a DOM re-render. */
        expected_fingerprint?: string;
      };

      if (x !== undefined && y !== undefined) {
        // Reward-band guard: if previous clicks in this 100px cell have
        // reliably produced nothing (mean reward < 0.1 with ≥3 samples),
        // reject instead of letting the LLM scatter-click the same dead
        // zone. Hint at higher-scoring bands when we have them.
        try {
          const host = hostKey(await page.getUrl());
          const stats = getDomainStats();
          const band = coordBand(x, y);
          const bandStats = stats.actionBandStats(host, 'navigator', band);
          if (bandStats && bandStats.n >= 3 && bandStats.mean < 0.1) {
            const top = stats.topActionBands(host, 'navigator', 3)
              .filter((b) => b.band !== band && b.mean >= 0.3);
            const suggestion = top.length > 0
              ? ` Historically effective zones on this page: ${top.map((b) => `band ${b.band} (mean=${b.mean.toFixed(2)})`).join(', ')}.`
              : '';
            res.status(409).json({
              error: `click_at (${x}, ${y}) lands in a dead zone (mean reward ${bandStats.mean.toFixed(2)} over ${bandStats.n} prior clicks).${suggestion} Re-read elements and pick by [index] instead.`,
              reason: 'low_reward_band',
              band,
              stats: bandStats,
              suggestions: top,
            });
            return;
          }
        } catch {
          // Band lookup is best-effort — never block the click on a
          // stats read failure.
        }
        await page.clickAt(x, y, { button, clickCount });
      } else if (index !== undefined) {
        const state = await page.getState({ useVision: false });
        const element = state.selectorMap.get(index);
        if (!element) { res.status(400).json({ error: `Element [${index}] not found` }); return; }

        // Stale-index guard: the LLM saw screenshot+elements at time T1;
        // if the DOM has since shifted, current[index] may point at a
        // different element. Compare fingerprints and reject with a
        // structured hint naming the new index of the intended element.
        if (expected_fingerprint) {
          const currentFp = fingerprintElement(element);
          if (currentFp !== expected_fingerprint) {
            const currentFpMap = fingerprintMap(state.selectorMap);
            const suggestedIndex = invertFingerprintMap(currentFpMap)[expected_fingerprint];
            res.status(409).json({
              error: `Element [${index}] has shifted since last state — the element you intended is ${suggestedIndex !== undefined ? `now at [${suggestedIndex}]` : 'no longer on the page'}.`,
              reason: 'stale_index',
              stale_index: index,
              suggested_index: suggestedIndex,
              // Echo current mapping so the caller can re-pick rather
              // than re-fetching state.
              current_element: `<${element.tagName.toLowerCase()}>${element.getAllTextTillNextClickableElement(2).slice(0, 60)}`,
            });
            return;
          }
        }

        const r = await page.clickElement(element, { button, clickCount });
        if (!r.success) {
          res.status(400).json({
            error: r.error ?? 'click failed',
            reason: r.reason,
            tried: r.tried,
            alternatives: r.alternatives,
          });
          return;
        }
      } else {
        res.status(400).json({ error: 'index or x,y required' });
        return;
      }

      // Return updated state — no screenshot (nanobot discards it anyway, saves processing time)
      const newState = await page.getState({ useVision: false, includeConsole: true });
      res.json({
        success: true,
        url: newState.url,
        title: newState.title,
        elements: newState.elementTree.clickableElementsToString(),
        consoleErrors: newState.consoleErrors,
        pendingDialogs: newState.pendingDialogs,
      });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Type text into an element. Returns updated state. */
  app.post('/session/:id/type', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    try {
      const { index, text, clear, expected_fingerprint } = req.body as {
        index?: number; text?: string; clear?: boolean; expected_fingerprint?: string;
      };
      if (index === undefined || !text) {
        res.status(400).json({ error: 'index and text required' });
        return;
      }

      const state = await page.getState({ useVision: false });
      const element = state.selectorMap.get(index);
      if (!element) { res.status(400).json({ error: `Element [${index}] not found` }); return; }

      if (expected_fingerprint) {
        const currentFp = fingerprintElement(element);
        if (currentFp !== expected_fingerprint) {
          const currentFpMap = fingerprintMap(state.selectorMap);
          const suggestedIndex = invertFingerprintMap(currentFpMap)[expected_fingerprint];
          res.status(409).json({
            error: `Element [${index}] has shifted since last state — retarget to ${suggestedIndex !== undefined ? `[${suggestedIndex}]` : 'a different index'}.`,
            reason: 'stale_index',
            stale_index: index,
            suggested_index: suggestedIndex,
            current_element: `<${element.tagName.toLowerCase()}>${element.getAllTextTillNextClickableElement(2).slice(0, 60)}`,
          });
          return;
        }
      }

      const r = await page.typeText(element, text, clear !== false);
      if (!r.success) {
        res.status(400).json({
          error: r.error ?? 'type failed',
          reason: r.reason,
          tried: r.tried,
          alternatives: r.alternatives,
        });
        return;
      }

      const newState = await page.getState({ useVision: false, includeConsole: true });
      res.json({
        success: true,
        elements: newState.elementTree.clickableElementsToString(),
      });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Send keyboard keys. Returns updated state. */
  app.post('/session/:id/keys', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    try {
      await page.sendKeys(req.body.keys);
      await new Promise((r) => setTimeout(r, 500));

      const newState = await page.getState({ useVision: false });
      res.json({
        success: true,
        elements: newState.elementTree.clickableElementsToString(),
      });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Scroll the page. Returns updated state. */
  app.post('/session/:id/scroll', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    try {
      const { direction, percent } = req.body;
      if (percent !== undefined) {
        await page.scrollToPercent(percent);
      } else {
        await page.scrollPage(direction || 'down');
      }

      const newState = await page.getState({ useVision: false });
      res.json({
        success: true,
        elements: newState.elementTree.clickableElementsToString(),
        scrollInfo: { scrollY: newState.scrollY, scrollHeight: newState.scrollHeight, viewportHeight: newState.viewportHeight },
      });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Drag from one point to another. Useful for slider CAPTCHAs and puzzle pieces. */
  app.post('/session/:id/drag', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    try {
      const { startX, startY, endX, endY, steps } = req.body;
      if (startX === undefined || startY === undefined || endX === undefined || endY === undefined) {
        res.status(400).json({ error: 'startX, startY, endX, endY are required' });
        return;
      }

      await page.dragTo(startX, startY, endX, endY, { steps: steps || 25 });
      await new Promise((r) => setTimeout(r, 500));

      const newState = await page.getState({ useVision: false });
      res.json({
        success: true,
        url: newState.url,
        title: newState.title,
        elements: newState.elementTree.clickableElementsToString(),
      });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Execute JavaScript in the session page. */
  app.post('/session/:id/evaluate', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    try {
      const result = await page.evaluateScript(req.body.script);
      res.json({ result });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Execute a Puppeteer script with full page API access in a session. */
  app.post('/session/:id/script', async (req, res) => {
    // Require TOKEN — this runs arbitrary Node.js code
    if (!process.env.TOKEN) {
      res.status(403).json({ error: '/session/:id/script requires TOKEN to be set for security' });
      return;
    }

    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    const { code, context, timeout } = req.body;
    if (!code || typeof code !== 'string') {
      res.status(400).json({ error: 'code (string) is required' });
      return;
    }
    if (code.length > 50000) {
      res.status(400).json({ error: 'code exceeds 50KB limit' });
      return;
    }

    try {
      const rawPage = page.getRawPage();
      const scriptResult = await runPuppeteerScript(rawPage, code, context, timeout);
      res.set('X-Script-Duration', String(scriptResult.duration));
      res.json(scriptResult);
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Handle a pending dialog. */
  app.post('/session/:id/dialog', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    try {
      await page.handleDialog(req.body.accept, req.body.text);
      res.json({ success: true });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Select a dropdown option. */
  app.post('/session/:id/select', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    try {
      const { index, value } = req.body;
      const state = await page.getState({ useVision: false });
      const element = state.selectorMap.get(index);
      if (!element) { res.status(400).json({ error: `Element [${index}] not found` }); return; }
      await page.selectOption(element, value);

      const newState = await page.getState({ useVision: false });
      res.json({ success: true, elements: newState.elementTree.clickableElementsToString() });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Extract page content as markdown. */
  app.get('/session/:id/markdown', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    try {
      const markdown = await page.getMarkdownContent();
      res.json({ content: markdown });
    } catch (err) {
      handleError(res, err);
    }
  });

  /**
   * Stealth-only "render a URL and return markdown" — the search worker's
   * fallback when plain `web_fetch` returns a bot-block stub.
   *
   * Deliberately does NOT attempt captcha auto-solve: if a captcha survives
   * the stealth fingerprint (Phase 3.7), we return { blocked: true } so the
   * search worker can move on to the next source URL rather than burning
   * solver budget inside a research loop. For pages that truly need captcha
   * solving, the orchestrator should delegate to `delegate_browser_task`.
   *
   * Lightweight: opens a new page on the shared browser, navigates, extracts
   * markdown, closes. No screenshots, no LLM calls. ~3s per call.
   */
  app.post('/fetch/rendered', async (req, res) => {
    const url: string | undefined = req.body?.url;
    const timeout: number = Math.min(Math.max(Number(req.body?.timeout) || 20000, 5000), 45000);
    if (!url || typeof url !== 'string') {
      res.status(400).json({ error: 'body must include { url: string }' });
      return;
    }
    const urlCheck = validateUrl(url);
    if (!urlCheck.valid) {
      res.status(400).json({ error: `Blocked URL: ${urlCheck.error}` });
      return;
    }

    let page: PageWrapper | null = null;
    try {
      page = await engine.newPage();
      await page.navigate(url, timeout);
      const finalUrl = page.getRawPage().url();

      // Quick captcha check — stealth-only path bails rather than solving.
      const captcha = await detectCaptcha(page.getRawPage()).catch(() => null);
      if (captcha && !captcha.solved) {
        const title = await page.getRawPage().title().catch(() => '');
        res.json({
          url,
          finalUrl,
          blocked: true,
          reason: `captcha_after_stealth: ${captcha.type}`,
          title,
          markdown: '',
        });
        return;
      }

      const markdown = await page.getMarkdownContent();
      const title = await page.getRawPage().title().catch(() => '');

      // Also flag obviously-thin pages so the caller can treat them as blocked
      // without a second heuristic pass — length gate is the simplest signal.
      const blocked = markdown.trim().length < 200;
      res.json({
        url,
        finalUrl,
        title,
        markdown: markdown.slice(0, 200_000),
        blocked,
        reason: blocked ? 'content_too_thin' : undefined,
      });
    } catch (err) {
      // Navigation/timeout errors surface as 'blocked' rather than 500 so the
      // search worker can adapt without the orchestrator seeing a hard failure.
      const message = err instanceof Error ? err.message : String(err);
      res.json({
        url,
        blocked: true,
        reason: `fetch_error: ${message}`,
        markdown: '',
      });
    } finally {
      // Always close the short-lived page. Keeps browser footprint minimal.
      if (page) {
        try { await page.getRawPage().close(); } catch { /* best-effort */ }
      }
    }
  });

  /** Export current page as PDF. */
  app.get('/session/:id/pdf', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    try {
      const buffer = await page.exportPdf();
      res.set('Content-Type', 'application/pdf');
      res.send(buffer);
    } catch (err) {
      handleError(res, err);
    }
  });

  // ============================================================
  // Human-in-the-loop endpoints — per-session.
  //
  // - GET  /session/:id/human-input       → returns the pending HumanInputRequest
  //                                          (or null). Used by the remote view
  //                                          UI to poll for "anything needed?"
  // - POST /session/:id/human-input       → user's reply; body is a
  //                                          HumanInputResponse { id, data, cancelled? }
  //                                          Releases the waiting requestInput() caller.
  // - POST /session/:id/human-input/ask   → blocking ask from the SERVER side.
  //                                          Body: { type, message, options?, fields?, screenshot?, timeout? }
  //                                          Returns once the user replies (or timeout).
  //                                          This is what Python browser_ask_user calls.
  // ============================================================

  /**
   * Remote view UI. Returns a self-contained HTML page that polls the
   * screenshot endpoint at 2 FPS and forwards user clicks + typing back
   * to the live Puppeteer session.
   *
   * Auth: the view page is bare HTML with no secrets of its own; the
   * token (if TOKEN is configured) is injected as a JS constant and
   * forwarded on every fetch the page makes. Pass ?token=<t> on the URL
   * when loading the page.
   */
  app.get('/session/:id/view', (req, res) => {
    const sessionId = req.params.id;
    if (!sessions.has(sessionId)) {
      res.status(404).type('text/plain').send('Session not found or expired');
      return;
    }
    const token = (req.query.token as string | undefined)
      || (req.header('authorization')?.replace(/^Bearer\s+/i, ''))
      || undefined;
    res.type('text/html').send(renderCaptchaViewHtml({ sessionId, token }));
  });

  app.get('/session/:id/human-input', (req, res) => {
    const mgr = sessionHumanInput.get(req.params.id);
    if (!mgr) { res.status(404).json({ error: 'Session not found or expired' }); return; }
    res.json({ pending: mgr.getPendingRequest() });
  });

  app.post('/session/:id/human-input', (req, res) => {
    const mgr = sessionHumanInput.get(req.params.id);
    if (!mgr) { res.status(404).json({ error: 'Session not found or expired' }); return; }
    const { id, data, cancelled } = req.body || {};
    if (typeof id !== 'string') {
      res.status(400).json({ error: 'id (string) is required' });
      return;
    }
    const delivered = mgr.provideInput({
      id,
      data: data && typeof data === 'object' ? data : {},
      cancelled: cancelled === true,
    });
    if (!delivered) {
      res.status(409).json({ error: 'no pending request, or id does not match' });
      return;
    }
    res.json({ success: true });
  });

  /**
   * Blocking "ask the user" endpoint. Opens a requestInput on the server,
   * holds the HTTP connection until the user replies via POST /human-input
   * (typically from the view UI) or the timeout fires.
   *
   * This gives Python (and other external callers) a single RPC:
   * body → wait → response, without them having to juggle polling.
   */
  app.post('/session/:id/human-input/ask', async (req, res) => {
    const mgr = sessionHumanInput.get(req.params.id);
    if (!mgr) { res.status(404).json({ error: 'Session not found or expired' }); return; }
    const {
      type,
      message,
      options,
      fields,
      screenshot,
      timeout,
    } = req.body || {};
    const validTypes: HumanInputType[] = [
      'credentials', 'captcha', 'confirmation', 'otp', 'card', 'text', 'choice',
    ];
    if (typeof type !== 'string' || !validTypes.includes(type as HumanInputType)) {
      res.status(400).json({ error: `type must be one of ${validTypes.join(', ')}` });
      return;
    }
    if (typeof message !== 'string' || !message.trim()) {
      res.status(400).json({ error: 'message is required' });
      return;
    }
    if (mgr.hasPending) {
      res.status(409).json({ error: 'another human-input request is already pending on this session' });
      return;
    }
    try {
      const response = await mgr.requestInput(type as HumanInputType, message, {
        options: Array.isArray(options) ? options : undefined,
        fields: Array.isArray(fields) ? fields : undefined,
        screenshot: typeof screenshot === 'string' ? screenshot : undefined,
        timeout: typeof timeout === 'number' ? timeout : undefined,
      });
      if (!response) {
        res.status(200).json({ timedOut: true });
        return;
      }
      res.json({ response });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Close a session. */
  app.delete('/session/:id', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    try {
      await page.close();
      sessions.delete(req.params.id);
      disposeSessionHumanInput(req.params.id);
      res.json({ success: true });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Detect captcha on the page. */
  app.get('/session/:id/captcha/detect', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    try {
      const captcha = await detectCaptcha(page.getRawPage());
      // Surface the live-view URL the moment a captcha is detected, not
      // only when handoff fires. Downstream callers (nanobot, WhatsApp
      // bot, CLI logs) can forward it immediately instead of waiting for
      // auto-solve attempts to chew through the strategy ladder.
      const publicHost = process.env.SUPERBROWSER_PUBLIC_HOST;
      const viewUrl = captcha && publicHost
        ? `${publicHost.replace(/\/$/, '')}/session/${req.params.id}/view`
        : undefined;
      res.json({ captcha, viewUrl });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Screenshot captcha area for vision-based solving. */
  app.get('/session/:id/captcha/screenshot', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    try {
      const result = await screenshotCaptchaArea(page.getRawPage());
      if (!result) {
        res.status(404).json({ error: 'No captcha area found' });
        return;
      }
      res.set('Content-Type', 'image/jpeg');
      res.send(result.screenshot);
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Solve captcha with multiple strategies (token, AI vision, 2captcha grid). */
  app.post('/session/:id/captcha/solve', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }
    const sessionId = req.params.id;

    try {
      const captcha = await detectCaptcha(page.getRawPage());
      if (!captcha) {
        res.json({ solved: false, error: 'No captcha detected' });
        return;
      }

      const config = {
        provider: req.body.provider || process.env.CAPTCHA_PROVIDER,
        apiKey: req.body.apiKey || process.env.CAPTCHA_API_KEY,
        timeout: req.body.timeout || 60000,
      };

      const method = req.body.method || 'auto';

      // Snapshot BEFORE — judge uses this for a before/after comparison if
      // the solver later claims success. Cheap (~25KB JPEG) and skipped if
      // we have no LLM to run the judge against.
      const beforeB64 = llm ? await captureJpegB64(page.getRawPage()).catch(() => undefined) : undefined;

      // Notify watchdog so concurrent tool calls (e.g., the agent firing
      // browser_click while we're still solving) block instead of racing.
      captchaWatchdog.notifySolveStarted(sessionId, config.timeout);
      let result;
      // Plumb session-scoped human-in-the-loop state into the solver. If
      // the session was not created with enableHumanHandoff, humanInput
      // is still passed (the strategy self-gates on budget>0) but the
      // budget is 0 so the strategy declines cleanly.
      const humanInput = sessionHumanInput.get(sessionId);
      const handoffBudget = sessionHandoffBudget.get(sessionId) ?? 0;
      try {
        result = await solveCaptchaFull(
          page.getRawPage(),
          captcha,
          config,
          llm,
          method,
          {
            sessionId,
            humanInput,
            humanHandoffBudget: handoffBudget,
            publicBaseUrl: process.env.SUPERBROWSER_PUBLIC_HOST,
          },
        );
      } finally {
        // If the solver succeeded via human_handoff, decrement the budget.
        if ((result as any)?.method === 'human_handoff' && (result as any)?.solved) {
          sessionHandoffBudget.set(sessionId, Math.max(0, handoffBudget - 1));
        }
        // Always unblock waiters, even on exception.
        captchaWatchdog.notifySolveFinished(sessionId, {
          outcome: result?.solved ? 'success' : 'failed',
          vendor: (result as any)?.vendorDetected,
          detail: (result as any)?.subMethod || result?.error,
        });
      }

      // Judge verification (Phase 5.4) — only on reported-success events.
      // If the independent verifier disagrees, flip solved=false and tag
      // the result so the caller knows to retry or ask for human help.
      let judgeVerdict: Awaited<ReturnType<typeof verifyCaptchaSolve>> | undefined;
      if (result.solved && llm && req.body.judge !== false) {
        try {
          const afterB64 = await captureJpegB64(page.getRawPage());
          judgeVerdict = await verifyCaptchaSolve(llm, {
            claim: `${(result as any).method || 'auto'}/${(result as any).subMethod || ''} claimed success`,
            beforeB64,
            afterB64,
            captchaType: captcha.type,
            url: page.getRawPage().url(),
          });
          if (!judgeVerdict.verdict) {
            result = {
              ...result,
              solved: false,
              error: `judge disagreed: ${judgeVerdict.reasoning}`,
            };
          }
        } catch (e) {
          // Judge failure is non-fatal — keep the solver's verdict.
          judgeVerdict = {
            verdict: result.solved,
            reasoning: `judge errored: ${(e as Error).message}`,
          };
        }
      }

      res.json({ ...result, captcha, judge: judgeVerdict });
    } catch (err) {
      captchaWatchdog.notifySolveFinished(sessionId, {
        outcome: 'failed',
        detail: err instanceof Error ? err.message : String(err),
      });
      handleError(res, err);
    }
  });

  /** Poll whether a captcha solve is in progress (for nanobot tool gating). */
  app.get('/session/:id/captcha/waiting', (req, res) => {
    res.json({ solving: captchaWatchdog.isSolving(req.params.id) });
  });

  // The /session/:id/captcha/solve-visual endpoint was retired along with
  // `solveWithVisionGeneric`. Screenshot-driven captcha solves now run in
  // the Python vision agent, invoked via the browser_solve_captcha tool
  // with method='vision' — see nanobot/vision_agent/ and
  // nanobot/superbrowser_bridge/session_tools.py::_solve_via_vision.

  // --- Human-in-the-loop for /task endpoint ---

  const taskExecutors = new Map<string, import('../agent/executor.js').BrowserExecutor>();

  /** Check if a running task needs human input. */
  app.get('/task/:taskId/human-input', (req, res) => {
    const executor = taskExecutors.get(req.params.taskId);
    if (!executor) {
      res.status(404).json({ error: 'Task not found' });
      return;
    }
    const pending = executor.getPendingHumanInput();
    res.json({ pending });
  });

  /** Get step-by-step execution history for a task. */
  app.get('/task/:taskId/history', (req, res) => {
    const executor = taskExecutors.get(req.params.taskId);
    if (!executor) {
      res.status(404).json({ error: 'Task not found' });
      return;
    }
    res.json({ history: executor.getStepRecords() });
  });

  /** Provide human input to a running task. */
  app.post('/task/:taskId/human-input', (req, res) => {
    const executor = taskExecutors.get(req.params.taskId);
    if (!executor) {
      res.status(404).json({ error: 'Task not found' });
      return;
    }
    const accepted = executor.provideHumanInput(req.body);
    res.json({ accepted });
  });

  /** Detect geo-block on current page and suggest a proxy region. */
  app.get('/session/:id/geo-block', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    try {
      const rawPage = page.getRawPage();
      const bodyText = await rawPage.evaluate(() => document.body?.innerText || '');
      const url = rawPage.url();

      const isBlocked = await proxyPool.detectGeoBlock(bodyText);
      const suggestedRegion = isBlocked ? proxyPool.suggestRegion(url, bodyText) : null;
      const hasProxy = suggestedRegion ? proxyPool.hasProxy(suggestedRegion) : false;

      res.json({
        blocked: isBlocked,
        suggestedRegion,
        hasProxy,
        url,
      });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** List configured proxies. */
  app.get('/proxies', (_req, res) => {
    const proxies = proxyPool.listProxies().map((p) => ({
      region: p.region,
      domains: p.domains,
      // Don't expose the actual proxy URL for security
    }));
    res.json({ proxies, count: proxies.length });
  });

  /** List active sessions. */
  app.get('/sessions', (_req, res) => {
    const list = Array.from(sessions.keys());
    res.json({ sessions: list, count: list.length });
  });

  // Expose sessions for WebSocket server binding
  (app as any)._getSessions = () => sessions;

  return app;
}

function handleError(res: express.Response, err: unknown): void {
  if (err instanceof BrowserError) {
    res.status(err.statusCode).json({ error: err.message });
  } else {
    const msg = err instanceof Error ? err.message : String(err);
    const code = msg.includes('Too many requests') ? 429 : 500;
    res.status(code).json({ error: msg });
  }
}
