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
import { selectOptionByLabel, selectOptionByVisionBbox, selectOptionInIframe } from '../browser/elements.js';
import { ProxyPool } from '../browser/proxy-pool.js';
import { HumanInputManager, type HumanInputType } from '../agent/human-input.js';
import { renderCaptchaViewHtml } from './captcha-ui-html.js';
import { feedbackBus } from '../agent/feedback-bus.js';
import { fingerprintMap, invertFingerprintMap, fingerprintElement } from '../browser/fingerprint.js';
import { getDomainStats, hostKey } from '../browser/captcha/domain-stats.js';
import { loadDomainCookies, saveDomainCookies } from '../browser/captcha/cookie-jar.js';
import { coordBand } from '../agent/step-observation.js';
import { captureEffect, diffEffect, settleForEffect, type EffectSnapshot } from './effect.js';
import {
  waitForTargetStable,
  capturePageRef,
  compareViewportShift,
} from '../browser/page-readiness.js';

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
        // /function is the stealth one-shot endpoint — callers use it
        // for genuine automation (rendering, scraping, seeded clicks);
        // keep mutations enabled here. The sandbox default only applies
        // to the agent-facing /session/:id/script path.
        const scriptResult = await runPuppeteerScript(
          rawPage, code, context, req.body.timeout, { mutates: true },
        );

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

      // Persist any cookies the site set during a successful navigation
      // so the NEXT session on this domain (T1, T2, or T3) starts warm.
      // Previously T1 only saved on captcha solve, leaving every fresh
      // T1 attempt cold. Status check rejects 4xx/5xx pages where the
      // cookies might be Cloudflare's challenge state rather than the
      // domain's real session.
      if (
        req.body.url
        && navStatusCode !== null
        && navStatusCode >= 200
        && navStatusCode < 400
        && /^https?:/i.test(req.body.url)
      ) {
        try { await saveDomainCookies(page.getRawPage(), req.body.url); }
        catch { /* best-effort */ }
      }

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

      // Persist post-navigation cookies on success — symmetric with the
      // /session/create path. T1 used to skip this, so returning to the
      // same domain in a later session always restarted from cold and
      // re-triggered the antibot challenge.
      if (
        nav.statusCode !== null
        && nav.statusCode >= 200
        && nav.statusCode < 400
        && /^https?:/i.test(url)
      ) {
        try { await saveDomainCookies(page.getRawPage(), url); }
        catch { /* best-effort */ }
      }

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
      // Optional settle gate — Python-side prefetch sends `settle=true`
      // after navigation so the screenshot reflects a post-render page
      // instead of a mid-transition one. v5: now waits for VISUAL
      // stability (fonts loaded, above-fold images decoded, layout-
      // shift idle), not just DOM-ready. DOM-ready was already done
      // by navigate(); rerunning it was a no-op. Bounded at the
      // helper's own maxMs (default 1500ms / VISUAL_STABLE_MAX_MS) so
      // a broken page can't hang the whole state request.
      if (req.query.settle === 'true') {
        try {
          const { waitForVisualStable } = await import('../browser/page-readiness.js');
          await waitForVisualStable(page.getRawPage());
        } catch {
          /* best-effort — never block /state on a settle failure */
        }
      }
      const state = await page.getState({
        useVision,
        includeConsole: true,
        includeAccessibility: req.query.accessibility === 'true',
        includeCursorElements: req.query.cursor === 'true',
      });

      // When this /state call is producing a screenshot for the brain
      // (useVision=true), capture the page reference frame
      // (scrollY/scrollHeight/viewport dims) right after getState so
      // the /click handler can detect a layout shift between this
      // capture and a subsequent click. We only update when vision is
      // requested — the brain's mental model is anchored to the last
      // screenshot, not to side-channel /state probes.
      if (useVision) {
        try {
          const ref = await capturePageRef(page.getRawPage());
          page.setVisionPageRef(ref);
        } catch {
          /* best-effort — never block /state on a ref capture failure */
        }
      }

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

      // Phase I: same-origin iframe content signature. Includes:
      //   - body.innerText length + first 200 chars + scrollY (catches
      //     question-text changes, layout updates, new screens)
      //   - input/textarea/select values + checked/selectedIndex state
      //     (catches typing into iframe inputs — values are NOT in
      //     innerText, so without this typing "44" into #userAnswer
      //     leaves the signature unchanged and the vision cache stays
      //     stale)
      //   - active element id/tag (focus changes affect what
      //     browser_keys will target next)
      // Cross-origin iframes throw on contentDocument access — handled
      // silently and skipped (empty contribution for those).
      let iframeSignature = '';
      try {
        iframeSignature = await page.getRawPage().evaluate(() => {
          const parts: string[] = [];
          const frames = document.querySelectorAll('iframe');
          for (let i = 0; i < Math.min(frames.length, 8); i++) {
            const f = frames[i] as HTMLIFrameElement;
            let inner = '';
            try {
              const d = f.contentDocument;
              if (d && d.body) {
                const txt = (d.body.innerText || '').replace(/\s+/g, ' ').trim();
                const sy = (d.defaultView && d.defaultView.scrollY) || 0;
                // Phase I': capture form field values. innerText does
                // NOT include <input>/<textarea>/<select> values, so
                // typing into iframe inputs is invisible to the outer
                // signature without this. Cap at 30 fields and 50 chars
                // each to keep the signature compact.
                const fields = d.querySelectorAll('input,textarea,select');
                const fvParts: string[] = [];
                for (let j = 0; j < Math.min(fields.length, 30); j++) {
                  const fe = fields[j] as HTMLInputElement
                                       | HTMLTextAreaElement
                                       | HTMLSelectElement;
                  const tag = fe.tagName.toLowerCase();
                  const id = fe.id ? `#${fe.id}` : `[${j}]`;
                  let val = '';
                  try {
                    if (tag === 'select') {
                      val = (fe as HTMLSelectElement).value || '';
                    } else if (tag === 'input') {
                      const inp = fe as HTMLInputElement;
                      const t = (inp.type || 'text').toLowerCase();
                      if (t === 'checkbox' || t === 'radio') {
                        val = inp.checked ? '1' : '0';
                      } else {
                        val = inp.value || '';
                      }
                    } else {
                      val = (fe as HTMLTextAreaElement).value || '';
                    }
                  } catch { val = ''; }
                  // Only include non-empty values + every <select>
                  // (so a default-select still appears) to keep size
                  // bounded.
                  if (val || tag === 'select') {
                    fvParts.push(`${tag}${id}=${val.slice(0, 50)}`);
                  }
                }
                const fv = fvParts.join('|');
                // Active element marker — focus state inside the
                // iframe. Affects whether browser_keys will hit the
                // right field.
                let activeMarker = '';
                try {
                  const ae = d.activeElement as HTMLElement | null;
                  if (ae && ae !== d.body && ae !== d.documentElement) {
                    const aeId = ae.id ? `#${ae.id}` : '';
                    activeMarker = ` active=${ae.tagName.toLowerCase()}${aeId}`;
                  }
                } catch { /* ignore */ }
                inner = `len=${txt.length} sy=${Math.round(sy)}`
                      + `${activeMarker} fv=${fv} head=${txt.slice(0, 200)}`;
              }
            } catch { /* cross-origin */ }
            const r = f.getBoundingClientRect();
            const visible = r.width > 0 && r.height > 0;
            const id = f.id || '';
            const aria = f.getAttribute('aria-label') || '';
            parts.push(
              `iframe[${i}] id=${id} aria=${aria.slice(0, 30)} `
              + `vis=${visible ? 1 : 0} ${inner}`,
            );
          }
          return parts.join('\n');
        });
      } catch {
        iframeSignature = '';
      }

      res.json({
        url: state.url,
        title: state.title,
        screenshot: useVision ? state.screenshot : undefined,
        elements: state.elementTree.clickableElementsToString(),
        // Phase I: same-origin iframe content summary. Python mixes
        // this into dom_hash so iframe-internal mutations bust the
        // vision cache. Empty string when the page has no iframes
        // (no cache-key impact) or all iframes are cross-origin.
        iframeSignature,
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
        // Pass selectorEntries so the bounds bucket is included — drops
        // collision rate on repeating sibling lists (filter checkboxes
        // with identical aria-labels at different on-screen positions).
        fingerprints: fingerprintMap(state.selectorMap, state.selectorEntries),
      });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Click an element by index. Returns updated screenshot + state. */
  app.post('/session/:id/click', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    const effectBefore: EffectSnapshot = await captureEffect(page.getRawPage());
    try {
      const { index, x, y, bbox, button, clickCount, expected_fingerprint, expected_label, strategy } = req.body as {
        index?: number;
        x?: number;
        y?: number;
        /** Vision-supplied bounding box (CSS pixels). Server snaps to
         *  the nearest interactive element inside it — preferred over
         *  raw (x, y) because it eliminates LLM coordinate drift. */
        bbox?: { x0: number; y0: number; x1: number; y1: number };
        button?: 'left' | 'right' | 'middle';
        clickCount?: number;
        /** Fingerprint the LLM was targeting (from the last state fetch).
         *  When provided and index is used, we reject if current[index]
         *  has a different fingerprint — prevents clicking a different
         *  element than the LLM intended after a DOM re-render. */
        expected_fingerprint?: string;
        /** v6 F2: vision's label for this bbox. clickInBbox uses it
         *  to validate that the snapped element's text/aria-label
         *  aligns — catches page-shift cases where the bbox centre
         *  now lies on a different element. */
        expected_label?: string;
        /** Silent-click escalation ladder: when the bridge detected a
         *  no-op primary click, it retries via `strategy: 'js'` (direct
         *  el.click() in the page context — bypasses bezier sweep
         *  guards) or `strategy: 'keyboard'` (focus + Enter — works
         *  for buttons/links that gate on key events). Mirrors what
         *  T3SessionManager.click_at(strategy=...) does for t3
         *  sessions; this lets t1 sessions hit the same recovery
         *  path. Only meaningful when bbox is provided. Falls through
         *  to the primary path when omitted or set to 'primary'. */
        strategy?: 'primary' | 'js' | 'keyboard';
      };

      if (bbox !== undefined) {
        // Vision-bbox click: snap to interactive element, dispatch
        // click at the snapped centre, return the resolved point so
        // the bridge can log/trace which element actually received
        // the input.
        //
        // Layer 1 — viewport-shift gate. The brain's V_n bbox is in
        // viewport-CSS coordinates frozen at vision-capture time. If
        // the page has scrolled or the layout has reflowed since
        // (lazy-load, banner injection, modal open), those CSS coords
        // now point at the wrong absolute element — labels alone
        // can't catch this when the new occupant is the same kind
        // of widget. Reject early so the brain re-screenshots
        // instead of dispatching against a stale frame.
        const stored = page.getVisionPageRef();
        const currentRef = await capturePageRef(page.getRawPage());
        const shift = compareViewportShift(stored, currentRef);
        if (process.env.VIEWPORT_SHIFT_DEBUG === '1') {
          console.log(
            `[viewport_shift] shifted=${shift.shifted}`
            + ` reason=${shift.reason}`
            + ` dy=${shift.delta.scrollY}`
            + ` dh=${shift.delta.scrollHeight}`
            + ` dvh=${shift.delta.viewportHeight}`,
          );
        }
        if (shift.shifted) {
          res.json({
            error: 'viewport_shifted',
            reason: shift.reason,
            delta: shift.delta,
            stored: shift.stored,
            current: shift.current,
            expected_label,
            bbox,
          });
          return;
        }

        // Layer 2 — pre-dispatch stability gate. Even on a non-shifted
        // page, the target element itself may be mid-animation.
        // waitForTargetStable polls in-page (one CDP roundtrip) until
        // the captured element's rect holds still and the surrounding
        // tree quiets. On stable, re-aim onto the element's CURRENT
        // bounds — the LLM's bbox is a hint, not a contract. On
        // timeout we proceed anyway; clickInBbox Phase 1/2 +
        // post-click verify catch the rest.
        let dispatchBbox = bbox;
        const cx = (bbox.x0 + bbox.x1) / 2;
        const cy = (bbox.y0 + bbox.y1) / 2;
        const stab = await waitForTargetStable(
          page.getRawPage(),
          { kind: 'point', x: cx, y: cy },
        );
        if (stab.lastBounds && stab.lastBounds.w > 0 && stab.lastBounds.h > 0) {
          const lb = stab.lastBounds;
          dispatchBbox = {
            x0: lb.x, y0: lb.y, x1: lb.x + lb.w, y1: lb.y + lb.h,
          };
        }
        if (process.env.CLICK_STABILITY_DEBUG === '1') {
          console.log(
            `[click_stability] branch=vision_bbox stable=${stab.stable}`
            + ` reason=${stab.reason} samples=${stab.samples}`,
          );
        }

        // Strategy escalation: when the bridge sends `strategy: 'js'`
        // or `strategy: 'keyboard'` (the silent-click ladder fired
        // because the primary CDP click produced zero DOM mutations),
        // dispatch via the alternate mechanism instead of going through
        // clickInBbox's snap pipeline. Mirrors T3SessionManager.click_at
        // (strategy=...) so t1 sessions get the same recovery
        // behaviour as t3. The bridge re-runs verify_after on the
        // response to decide if the escalation landed.
        if (strategy === 'js' || strategy === 'keyboard') {
          const dispatchX = (dispatchBbox.x0 + dispatchBbox.x1) / 2;
          const dispatchY = (dispatchBbox.y0 + dispatchBbox.y1) / 2;
          if (strategy === 'js') {
            await page.dispatchJsClickAt(dispatchX, dispatchY);
          } else {
            await page.dispatchKeyboardEnterAt(dispatchX, dispatchY);
          }
          await settleForEffect(page.getRawPage());
          const effectAfterAlt = await captureEffect(page.getRawPage());
          const newStateAlt = await page.getState({
            useVision: false, includeConsole: true,
          });
          res.json({
            success: true,
            url: newStateAlt.url,
            title: newStateAlt.title,
            elements: newStateAlt.elementTree.clickableElementsToString(),
            consoleErrors: newStateAlt.consoleErrors,
            pendingDialogs: newStateAlt.pendingDialogs,
            snap: { x: dispatchX, y: dispatchY, snapped: false, target: '' },
            strategy,
            effect: diffEffect(effectBefore, effectAfterAlt),
            fingerprints: fingerprintMap(
              newStateAlt.selectorMap, newStateAlt.selectorEntries,
            ),
          });
          return;
        }

        const snap = await page.clickInBbox(dispatchBbox, {
          button,
          clickCount,
          expectedLabel: expected_label,
        });
        // Phase B fallback: clickInBbox's in-page descent (Phase A)
        // could not resolve the click target inside the iframe.
        // Three cases trigger this path:
        //   - cross_origin: contentDocument blocked by SOP; the Frame
        //     walk uses Puppeteer's Target API which CAN access OOPIFs.
        //   - miss: same-origin but neither pinpoint nor inner grid
        //     scan found a clickable in the bbox region. Worth retry
        //     because Phase B's snap may pick up a candidate the
        //     in-page scan missed (it runs inside frame.evaluate, so
        //     same JS engine but potentially different frame state).
        //   - legacy `target_in_iframe`: shouldn't occur now but kept
        //     defensively.
        // Successful frameSnap REPLACES the original snap result so
        // the rest of the pipeline (settle / effect diff / dom_index)
        // uses the resolved target.
        const needsFrameFallback =
          snap.warning === 'target_in_iframe_cross_origin'
          || snap.warning === 'target_in_iframe_miss'
          || snap.warning === 'target_in_iframe';
        if (needsFrameFallback) {
          try {
            const frameSnap = await page.clickInIframeFrame(dispatchBbox, {
              button,
              clickCount,
              expectedLabel: expected_label,
            });
            if (frameSnap.snapped) {
              // Preserve iframe_host_selector from Phase A (Phase B
              // doesn't compute it the same way).
              const phaseAHost = (snap as { iframe_host_selector?: string })
                .iframe_host_selector;
              Object.assign(snap, frameSnap);
              if (phaseAHost && !(snap as { iframe_host_selector?: string })
                  .iframe_host_selector) {
                (snap as { iframe_host_selector?: string })
                  .iframe_host_selector = phaseAHost;
              }
            } else if (frameSnap.warning) {
              snap.warning = frameSnap.warning;
            }
          } catch (e) {
            console.warn('[click] clickInIframeFrame fallback failed:',
              (e as Error).message);
          }
        }
        // labelMismatch is no longer a hard block — historically we
        // returned element_mismatch here and skipped dispatch, but that
        // caused silent rejections when the brain's expected_label was
        // slightly off (paraphrased autocomplete suggestion, stale
        // label from a prior screenshot, etc.). The bbox-snap above
        // already picked the best element under the bbox by area; we
        // trust that and dispatch. The labelMismatch flag is still
        // surfaced in `snap` for the bridge to log as a diagnostic so
        // the brain can read what was actually clicked vs what it
        // expected.
        await settleForEffect(page.getRawPage());
        const effectAfter = await captureEffect(page.getRawPage());
        const newState = await page.getState({ useVision: false, includeConsole: true });
        // Resolve the snapped element to its DOM index so the Python
        // bridge can record `last_click_dom_index`. Cross-tool dead-
        // click guard then catches a follow-up `browser_click(N)` on
        // the SAME element after a `browser_click_at(V_n)` toggled it.
        let domIndex: number | undefined;
        if (snap.targetXpath) {
          for (const [idx, el] of newState.selectorMap) {
            if (el.xpath === snap.targetXpath) {
              domIndex = idx;
              break;
            }
          }
        }
        const snapWithIndex = domIndex !== undefined
          ? { ...snap, dom_index: domIndex }
          : snap;
        res.json({
          success: true,
          url: newState.url,
          title: newState.title,
          elements: newState.elementTree.clickableElementsToString(),
          consoleErrors: newState.consoleErrors,
          pendingDialogs: newState.pendingDialogs,
          snap: snapWithIndex,
          effect: diffEffect(effectBefore, effectAfter),
          fingerprints: fingerprintMap(newState.selectorMap, newState.selectorEntries),
        });
        return;
      }

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
          const entryByIdx = new Map<number, import('../browser/dom.js').SelectorEntry>();
          for (const e of state.selectorEntries ?? []) {
            if (typeof e.index === 'number') entryByIdx.set(e.index, e);
          }
          const currentFp = fingerprintElement(element, entryByIdx.get(index) ?? null);
          if (currentFp !== expected_fingerprint) {
            const currentFpMap = fingerprintMap(state.selectorMap, state.selectorEntries);
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

        // Route the DOM-index click through the same bbox-snap pipeline
        // that V_n clicks use. We compute LIVE bounds at click time
        // (via xpath → getBoundingClientRect) so any drift between the
        // brain's mental model of [N] and the page's current state is
        // caught by clickInBbox's Phase 1 label-match guard. Brain's
        // tool surface is unchanged; the protections are now invisible
        // to it.
        //
        // Fallback to legacy clickElement when:
        //   - DOM_CLICK_VIA_BBOX=0 (env-flag opt-out for prod rollback);
        //   - bounds aren't readable (element removed or zero-size);
        //   - clickInBbox failed to dispatch (then snap.snapped=false
        //     AND we surface a hard error rather than the silent
        //     fallback the legacy path used).
        const viaBboxEnabled = process.env.DOM_CLICK_VIA_BBOX !== '0';
        let snapWithIndex: Record<string, unknown> | null = null;

        if (viaBboxEnabled) {
          const live = await page.getRawPage().evaluate((xp: string) => {
            try {
              const node = document.evaluate(
                xp, document, null,
                XPathResult.FIRST_ORDERED_NODE_TYPE, null,
              ).singleNodeValue;
              if (!node || !(node instanceof HTMLElement)) return null;
              // Scroll into view ONLY when the element isn't already
              // visible. The previous `block: 'center'` re-centred on
              // every click — even when the element was already a few
              // pixels off-centre — shifting the viewport 100-400px
              // each call. That broke the brain's V_n bboxes (which
              // are in viewport CSS coords) for every NEXT click,
              // because the element under coords (cx,cy) was now a
              // different absolute element. `'nearest'` only scrolls
              // when at least one edge is outside the viewport, and
              // scrolls the minimum amount to bring the element in.
              const r0 = node.getBoundingClientRect();
              const inView = (
                r0.top >= 0 && r0.bottom <= window.innerHeight
                && r0.left >= 0 && r0.right <= window.innerWidth
              );
              if (!inView) {
                try {
                  node.scrollIntoView({
                    block: 'nearest',
                    inline: 'nearest',
                    behavior: 'instant',
                  });
                } catch { /* ignore — older engines */ }
              }
              const r = node.getBoundingClientRect();
              return {
                x: r.left, y: r.top, w: r.width, h: r.height,
                tag: node.tagName.toLowerCase(),
              };
            } catch {
              return null;
            }
          }, element.xpath) as
            | { x: number; y: number; w: number; h: number; tag: string }
            | null;

          if (live && live.w > 0 && live.h > 0) {
            const expectedLabel = (
              element.attributes['aria-label']
              || element.attributes['placeholder']
              || element.attributes['title']
              || element.getAllTextTillNextClickableElement(2)
              || ''
            ).slice(0, 80).trim();

            // Pre-dispatch stability gate. The live bounds we just
            // queried are a single sample taken one tick ago; on
            // heavy-rendering sites the element may still be mid-
            // animation. Polling in-page until the rect quiets — and
            // the surrounding tree's mutation counter quiets — turns
            // a coordinate race into a 150-600ms wait. On timeout we
            // still dispatch with the most recent bounds (strictly
            // fresher than `live`), trusting clickInBbox Phase 1/2
            // and post-click verify to catch the residual misses.
            let dispatchBounds = live;
            const stab = await waitForTargetStable(
              page.getRawPage(),
              { kind: 'xpath', xpath: element.xpath },
            );
            if (
              stab.lastBounds
              && stab.lastBounds.w > 0
              && stab.lastBounds.h > 0
            ) {
              dispatchBounds = {
                x: stab.lastBounds.x,
                y: stab.lastBounds.y,
                w: stab.lastBounds.w,
                h: stab.lastBounds.h,
                tag: live.tag,
              };
            }
            if (process.env.CLICK_STABILITY_DEBUG === '1') {
              console.log(
                `[click_stability] branch=dom_index xpath=${element.xpath}`
                + ` stable=${stab.stable} reason=${stab.reason}`
                + ` samples=${stab.samples}`,
              );
            }

            const snap = await page.clickInBbox(
              {
                x0: dispatchBounds.x, y0: dispatchBounds.y,
                x1: dispatchBounds.x + dispatchBounds.w,
                y1: dispatchBounds.y + dispatchBounds.h,
              },
              { button, clickCount, expectedLabel: expectedLabel || undefined },
            );

            // labelMismatch is no longer a hard block (mirrors the
            // vision-bbox branch above). The snap already picked the
            // best candidate by area; we dispatch and surface the
            // mismatch flag in the response for the bridge to log as
            // a diagnostic. Brain reads what was actually clicked and
            // adapts — no silent rejection.
            await settleForEffect(page.getRawPage());
            const effectAfter = await captureEffect(page.getRawPage());
            const newState = await page.getState({
              useVision: false, includeConsole: true,
            });
            // Resolve the snapped element to its DOM index in the
            // post-click state. Falls back to the brain's [N] when the
            // snap targeted a different element (rare — usually means
            // grid-scan picked a sibling because the centre-element
            // didn't label-match). Either way the brain learns the
            // identity.
            let domIndex: number | undefined;
            if (snap.targetXpath) {
              for (const [idx, el] of newState.selectorMap) {
                if (el.xpath === snap.targetXpath) {
                  domIndex = idx;
                  break;
                }
              }
            }
            if (domIndex === undefined) domIndex = index;
            snapWithIndex = { ...snap, dom_index: domIndex };

            res.json({
              success: true,
              url: newState.url,
              title: newState.title,
              elements: newState.elementTree.clickableElementsToString(),
              consoleErrors: newState.consoleErrors,
              pendingDialogs: newState.pendingDialogs,
              snap: snapWithIndex,
              effect: diffEffect(effectBefore, effectAfter),
              fingerprints: fingerprintMap(
                newState.selectorMap, newState.selectorEntries,
              ),
            });
            return;
          }
          // bounds unreadable — fall through to legacy path.
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
      await settleForEffect(page.getRawPage());
      const effectAfter = await captureEffect(page.getRawPage());
      const newState = await page.getState({ useVision: false, includeConsole: true });
      res.json({
        success: true,
        url: newState.url,
        title: newState.title,
        elements: newState.elementTree.clickableElementsToString(),
        consoleErrors: newState.consoleErrors,
        pendingDialogs: newState.pendingDialogs,
        effect: diffEffect(effectBefore, effectAfter),
        // Fresh per-index fingerprints so the Python bridge's
        // element_fingerprints cache stays in sync after every click.
        // Without this, a follow-up DOM-index click on a re-rendered
        // page sends a STALE expected_fingerprint that may collide with
        // the new occupant of [N], silently misclicking.
        fingerprints: fingerprintMap(newState.selectorMap, newState.selectorEntries),
      });
    } catch (err) {
      handleError(res, err);
    }
  });

  /**
   * Push a vision-pass bbox set to live viewers. Called by the Python
   * bridge fire-and-forget right after the vision agent (Gemini)
   * returns. Body shape:
   *   {
   *     bboxes: [
   *       { x0, y0, x1, y1, label?, role?, clickable?,
   *         intent_relevant?, index? },
   *       ...
   *     ],
   *     imageWidth?: number,
   *     imageHeight?: number,
   *   }
   * Coords are CSS pixels (already denormalized from box_2d).
   */
  app.post('/session/:id/vision-bboxes', async (req, res) => {
    if (!getSession(req.params.id)) {
      res.status(404).json({ error: 'Session not found or expired' });
      return;
    }
    try {
      const body = req.body as {
        bboxes?: Array<{
          x0: number; y0: number; x1: number; y1: number;
          label?: string; role?: string;
          clickable?: boolean; intent_relevant?: boolean; index?: number;
        }>;
        imageWidth?: number;
        imageHeight?: number;
        url?: string;
        freshness?: 'fresh' | 'uncertain' | 'stale';
        latencyMs?: number;
      };
      const bboxes = Array.isArray(body.bboxes) ? body.bboxes : [];
      // Lazy import to avoid a circular dep at module load.
      const { inputEventBus } = await import('../browser/input-events.js');
      inputEventBus.emitVisionBboxes(
        req.params.id,
        bboxes,
        body.imageWidth,
        body.imageHeight,
        {
          url: body.url,
          freshness: body.freshness,
          latencyMs: body.latencyMs,
        },
      );
      res.json({ ok: true, count: bboxes.length });
    } catch (err) {
      handleError(res, err);
    }
  });

  /**
   * Announce that a vision pass is in flight. The UI shows a
   * transient "vision updating…" indicator until the next
   * vision-bboxes event lands.
   */
  app.post('/session/:id/vision-pending', async (req, res) => {
    if (!getSession(req.params.id)) {
      res.status(404).json({ error: 'Session not found or expired' });
      return;
    }
    try {
      const body = (req.body ?? {}) as { dispatchedAt?: number };
      const { inputEventBus } = await import('../browser/input-events.js');
      inputEventBus.emitVisionPending(req.params.id, body.dispatchedAt);
      res.json({ ok: true });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Type text into an element. Returns updated state. */
  app.post('/session/:id/type', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    const effectBefore: EffectSnapshot = await captureEffect(page.getRawPage());
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
        const entryByIdx = new Map<number, import('../browser/dom.js').SelectorEntry>();
        for (const e of state.selectorEntries ?? []) {
          if (typeof e.index === 'number') entryByIdx.set(e.index, e);
        }
        const currentFp = fingerprintElement(element, entryByIdx.get(index) ?? null);
        if (currentFp !== expected_fingerprint) {
          const currentFpMap = fingerprintMap(state.selectorMap, state.selectorEntries);
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

      // Pre-type value probe — gives the LLM "before X, now Y" feedback so
      // a silent clear-failure-and-append doesn't look like a clean type.
      // Best-effort: empty default if probe throws.
      let pretypeValue = '';
      try {
        const sel = element.enhancedCssSelectorForElement();
        pretypeValue = await page.getRawPage().evaluate((s: string) => {
          const el = document.querySelector(s);
          if (!el) return '';
          const valEl = el as unknown as { value?: string };
          if (typeof valEl.value === 'string') return valEl.value || '';
          if ((el as HTMLElement).isContentEditable) {
            return (el as HTMLElement).innerText || '';
          }
          return '';
        }, sel);
      } catch (_e) { /* probe is best-effort */ }

      // Skip-match short-circuit: field already matches target, no work
      // needed (mirrors the t3 contract at interactive_session.py:2889-2897).
      if (pretypeValue === text && pretypeValue !== '') {
        const newState = await page.getState({ useVision: false, includeConsole: true });
        res.json({
          success: true,
          elements: newState.elementTree.clickableElementsToString(),
          effect: { url_changed: false, mutation_delta: 0, focused_changed: false },
          pretype_action: 'skip_match',
          pretype_value: pretypeValue,
        });
        return;
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

      await settleForEffect(page.getRawPage());
      const effectAfter = await captureEffect(page.getRawPage());
      const newState = await page.getState({ useVision: false, includeConsole: true });

      // Empty before → typed_into_empty; non-empty before → cleared_and_typed.
      const pretypeAction = pretypeValue ? 'cleared_and_typed' : 'typed_into_empty';

      res.json({
        success: true,
        elements: newState.elementTree.clickableElementsToString(),
        effect: diffEffect(effectBefore, effectAfter),
        pretype_action: pretypeAction,
        pretype_value: pretypeValue,
      });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Send keyboard keys. Returns updated state. */
  app.post('/session/:id/keys', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    const effectBefore: EffectSnapshot = await captureEffect(page.getRawPage());
    try {
      await page.sendKeys(req.body.keys);
      await new Promise((r) => setTimeout(r, 500));
      await settleForEffect(page.getRawPage());
      const effectAfter = await captureEffect(page.getRawPage());
      const newState = await page.getState({ useVision: false });
      res.json({
        success: true,
        elements: newState.elementTree.clickableElementsToString(),
        effect: diffEffect(effectBefore, effectAfter),
      });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Scroll the page. Returns updated state, plus pre/post scroll Y so
   * the caller can detect silent no-op scrolls (locked-body SPAs that
   * neither window nor scrollingElement nor the largest container can
   * scroll — rare but real, and otherwise invisible to the LLM).
   */
  app.post('/session/:id/scroll', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    try {
      const { direction, percent, pixels } = req.body;
      const [preY] = await page.getScrollInfo();
      // `pixels` (when set) wins — explicit incremental motion that
      // sidesteps the percent-vs-viewport ambiguity. Falls through to
      // legacy direction/percent paths otherwise.
      if (typeof pixels === 'number' && pixels > 0) {
        await page.scrollByPixels(direction === 'up' ? 'up' : 'down', pixels);
      } else if (percent !== undefined) {
        await page.scrollToPercent(percent);
      } else {
        await page.scrollPage(direction || 'down');
      }

      const newState = await page.getState({ useVision: false });
      res.json({
        success: true,
        elements: newState.elementTree.clickableElementsToString(),
        prevScrollInfo: { scrollY: preY },
        scrollInfo: { scrollY: newState.scrollY, scrollHeight: newState.scrollHeight, viewportHeight: newState.viewportHeight },
      });
    } catch (err) {
      handleError(res, err);
    }
  });

  /**
   * Closed-loop scroll: walks the page in `direction` until an element
   * matching `targetText` (substring or regex) and/or `targetRole`
   * appears in viewport, the page can't scroll further, or
   * `maxIterations` elapses. Returns a structured outcome with a
   * `reason` so the caller knows whether to keep trying, retreat, or
   * give up — closes the "blind scroll, then re-screenshot" loop the
   * raw /scroll endpoint forces.
   */
  app.post('/session/:id/scroll-until', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    try {
      const {
        targetText, targetRole, direction, maxIterations, stepRatio,
        cadence, autoReverse, containerSelector, emitTrace,
      } = req.body ?? {};
      if (
        (typeof targetText !== 'string' || !targetText.trim())
        && (typeof targetRole !== 'string' || !targetRole.trim())
      ) {
        res.status(400).json({ error: 'targetText or targetRole required' });
        return;
      }
      const cad = (cadence === 'fine' || cadence === 'medium' || cadence === 'coarse')
        ? cadence
        : undefined;
      const outcome = await page.scrollUntil({
        targetText: typeof targetText === 'string' ? targetText : undefined,
        targetRole: typeof targetRole === 'string' ? targetRole : undefined,
        direction: direction === 'up' ? 'up' : 'down',
        maxIterations: typeof maxIterations === 'number' ? maxIterations : 10,
        // Only forward stepRatio when the caller explicitly set it —
        // otherwise let cadence drive the default. Avoids overriding
        // the new fine/medium semantics with an old hardcoded 0.8.
        ...(typeof stepRatio === 'number' ? { stepRatio } : {}),
        cadence: cad,
        autoReverse: typeof autoReverse === 'boolean' ? autoReverse : true,
        containerSelector: typeof containerSelector === 'string' && containerSelector.trim()
          ? containerSelector
          : undefined,
        emitTrace: typeof emitTrace === 'boolean' ? emitTrace : true,
      });

      const newState = await page.getState({ useVision: false });
      res.json({
        success: outcome.found,
        outcome,
        url: newState.url,
        elements: newState.elementTree.clickableElementsToString(),
        scrollInfo: {
          scrollY: newState.scrollY,
          scrollHeight: newState.scrollHeight,
          viewportHeight: newState.viewportHeight,
        },
      });
    } catch (err) {
      handleError(res, err);
    }
  });

  /**
   * Scroll inside a popup/listbox/menu/modal. Page-level scroll won't
   * move a popup's internal scroll, so dropdowns whose options extend
   * below the visible menu need this. Auto-detects the most recently
   * opened scrollable popup when `containerSelector` is omitted.
   */
  app.post('/session/:id/scroll-within', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    try {
      const {
        containerSelector, direction, amount, targetText, maxIterations,
      } = req.body ?? {};
      const amt = (amount === 'page' || amount === 'half' || typeof amount === 'number')
        ? amount
        : undefined;
      const outcome = await page.scrollWithin({
        containerSelector: typeof containerSelector === 'string' && containerSelector.trim()
          ? containerSelector
          : undefined,
        direction: direction === 'up' ? 'up' : 'down',
        amount: amt,
        targetText: typeof targetText === 'string' && targetText.trim() ? targetText : undefined,
        maxIterations: typeof maxIterations === 'number' ? maxIterations : undefined,
      });

      const newState = await page.getState({ useVision: false });
      res.json({
        success: outcome.found || outcome.reason !== 'no_container',
        outcome,
        url: newState.url,
        elements: newState.elementTree.clickableElementsToString(),
        scrollInfo: {
          scrollY: newState.scrollY,
          scrollHeight: newState.scrollHeight,
          viewportHeight: newState.viewportHeight,
        },
      });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Drag from one point to another. Useful for slider CAPTCHAs and puzzle pieces. */
  app.post('/session/:id/drag', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    const effectBefore: EffectSnapshot = await captureEffect(page.getRawPage());
    try {
      const { startX, startY, endX, endY, steps } = req.body;
      if (startX === undefined || startY === undefined || endX === undefined || endY === undefined) {
        res.status(400).json({ error: 'startX, startY, endX, endY are required' });
        return;
      }

      await page.dragTo(startX, startY, endX, endY, { steps: steps || 25 });
      await new Promise((r) => setTimeout(r, 500));
      await settleForEffect(page.getRawPage());
      const effectAfter = await captureEffect(page.getRawPage());
      const newState = await page.getState({ useVision: false });
      res.json({
        success: true,
        url: newState.url,
        title: newState.title,
        elements: newState.elementTree.clickableElementsToString(),
        effect: diffEffect(effectBefore, effectAfter),
      });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Return getBoundingClientRect() for one or more CSS selectors. Cheap, synchronous, no state refresh. */
  app.post('/session/:id/rect', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    try {
      const { selectors, ensureVisible } = req.body ?? {};
      if (!Array.isArray(selectors) || selectors.some((s) => typeof s !== 'string')) {
        res.status(400).json({ error: 'selectors must be an array of CSS selector strings' });
        return;
      }
      const rects = await page.getRects(selectors, { ensureVisible: !!ensureVisible });
      res.json({ success: true, rects });
    } catch (err) {
      handleError(res, err);
    }
  });

  /**
   * INTERNAL endpoint — selector-based click. No longer exposed to the
   * LLM as a tool (registry entry removed; the agent must use vision-
   * bbox clicks via /click). Kept for internal callers:
   *   - Puzzle solvers (puzzle_solvers/browser.py) for captcha widgets
   *     with stable selectors (chess squares, captcha handles).
   *   - T3 backend mirror (session_tools/http_client.py) so patchright
   *     sessions keep parity with Puppeteer sessions.
   */
  app.post('/session/:id/click-selector', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    try {
      const {
        selector: rawSelector, button, clickCount, linear, ensureVisible,
        in_iframe: inIframe,
      } = req.body ?? {};
      if (typeof rawSelector !== 'string' || !rawSelector) {
        res.status(400).json({ error: 'selector is required' });
        return;
      }
      const selector = normalizeIdSelector(rawSelector);
      const result = (typeof inIframe === 'string' && inIframe.length > 0)
        ? await page.clickSelectorInIframe(
            normalizeIdSelector(inIframe), selector,
            { button, clickCount, linear },
          )
        : await page.clickSelector(selector, {
            button, clickCount, linear, ensureVisible,
          });
      const newState = await page.getState({ useVision: false });
      res.json({
        success: true,
        clicked: result,
        url: newState.url,
        title: newState.title,
        elements: newState.elementTree.clickableElementsToString(),
        fingerprints: fingerprintMap(newState.selectorMap, newState.selectorEntries),
      });
    } catch (err) {
      handleError(res, err);
    }
  });

  /**
   * Surgical undo support — single history.back() step with effect
   * diff. The Python recovery tool decides how far to go and calls us
   * repeatedly. Mirrors /click's effect-snapshot envelope so the
   * response shape is familiar to the brain.
   */
  app.post('/session/:id/back', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    const effectBefore: EffectSnapshot = await captureEffect(page.getRawPage());
    try {
      await page.goBack();
      await settleForEffect(page.getRawPage());
      const effectAfter = await captureEffect(page.getRawPage());
      const newState = await page.getState({ useVision: false });
      res.json({
        success: true,
        url: newState.url,
        title: newState.title,
        elements: newState.elementTree.clickableElementsToString(),
        effect: diffEffect(effectBefore, effectAfter),
        // Fresh fingerprints for the post-back DOM.
        fingerprints: fingerprintMap(newState.selectorMap, newState.selectorEntries),
      });
    } catch (err) {
      handleError(res, err);
    }
  });

  /**
   * Surgical undo support — read aria toggle state of a single
   * element by CSS selector. Returns null fields when missing /
   * absent. Used by browser_click_selector to capture pre_active
   * before dispatch so a later browser_undo_last_click on a toggle-
   * shaped target can verify the flip.
   */
  app.post('/session/:id/probe-aria', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }
    try {
      const { selector } = req.body ?? {};
      if (typeof selector !== 'string' || !selector) {
        res.status(400).json({ error: 'selector is required' });
        return;
      }
      const probe = await page.getRawPage().evaluate((sel: string) => {
        const el = document.querySelector(sel);
        if (!el) return null;
        const ariaCheckedRaw = el.getAttribute('aria-checked');
        const ariaPressedRaw = el.getAttribute('aria-pressed');
        const ariaSelectedRaw = el.getAttribute('aria-selected');
        const ariaCurrentRaw = el.getAttribute('aria-current');
        const truthy = (v: string | null) => v != null && v !== 'false' && v !== '';
        return {
          ariaChecked: ariaCheckedRaw,
          ariaPressed: ariaPressedRaw,
          ariaSelected: ariaSelectedRaw,
          ariaCurrent: ariaCurrentRaw,
          // Single normalized "is_active" — true if any toggle attribute
          // is truthy. None if no toggle attributes present at all.
          isActive: (
            ariaCheckedRaw == null
            && ariaPressedRaw == null
            && ariaSelectedRaw == null
            && ariaCurrentRaw == null
          )
            ? null
            : (truthy(ariaCheckedRaw) || truthy(ariaPressedRaw) || truthy(ariaSelectedRaw) || truthy(ariaCurrentRaw)),
        };
      }, selector);
      if (probe == null) {
        res.json({ success: false, found: false });
        return;
      }
      res.json({ success: true, found: true, ...probe });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Drag from one selector to another. Supports click_click / drag / auto methods. */
  app.post('/session/:id/drag-selectors', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    try {
      const { fromSelector, toSelector, method, holdMs, linear, steps } = req.body ?? {};
      if (typeof fromSelector !== 'string' || !fromSelector) {
        res.status(400).json({ error: 'fromSelector is required' });
        return;
      }
      if (typeof toSelector !== 'string' || !toSelector) {
        res.status(400).json({ error: 'toSelector is required' });
        return;
      }
      if (method && !['drag', 'click_click', 'auto'].includes(method)) {
        res.status(400).json({ error: "method must be one of: drag, click_click, auto" });
        return;
      }
      const outcome = await page.dragSelectors(fromSelector, toSelector, {
        method, holdMs, linear, steps,
      });
      const newState = await page.getState({ useVision: false });
      res.json({
        success: true,
        outcome,
        url: newState.url,
        title: newState.title,
        elements: newState.elementTree.clickableElementsToString(),
      });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Drag along an arbitrary polyline (jigsaw trace, connect-the-dots, signature). */
  app.post('/session/:id/drag-path', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    try {
      const { points, holdMs, stepMs, button } = req.body ?? {};
      if (!Array.isArray(points) || points.length < 2) {
        res.status(400).json({ error: 'points must be an array of at least 2 {x,y} objects' });
        return;
      }
      for (const p of points) {
        if (!p || typeof p.x !== 'number' || typeof p.y !== 'number') {
          res.status(400).json({ error: 'each point must be an object with numeric x,y' });
          return;
        }
      }
      await page.dragPath(points, { holdMs, stepMs, button });
      const newState = await page.getState({ useVision: false });
      res.json({
        success: true,
        pointCount: points.length,
        url: newState.url,
        title: newState.title,
        elements: newState.elementTree.clickableElementsToString(),
      });
    } catch (err) {
      handleError(res, err);
    }
  });

  /**
   * Set a slider's value (native range input, ARIA slider, or custom widget).
   * Frame-aware: resolves selector across all frames, including same-origin
   * iframes. Tries value-set → keyboard → drag in that order.
   */
  app.post('/session/:id/set-slider', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    try {
      const { selector, value, as, method } = req.body ?? {};
      if (typeof selector !== 'string' || !selector) {
        res.status(400).json({ error: 'selector (string) is required' });
        return;
      }
      const valid = (v: unknown): v is number | [number, number] =>
        typeof v === 'number'
        || (Array.isArray(v) && v.length === 2 && v.every((n) => typeof n === 'number'));
      if (!valid(value)) {
        res.status(400).json({ error: 'value must be a number or [lo, hi] for dual-thumb' });
        return;
      }
      if (as && !['absolute', 'ratio'].includes(as)) {
        res.status(400).json({ error: "as must be 'absolute' or 'ratio'" });
        return;
      }
      if (method && !['auto', 'range-input', 'keyboard', 'drag'].includes(method)) {
        res.status(400).json({ error: "method must be one of: auto, range-input, keyboard, drag" });
        return;
      }
      const outcome = await page.setSlider(selector, value, { as, method });
      const newState = await page.getState({ useVision: false });
      res.json({
        success: true,
        outcome,
        url: newState.url,
        title: newState.title,
        elements: newState.elementTree.clickableElementsToString(),
      });
    } catch (err) {
      handleError(res, err);
    }
  });

  /**
   * Vision-indexed slider drag. Caller supplies handle + track bboxes
   * (already resolved via cached vision response) and a 0..1 ratio.
   * Frame-agnostic: bboxes are document-coordinate rects and CDP drag
   * dispatches there directly.
   */
  app.post('/session/:id/set-slider-at', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    try {
      const { handle, track, ratio } = req.body ?? {};
      const isBbox = (b: unknown): b is { x: number; y: number; w: number; h: number } =>
        !!b && typeof b === 'object'
        && typeof (b as any).x === 'number' && typeof (b as any).y === 'number'
        && typeof (b as any).w === 'number' && typeof (b as any).h === 'number';
      if (!isBbox(handle)) {
        res.status(400).json({ error: 'handle must be {x,y,w,h}' });
        return;
      }
      if (!isBbox(track)) {
        res.status(400).json({ error: 'track must be {x,y,w,h}' });
        return;
      }
      if (typeof ratio !== 'number' || !isFinite(ratio)) {
        res.status(400).json({ error: 'ratio must be a finite number in [0, 1]' });
        return;
      }
      const outcome = await page.setSliderAt(handle, track, ratio);
      const newState = await page.getState({ useVision: false });
      res.json({
        success: true,
        outcome,
        url: newState.url,
        title: newState.title,
        elements: newState.elementTree.clickableElementsToString(),
      });
    } catch (err) {
      handleError(res, err);
    }
  });

  /**
   * DOM-only slider enumeration. No vision required.
   */
  app.post('/session/:id/list-slider-handles', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }
    try {
      const handles = await page.listSliderHandles();
      res.json({ success: true, handles });
    } catch (err) {
      handleError(res, err);
    }
  });

  /**
   * Closed-loop slider drag. Holds mouse down on the handle, steps
   * incrementally, polls every frame's DOM for a labelled value, stops
   * when the target is reached (within tolerance). Frame-agnostic.
   */
  app.post('/session/:id/drag-slider-until', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    const effectBefore: EffectSnapshot = await captureEffect(page.getRawPage());
    try {
      const { handle, target_value, label_pattern, tolerance, max_iterations, step_px, direction } = req.body ?? {};
      const isBbox = (b: unknown): b is { x: number; y: number; w: number; h: number } =>
        !!b && typeof b === 'object'
        && typeof (b as any).x === 'number' && typeof (b as any).y === 'number'
        && typeof (b as any).w === 'number' && typeof (b as any).h === 'number';
      if (!isBbox(handle)) {
        res.status(400).json({ error: 'handle must be {x,y,w,h}' });
        return;
      }
      if (typeof target_value !== 'number' || !isFinite(target_value)) {
        res.status(400).json({ error: 'target_value must be a finite number' });
        return;
      }
      if (direction && !['auto', 'left', 'right'].includes(direction)) {
        res.status(400).json({ error: "direction must be 'auto', 'left', or 'right'" });
        return;
      }
      const outcome = await page.dragSliderUntil(handle, target_value, {
        labelPattern: typeof label_pattern === 'string' ? label_pattern : undefined,
        tolerance: typeof tolerance === 'number' ? tolerance : undefined,
        maxIterations: typeof max_iterations === 'number' ? max_iterations : undefined,
        stepPx: typeof step_px === 'number' ? step_px : undefined,
        direction,
      });
      await settleForEffect(page.getRawPage());
      const effectAfter = await captureEffect(page.getRawPage());
      const newState = await page.getState({ useVision: false });
      res.json({
        success: outcome.completed,
        outcome,
        url: newState.url,
        title: newState.title,
        elements: newState.elementTree.clickableElementsToString(),
        effect: diffEffect(effectBefore, effectAfter),
      });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Screenshot a viewport region and return it as base64 JPEG. Cheaper than full-page vision. */
  app.post('/session/:id/image-region', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    try {
      const { bbox, quality } = req.body ?? {};
      if (!bbox || typeof bbox.x !== 'number' || typeof bbox.y !== 'number'
        || typeof bbox.w !== 'number' || typeof bbox.h !== 'number') {
        res.status(400).json({ error: 'bbox must be {x, y, w, h} with numeric values' });
        return;
      }
      const base64 = await page.getImageRegion(bbox, { quality });
      res.json({ success: true, base64, bbox });
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
      const mutates = Boolean(req.body.mutates);
      const scriptResult = await runPuppeteerScript(
        rawPage, code, context, timeout, { mutates },
      );
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

    const effectBefore: EffectSnapshot = await captureEffect(page.getRawPage());
    try {
      const { index, value } = req.body;
      const state = await page.getState({ useVision: false });
      const element = state.selectorMap.get(index);
      if (!element) { res.status(400).json({ error: `Element [${index}] not found` }); return; }
      await page.selectOption(element, value);
      await settleForEffect(page.getRawPage());
      const effectAfter = await captureEffect(page.getRawPage());
      const newState = await page.getState({ useVision: false });
      res.json({
        success: true,
        elements: newState.elementTree.clickableElementsToString(),
        effect: diffEffect(effectBefore, effectAfter),
      });
    } catch (err) {
      handleError(res, err);
    }
  });

  /**
   * Select a dropdown option by *label text* (not DOM index).
   *
   * Handles native <select>, ARIA combobox+listbox, Headless-UI Listbox,
   * and similar custom widgets. The agent passes a human-readable label
   * (e.g. "Brand") and value (e.g. "Dell"); the server resolves the
   * trigger via the accessibility tree, opens the listbox if needed,
   * fuzzy-matches the option, clicks it with real Puppeteer events, and
   * verifies the trigger now reflects the pick. On ambiguity it returns
   * a candidate list instead of guessing — preventing the "wrong dropdown
   * picked, regress, retry" hallucination loop on cascading filter forms.
   */
  app.post('/session/:id/select_option', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    const {
      label, value, fuzzy, timeout,
      extra_option_selectors,
      bbox, expected_label,
      in_iframe: inIframe,
    } = req.body || {};
    // Either label OR bbox must be supplied. When bbox is present we
    // dispatch to the vision-bbox path which sidesteps DOM-text
    // ambiguity entirely.
    const hasBbox = (
      bbox != null && typeof bbox === 'object'
      && typeof bbox.x0 === 'number' && typeof bbox.y0 === 'number'
      && typeof bbox.x1 === 'number' && typeof bbox.y1 === 'number'
    );
    const hasIframeHost = typeof inIframe === 'string' && inIframe.length > 0;
    if (!hasBbox && (typeof label !== 'string' || !label.trim())) {
      res.status(400).json({ error: 'label (string) or bbox is required' });
      return;
    }
    if (typeof value !== 'string' || !value.trim()) {
      res.status(400).json({ error: 'value (string) is required' });
      return;
    }
    // Phase F: in_iframe wins when set — bbox/label fall through to
    // selectOptionInIframe regardless. The iframe path supports
    // label-based resolution only for v1 (native <select> only).
    if (hasIframeHost && !label) {
      res.status(400).json({
        error: 'label is required when in_iframe is provided'
              + ' (iframe path resolves selects by label only in v1)',
      });
      return;
    }

    const effectBefore: EffectSnapshot = await captureEffect(page.getRawPage());
    try {
      const result = hasIframeHost
        ? await selectOptionInIframe(page.getRawPage(), {
            iframeHost: inIframe,
            label,
            value,
            fuzzy: fuzzy !== false,
            timeout: typeof timeout === 'number' ? timeout : undefined,
          })
        : hasBbox
        ? await selectOptionByVisionBbox(page.getRawPage(), {
            bbox: {
              x0: Number(bbox.x0), y0: Number(bbox.y0),
              x1: Number(bbox.x1), y1: Number(bbox.y1),
            },
            expectedLabel: typeof expected_label === 'string' ? expected_label : undefined,
            value,
            fuzzy: fuzzy !== false,
            timeout: typeof timeout === 'number' ? timeout : undefined,
            extraOptionSelectors: Array.isArray(extra_option_selectors) ? extra_option_selectors : undefined,
          })
        : await selectOptionByLabel(page.getRawPage(), {
            label,
            value,
            fuzzy: fuzzy !== false,
            timeout: typeof timeout === 'number' ? timeout : undefined,
            extraOptionSelectors: Array.isArray(extra_option_selectors) ? extra_option_selectors : undefined,
          });
      await settleForEffect(page.getRawPage());
      const effectAfter = await captureEffect(page.getRawPage());
      const newState = await page.getState({ useVision: false }).catch(() => null);
      res.json({
        ...result,
        // Mirror `ok` to `success` so HTTP-status-only callers get the
        // right signal. Bridge code already keys on `data.ok`; this is
        // a no-op for it and a correctness fix for everyone else.
        success: result.ok === true,
        elements: newState ? newState.elementTree.clickableElementsToString() : undefined,
        effect: diffEffect(effectBefore, effectAfter),
        // Fresh fingerprints — Python's element_fingerprints cache stays
        // in sync after select_option mutates the DOM (filter applied,
        // listbox re-rendered, etc.).
        fingerprints: newState
          ? fingerprintMap(newState.selectorMap, newState.selectorEntries)
          : undefined,
      });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Extract page content as markdown.
   *
   * Optional `?include_anchors=true` annotates each heading with
   * `[@y=N]` (its absolute scroll-Y position) and appends a trailing
   * `[OUTLINE scrollY=N scrollHeight=H vp=V]` line. Used by the brain
   * to scroll approximately to a named section: read anchors, compute
   * `pixels = anchor_y - scrollY`, call `browser_scroll(pixels=…)`,
   * then let vision finish the fine targeting.
   */
  app.get('/session/:id/markdown', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    try {
      const includeAnchors =
        req.query.include_anchors === 'true' || req.query.include_anchors === '1';
      const markdown = await page.getMarkdownContent({ includeAnchors });
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

/**
 * Repair brain-emitted CSS selectors with unescaped React `useId()` IDs.
 *
 * React 18's `useId()` produces IDs like `:r13:`. In CSS these must be
 * backslash-escaped (`\:`) or `document.querySelector` parses the colons
 * as pseudo-classes and the call silently matches nothing — the exact
 * shape of the "radix selector failed for no reason" bug.
 *
 * Idempotent: the regex requires the `:` to immediately follow `#` or an
 * ID prefix (no backslash in between), so already-escaped selectors like
 * `#radix-\:r13\:` pass through unchanged.
 */
export function normalizeIdSelector(sel: string): string {
  return sel.replace(
    /#([a-zA-Z_][\w-]*)?(:r[a-z0-9]+:?)/g,
    (_m, prefix, idTail) => `#${prefix ?? ''}${idTail.replace(/:/g, '\\:')}`,
  );
}
