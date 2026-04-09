/**
 * HTTP API server for direct browser control.
 *
 * Full browserless-style API with all page setup options,
 * request interception, wait conditions, scraping, and function execution.
 */

import express from 'express';
import { Stream } from 'stream';
import type { BrowserEngine } from '../browser/engine.js';
import type { PageWrapper } from '../browser/page.js';
import type { LLMProvider } from '../llm/provider.js';
import { BrowserExecutor } from '../agent/executor.js';
import { goto, type NavigationOptions } from '../browser/goto.js';
import { scrapeElements, setupScrapeDebug, type ScrapeElementSelector } from '../browser/scraper.js';
import { Limiter } from '../browser/limiter.js';
import { BrowserError } from '../browser/errors.js';

export function createHttpServer(
  engine: BrowserEngine,
  llm: LLMProvider,
  limiterConfig?: { maxConcurrent?: number; maxQueued?: number; defaultTimeout?: number },
): express.Application {
  const app = express();
  app.use(express.json({ limit: '10mb' }));

  const limiter = new Limiter(limiterConfig);

  // CORS support (from browserless config)
  app.use((_req, res, next) => {
    if (process.env.CORS === 'true' || process.env.ENABLE_CORS === 'true') {
      res.header('Access-Control-Allow-Origin', process.env.CORS_ALLOW_ORIGIN || '*');
      res.header('Access-Control-Allow-Methods', process.env.CORS_ALLOW_METHODS || 'OPTIONS, POST, GET');
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

  app.post('/task', async (req, res) => {
    const { task, url, options } = req.body;
    if (!task) {
      res.status(400).json({ error: 'task is required' });
      return;
    }

    try {
      await limiter.submit(async () => {
        const page = await engine.newPage();
        if (url) await page.navigate(url);

        const executor = new BrowserExecutor(page, llm, options);
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

  // --- Function execution (run arbitrary puppeteer code) ---

  app.post('/function', async (req, res) => {
    const { code, context, url } = req.body;
    if (!code) {
      res.status(400).json({ error: 'code is required' });
      return;
    }

    try {
      await limiter.submit(async () => {
        const page = await engine.newPage();
        const rawPage = page.getRawPage();

        if (url) {
          await rawPage.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
        }

        // Create a function from the code string and execute it
        // The function receives { page, context } as arguments
        const fn = new Function('page', 'context', `return (async () => { ${code} })()`);
        const result = await fn(rawPage, context || {});

        await page.close();

        // Determine response type
        if (Buffer.isBuffer(result)) {
          res.set('Content-Type', 'application/octet-stream');
          res.send(result);
        } else if (typeof result === 'object') {
          res.json(result);
        } else {
          res.set('Content-Type', 'text/plain');
          res.send(String(result));
        }
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

  const sessions = new Map<string, PageWrapper>();
  let sessionCounter = 0;

  /** Create a new persistent session. */
  app.post('/session/create', async (req, res) => {
    try {
      const page = await engine.newPage();
      await page.setupDialogHandler();
      await page.enableConsoleCapture();
      await page.enableDownloadMonitor();

      const id = `session-${++sessionCounter}`;
      sessions.set(id, page);

      if (req.body.url) {
        await page.navigate(req.body.url);
      }

      const state = await page.getState({ useVision: true, includeConsole: true });

      res.json({
        sessionId: id,
        url: state.url,
        title: state.title,
        screenshot: state.screenshot,
        elements: state.elementTree.clickableElementsToString(),
        scrollInfo: { scrollY: state.scrollY, scrollHeight: state.scrollHeight, viewportHeight: state.viewportHeight },
      });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Navigate within a session. Returns screenshot + state. */
  app.post('/session/:id/navigate', async (req, res) => {
    const page = sessions.get(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found' }); return; }

    try {
      await page.navigate(req.body.url);
      const state = await page.getState({ useVision: true, includeConsole: true });
      res.json({
        url: state.url,
        title: state.title,
        screenshot: state.screenshot,
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
    const page = sessions.get(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found' }); return; }

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
    const page = sessions.get(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found' }); return; }

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
    const page = sessions.get(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found' }); return; }

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

      // Return updated state with screenshot
      const newState = await page.getState({ useVision: true, includeConsole: true });
      res.json({
        success: true,
        url: newState.url,
        title: newState.title,
        screenshot: newState.screenshot,
        elements: newState.elementTree.clickableElementsToString(),
        consoleErrors: newState.consoleErrors,
        pendingDialogs: newState.pendingDialogs,
      });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Type text into an element. Returns updated screenshot + state. */
  app.post('/session/:id/type', async (req, res) => {
    const page = sessions.get(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found' }); return; }

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

      const newState = await page.getState({ useVision: true, includeConsole: true });
      res.json({
        success: true,
        screenshot: newState.screenshot,
        elements: newState.elementTree.clickableElementsToString(),
      });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Send keyboard keys. Returns updated state. */
  app.post('/session/:id/keys', async (req, res) => {
    const page = sessions.get(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found' }); return; }

    try {
      await page.sendKeys(req.body.keys);
      await new Promise((r) => setTimeout(r, 500));

      const newState = await page.getState({ useVision: true });
      res.json({
        success: true,
        screenshot: newState.screenshot,
        elements: newState.elementTree.clickableElementsToString(),
      });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Scroll the page. Returns updated state. */
  app.post('/session/:id/scroll', async (req, res) => {
    const page = sessions.get(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found' }); return; }

    try {
      const { direction, percent } = req.body;
      if (percent !== undefined) {
        await page.scrollToPercent(percent);
      } else {
        await page.scrollPage(direction || 'down');
      }

      const newState = await page.getState({ useVision: true });
      res.json({
        success: true,
        screenshot: newState.screenshot,
        elements: newState.elementTree.clickableElementsToString(),
        scrollInfo: { scrollY: newState.scrollY, scrollHeight: newState.scrollHeight, viewportHeight: newState.viewportHeight },
      });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Execute JavaScript in the session page. */
  app.post('/session/:id/evaluate', async (req, res) => {
    const page = sessions.get(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found' }); return; }

    try {
      const result = await page.evaluateScript(req.body.script);
      res.json({ result });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Handle a pending dialog. */
  app.post('/session/:id/dialog', async (req, res) => {
    const page = sessions.get(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found' }); return; }

    try {
      await page.handleDialog(req.body.accept, req.body.text);
      res.json({ success: true });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Select a dropdown option. */
  app.post('/session/:id/select', async (req, res) => {
    const page = sessions.get(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found' }); return; }

    try {
      const { index, value } = req.body;
      const state = await page.getState({ useVision: false });
      const element = state.selectorMap.get(index);
      if (!element) { res.status(400).json({ error: `Element [${index}] not found` }); return; }
      await page.selectOption(element, value);

      const newState = await page.getState({ useVision: true });
      res.json({ success: true, screenshot: newState.screenshot });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Extract page content as markdown. */
  app.get('/session/:id/markdown', async (req, res) => {
    const page = sessions.get(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found' }); return; }

    try {
      const markdown = await page.getMarkdownContent();
      res.json({ content: markdown });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** Export current page as PDF. */
  app.get('/session/:id/pdf', async (req, res) => {
    const page = sessions.get(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found' }); return; }

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
    const page = sessions.get(req.params.id);
    if (!page) { res.status(404).json({ error: 'Session not found' }); return; }

    try {
      await page.close();
      sessions.delete(req.params.id);
      res.json({ success: true });
    } catch (err) {
      handleError(res, err);
    }
  });

  /** List active sessions. */
  app.get('/sessions', (_req, res) => {
    const list = Array.from(sessions.keys());
    res.json({ sessions: list, count: list.length });
  });

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
