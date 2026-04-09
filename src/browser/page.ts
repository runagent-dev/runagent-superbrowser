/**
 * Page wrapper with high-level browser operations.
 *
 * Combines patterns from browserless (screenshot/navigation),
 * nanobrowser (getState, clickElementNode, inputTextElementNode),
 * and BrowserOS (dialog handling, console capture, file upload, PDF).
 */

import type { Page, CDPSession } from 'puppeteer-core';
import type { BrowserConfig } from './engine.js';
import { buildDomTree, type PageState, type DialogInfo, type DOMElementNode } from './dom.js';
import { getAccessibilitySnapshot } from './accessibility.js';
import { dispatchClick, dispatchHover, dispatchDrag, dispatchScroll } from './input-mouse.js';
import { typeText as cdpTypeText, pressKeyCombo, clearField } from './input-keyboard.js';
import { getElementCenterBySelector } from './elements.js';
import { findCursorInteractiveElements, formatCursorElements } from './cursor-detect.js';
import { ConsoleCollector, type CollectedLog } from './console-collector.js';
import { DownloadMonitor } from './download-monitor.js';

export class PageWrapper {
  private cdpClient: CDPSession | null = null;
  private pendingDialogs: DialogInfo[] = [];
  private dialogHandlerSetup = false;
  private consoleCollector = new ConsoleCollector();
  private downloadMonitor = new DownloadMonitor();

  constructor(
    private page: Page,
    private config: BrowserConfig,
  ) {}

  /** Get the underlying puppeteer Page. */
  getRawPage(): Page {
    return this.page;
  }

  // --- Navigation ---

  async navigate(url: string, timeout: number = 30000): Promise<void> {
    await this.page.goto(url, {
      waitUntil: 'domcontentloaded',
      timeout,
    });
    // Wait a bit for dynamic content
    await this.waitForIdle(2000).catch(() => {});
  }

  async goBack(): Promise<void> {
    await this.page.goBack({ waitUntil: 'domcontentloaded', timeout: 10000 });
    await this.waitForIdle(1500).catch(() => {});
  }

  async getUrl(): Promise<string> {
    return this.page.url();
  }

  async getTitle(): Promise<string> {
    return this.page.title();
  }

  // --- Screenshots ---

  async screenshot(quality: number = 70): Promise<Buffer> {
    return (await this.page.screenshot({
      type: 'jpeg',
      quality,
      fullPage: false,
    })) as Buffer;
  }

  async screenshotBase64(quality: number = 70): Promise<string> {
    const buffer = await this.screenshot(quality);
    return buffer.toString('base64');
  }

  // --- Element interaction ---

  /**
   * Click an element using 3-tier fallback (from BrowserOS):
   * 1. CDP Input.dispatchMouseEvent at element center coordinates
   * 2. Puppeteer page.click() with CSS selector
   * 3. JS click() via XPath evaluation
   */
  async clickElement(element: DOMElementNode, options?: {
    button?: 'left' | 'right' | 'middle';
    clickCount?: number;
  }): Promise<void> {
    const selector = element.enhancedCssSelectorForElement();

    // Tier 1: CDP mouse dispatch at computed coordinates
    try {
      const coords = await getElementCenterBySelector(this.page, selector);
      if (coords) {
        const client = await this.getCDPSession();
        await dispatchClick(client, coords.x, coords.y, {
          button: options?.button,
          clickCount: options?.clickCount,
        });
        await this.waitForIdle(1500).catch(() => {});
        return;
      }
    } catch {
      // Fallthrough
    }

    // Tier 2: Puppeteer click
    try {
      await this.page.waitForSelector(selector, { timeout: 5000 });
      await this.page.click(selector);
      await this.waitForIdle(1500).catch(() => {});
      return;
    } catch {
      // Fallthrough
    }

    // Tier 3: JS fallback via XPath
    if (element.xpath) {
      await this.page.evaluate((xpath: string) => {
        const result = document.evaluate(
          xpath, document, null,
          XPathResult.FIRST_ORDERED_NODE_TYPE, null,
        );
        const el = result.singleNodeValue as HTMLElement;
        if (el) {
          el.scrollIntoView({ block: 'center' });
          el.click();
        }
      }, element.xpath);
    }
    await this.waitForIdle(1500).catch(() => {});
  }

  /** Click at specific page coordinates (from BrowserOS click_at). */
  async clickAt(x: number, y: number, options?: {
    button?: 'left' | 'right' | 'middle';
    clickCount?: number;
  }): Promise<void> {
    const client = await this.getCDPSession();
    await dispatchClick(client, x, y, options);
    await this.waitForIdle(1000).catch(() => {});
  }

  /** Hover over an element (from BrowserOS hover). */
  async hoverElement(element: DOMElementNode): Promise<void> {
    const selector = element.enhancedCssSelectorForElement();
    const coords = await getElementCenterBySelector(this.page, selector);
    if (coords) {
      const client = await this.getCDPSession();
      await dispatchHover(client, coords.x, coords.y);
    } else {
      await this.page.hover(selector);
    }
  }

  /** Hover at specific coordinates (from BrowserOS hover_at). */
  async hoverAt(x: number, y: number): Promise<void> {
    const client = await this.getCDPSession();
    await dispatchHover(client, x, y);
  }

  /** Drag from one element to another or to coordinates (from BrowserOS drag). */
  async dragTo(
    startX: number, startY: number,
    endX: number, endY: number,
    options?: { steps?: number },
  ): Promise<void> {
    const client = await this.getCDPSession();
    await dispatchDrag(client, startX, startY, endX, endY, options);
  }

  /** Scroll within a specific element or the page (from BrowserOS scroll). */
  async scrollElement(
    element: DOMElementNode | null,
    direction: 'up' | 'down' | 'left' | 'right',
    amount: number = 300,
  ): Promise<void> {
    const client = await this.getCDPSession();

    let x = this.config.viewport.width / 2;
    let y = this.config.viewport.height / 2;

    if (element) {
      const selector = element.enhancedCssSelectorForElement();
      const coords = await getElementCenterBySelector(this.page, selector);
      if (coords) {
        x = coords.x;
        y = coords.y;
      }
    }

    const deltaX = direction === 'left' ? -amount : direction === 'right' ? amount : 0;
    const deltaY = direction === 'up' ? -amount : direction === 'down' ? amount : 0;

    await dispatchScroll(client, x, y, deltaX, deltaY);
    await new Promise((r) => setTimeout(r, 300));
  }

  /**
   * Type text into an element using CDP keyboard dispatch.
   * Smart field clearing: click → Ctrl+A → Backspace (from BrowserOS fill pattern).
   */
  async typeText(element: DOMElementNode, text: string, clear: boolean = true): Promise<void> {
    const selector = element.enhancedCssSelectorForElement();

    try {
      // Focus the element by clicking it
      const coords = await getElementCenterBySelector(this.page, selector);
      const client = await this.getCDPSession();

      if (coords) {
        await dispatchClick(client, coords.x, coords.y);
        await new Promise((r) => setTimeout(r, 100));

        // Clear existing content (BrowserOS clearField pattern)
        if (clear) {
          await clearField(client, coords.x, coords.y);
          await new Promise((r) => setTimeout(r, 50));
        }

        // Type via CDP keyboard dispatch
        await cdpTypeText(client, text, 30);
      } else {
        // Fallback: puppeteer type
        await this.page.waitForSelector(selector, { timeout: 5000 });
        if (clear) {
          await this.page.click(selector, { clickCount: 3 });
          await this.page.keyboard.press('Backspace');
        }
        await this.page.type(selector, text, { delay: 30 });
      }
    } catch {
      // Last resort: JS value assignment
      if (element.xpath) {
        await this.page.evaluate((xpath: string, inputText: string) => {
          const result = document.evaluate(
            xpath, document, null,
            XPathResult.FIRST_ORDERED_NODE_TYPE, null,
          );
          const el = result.singleNodeValue as HTMLInputElement;
          if (el) {
            el.scrollIntoView({ block: 'center' });
            el.focus();
            el.value = '';
            el.value = inputText;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
          }
        }, element.xpath, text);
      }
    }
  }

  async selectOption(element: DOMElementNode, value: string): Promise<void> {
    const selector = element.enhancedCssSelectorForElement();
    await this.page.select(selector, value);
  }

  /**
   * Send keyboard keys via CDP dispatch.
   * Supports combos like "Control+A", "Meta+Shift+P", special keys, etc.
   * Pattern from BrowserOS keyboard.ts pressKeyCombo.
   */
  async sendKeys(keys: string): Promise<void> {
    const client = await this.getCDPSession();
    await pressKeyCombo(client, keys);
  }

  // --- Scrolling ---

  async scrollPage(direction: 'up' | 'down'): Promise<void> {
    const viewportHeight = this.config.viewport.height;
    const distance = direction === 'down' ? viewportHeight - 100 : -(viewportHeight - 100);
    await this.page.evaluate((d: number) => {
      window.scrollBy(0, d);
    }, distance);
    await new Promise((r) => setTimeout(r, 500));
  }

  async scrollToPercent(percent: number): Promise<void> {
    await this.page.evaluate((pct: number) => {
      const maxScroll = document.documentElement.scrollHeight - window.innerHeight;
      window.scrollTo(0, Math.round(maxScroll * pct / 100));
    }, percent);
    await new Promise((r) => setTimeout(r, 500));
  }

  async getScrollInfo(): Promise<[number, number, number]> {
    return this.page.evaluate(() => [
      Math.round(window.scrollY),
      Math.round(window.innerHeight),
      Math.round(document.documentElement.scrollHeight),
    ]) as Promise<[number, number, number]>;
  }

  // --- State ---

  async getState(options: {
    useVision?: boolean;
    includeAccessibility?: boolean;
    includeConsole?: boolean;
    includeCursorElements?: boolean;
  } = {}): Promise<PageState> {
    const {
      useVision = true,
      includeAccessibility = false,
      includeConsole = true,
      includeCursorElements = false,
    } = options;

    const domResult = await buildDomTree(this.page);

    let screenshot: string | undefined;
    if (useVision) {
      screenshot = await this.screenshotBase64();
    }

    let accessibilityTree: string | undefined;
    if (includeAccessibility) {
      try {
        accessibilityTree = await getAccessibilitySnapshot(this.page);
      } catch {
        // AX tree not available
      }
    }

    // FROM BROWSEROS: Detect cursor-interactive elements the DOM tree misses
    if (includeCursorElements) {
      try {
        const cursorElements = await findCursorInteractiveElements(this.page);
        if (cursorElements.length > 0) {
          const formatted = formatCursorElements(cursorElements);
          accessibilityTree = (accessibilityTree || '') + formatted;
        }
      } catch {
        // Cursor detection is best-effort
      }
    }

    const pendingDialogs = this.pendingDialogs.length > 0
      ? [...this.pendingDialogs]
      : undefined;

    let consoleErrors: string[] | undefined;
    if (includeConsole) {
      const errors = this.consoleCollector.getErrors(5);
      if (errors.length > 0) {
        consoleErrors = errors.map((e) => e.text);
      }
    }

    return {
      ...domResult,
      screenshot,
      accessibilityTree,
      pendingDialogs,
      consoleErrors,
    };
  }

  /** Enable download monitoring via CDP events (from BrowserOS). */
  async enableDownloadMonitor(): Promise<void> {
    const client = await this.getCDPSession();
    await this.downloadMonitor.enable(client, this.config.downloadDir);
  }

  /** Wait for a download to complete. */
  async waitForDownload(timeout?: number): Promise<{ filename: string; url: string } | null> {
    const result = await this.downloadMonitor.waitForDownload(timeout);
    if (!result) return null;
    return { filename: result.suggestedFilename, url: result.url };
  }

  /** Get link URLs from the page (from BrowserOS snapshot.ts link extraction). */
  async extractLinks(): Promise<Array<{ text: string; href: string }>> {
    return this.page.evaluate(() => {
      const links: Array<{ text: string; href: string }> = [];
      const seen = new Set<string>();
      document.querySelectorAll('a[href]').forEach((a) => {
        const href = (a as HTMLAnchorElement).href;
        if (!href || href.startsWith('javascript:') || seen.has(href)) return;
        seen.add(href);
        const text = (a.textContent || '').trim().substring(0, 100);
        links.push({ text, href });
      });
      return links;
    });
  }

  // --- FROM BROWSEROS: Dialog handling ---

  async setupDialogHandler(): Promise<void> {
    if (this.dialogHandlerSetup) return;
    this.page.on('dialog', async (dialog) => {
      this.pendingDialogs.push({
        type: dialog.type(),
        message: dialog.message(),
        defaultValue: dialog.defaultValue(),
      });
    });
    this.dialogHandlerSetup = true;
  }

  async handleDialog(accept: boolean, text?: string): Promise<void> {
    const dialog = this.pendingDialogs.shift();
    if (!dialog) return;

    // The dialog should still be active on the page
    // We need to handle it through the page's dialog event
    // Since puppeteer auto-dismisses after 200ms, we listen before action
    this.page.once('dialog', async (d) => {
      if (accept) {
        await d.accept(text);
      } else {
        await d.dismiss();
      }
    });
  }

  getPendingDialogs(): DialogInfo[] {
    return [...this.pendingDialogs];
  }

  // --- Console capture (enhanced from BrowserOS console-collector) ---

  async enableConsoleCapture(): Promise<void> {
    const client = await this.getCDPSession();
    await this.consoleCollector.enable(client);
  }

  getConsoleMessages(filter?: 'error' | 'warning'): CollectedLog[] {
    return this.consoleCollector.getLogs({ level: filter });
  }

  /** Search console logs by text (from BrowserOS). */
  searchConsoleLogs(search: string, limit?: number): CollectedLog[] {
    return this.consoleCollector.getLogs({ search, limit });
  }

  // --- FROM BROWSEROS: File upload ---

  async uploadFile(element: DOMElementNode, filePaths: string[]): Promise<void> {
    const selector = element.enhancedCssSelectorForElement();
    const fileInput = await this.page.$(selector);
    if (!fileInput) throw new Error('File input element not found');
    await (fileInput as any).uploadFile(...filePaths);
  }

  // --- FROM BROWSEROS: PDF export ---

  async exportPdf(options?: {
    format?: 'A4' | 'Letter';
    printBackground?: boolean;
  }): Promise<Buffer> {
    return (await this.page.pdf({
      format: options?.format || 'A4',
      printBackground: options?.printBackground ?? true,
      margin: { top: '10mm', bottom: '10mm', left: '10mm', right: '10mm' },
    })) as Buffer;
  }

  // --- FROM BROWSEROS: Content extraction to markdown ---

  async getMarkdownContent(): Promise<string> {
    return this.page.evaluate(() => {
      // Simple markdown extraction from the page
      const clone = document.body.cloneNode(true) as HTMLElement;

      // Remove scripts, styles, nav, footer, etc.
      const removeSelectors = 'script, style, noscript, nav, footer, header, aside, [role="navigation"], [role="banner"], [aria-hidden="true"]';
      clone.querySelectorAll(removeSelectors).forEach((el) => el.remove());

      // Convert links
      clone.querySelectorAll('a').forEach((a) => {
        const text = a.textContent?.trim() || '';
        const href = a.getAttribute('href') || '';
        if (text && href) {
          a.textContent = `[${text}](${href})`;
        }
      });

      // Convert headings
      for (let i = 1; i <= 6; i++) {
        clone.querySelectorAll(`h${i}`).forEach((h) => {
          const text = h.textContent?.trim() || '';
          if (text) {
            h.textContent = '\n' + '#'.repeat(i) + ' ' + text + '\n';
          }
        });
      }

      // Convert lists
      clone.querySelectorAll('li').forEach((li) => {
        const text = li.textContent?.trim() || '';
        if (text) {
          li.textContent = '- ' + text;
        }
      });

      // Get text and normalize whitespace
      let text = clone.innerText || clone.textContent || '';
      text = text.replace(/[ \t]+/g, ' ');
      text = text.replace(/\n{3,}/g, '\n\n');
      return text.trim().substring(0, 50000);
    });
  }

  // --- FROM BROWSEROS: DOM search ---

  async domSearch(selector: string): Promise<string[]> {
    return this.page.evaluate((sel: string) => {
      const elements = document.querySelectorAll(sel);
      return Array.from(elements).map((el) => {
        const text = (el as HTMLElement).innerText || el.textContent || '';
        return text.trim().substring(0, 200);
      }).filter(Boolean);
    }, selector);
  }

  // --- FROM BROWSEROS: Custom wait conditions ---

  async waitForCondition(jsExpression: string, timeout: number = 10000): Promise<boolean> {
    try {
      await this.page.waitForFunction(jsExpression, { timeout });
      return true;
    } catch {
      return false;
    }
  }

  // --- FROM BROWSEROS: Evaluate arbitrary script ---

  async evaluateScript(script: string): Promise<unknown> {
    return this.page.evaluate(script);
  }

  // --- Waiting ---

  async waitForIdle(timeout: number = 3000): Promise<void> {
    try {
      await this.page.waitForNetworkIdle({ idleTime: 500, timeout });
    } catch {
      // Timeout is acceptable
    }
  }

  // --- Lifecycle ---

  async close(): Promise<void> {
    try {
      await this.page.close();
    } catch {
      // Page may already be closed
    }
  }

  /** Get or create CDP session for low-level operations. */
  async getCDPSession(): Promise<CDPSession> {
    if (!this.cdpClient) {
      this.cdpClient = await this.page.createCDPSession();
    }
    return this.cdpClient;
  }
}
