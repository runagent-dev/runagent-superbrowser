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
import { tokenAuth, validateUrl, isValidSessionId, RateLimiter } from './auth.js';
import { runPuppeteerScript } from '../browser/script-runner.js';
import { ProxyPool } from '../browser/proxy-pool.js';

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
  const rateLimiter = new RateLimiter(
    parseInt(process.env.RATE_LIMIT || '200', 10),
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

  // Session cleanup loop — expire idle and old sessions
  setInterval(() => {
    const now = Date.now();
    for (const [id, session] of sessions) {
      const idle = now - session.lastAccessed > SESSION_IDLE_TIMEOUT;
      const expired = now - session.createdAt > SESSION_MAX_LIFETIME;
      if (idle || expired) {
        session.page.close().catch(() => {});
        sessions.delete(id);
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
      sessions.set(id, { page, createdAt: Date.now(), lastAccessed: Date.now() });

      if (req.body.url) {
        await page.navigate(req.body.url);
      }

      const wantVision = req.body.vision !== false;
      const state = await page.getState({ useVision: wantVision, includeConsole: true });

      res.json({
        sessionId: id,
        url: state.url,
        title: state.title,
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
      await page.navigate(url);
      const wantVision = req.body.vision !== false;
      const state = await page.getState({ useVision: wantVision, includeConsole: true });
      res.json({
        url: state.url,
        title: state.title,
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
      const state = await page.getState({
        useVision,
        includeConsole: true,
        includeAccessibility: req.query.accessibility === 'true',
        includeCursorElements: req.query.cursor === 'true',
      });
      res.json({
        url: state.url,
        title: state.title,
        screenshot: useVision ? state.screenshot : undefined,
        elements: state.elementTree.clickableElementsToString(),
        scrollInfo: { scrollY: state.scrollY, scrollHeight: state.scrollHeight, viewportHeight: state.viewportHeight },
        accessibilityTree: state.accessibilityTree,
        consoleErrors: state.consoleErrors,
        pendingDialogs: state.pendingDialogs,
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
      const { index, x, y, button, clickCount } = req.body;

      if (x !== undefined && y !== undefined) {
        await page.clickAt(x, y, { button, clickCount });
      } else if (index !== undefined) {
        const state = await page.getState({ useVision: false });
        const element = state.selectorMap.get(index);
        if (!element) { res.status(400).json({ error: `Element [${index}] not found` }); return; }
        await page.clickElement(element, { button, clickCount });
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
      const { index, text, clear } = req.body;
      if (index === undefined || !text) {
        res.status(400).json({ error: 'index and text required' });
        return;
      }

      const state = await page.getState({ useVision: false });
      const element = state.selectorMap.get(index);
      if (!element) { res.status(400).json({ error: `Element [${index}] not found` }); return; }

      await page.typeText(element, text, clear !== false);

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

  /** Close a session. */
  app.delete('/session/:id', async (req, res) => {
    const page = getSession(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found or expired' }); return; }

    try {
      await page.close();
      sessions.delete(req.params.id);
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
      res.json({ captcha });
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

      const result = await solveCaptchaFull(
        page.getRawPage(),
        captcha,
        config,
        llm, // LLM provider for AI vision (may be null)
        method,
      );

      res.json({ ...result, captcha });
    } catch (err) {
      handleError(res, err);
    }
  });

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
