/**
 * Scraping with debug data from browserless.
 *
 * Extracts elements by selector with full attribute/position info,
 * captures network requests, console messages, and cookies.
 */

import type { Page, HTTPRequest, HTTPResponse } from 'puppeteer-core';

export interface ScrapeElementSelector {
  /** CSS selector to find elements. */
  selector: string;
  /** Specific attributes to extract (default: all). */
  attributes?: string[];
  /** Timeout for waiting for selector (ms). */
  timeout?: number;
}

export interface ScrapedElement {
  attributes: Record<string, string>;
  height: number;
  html: string;
  left: number;
  text: string;
  top: number;
  width: number;
}

export interface NetworkEntry {
  url: string;
  method: string;
  status?: number;
  type: string;
  size?: number;
}

export interface ScrapeDebugInfo {
  console: string[];
  network: {
    inbound: NetworkEntry[];
    outbound: NetworkEntry[];
  };
  cookies: Array<{ name: string; value: string; domain: string }>;
  html: string;
  screenshot?: string; // base64 JPEG
}

export interface ScrapeResult {
  data: Array<{
    selector: string;
    results: ScrapedElement[];
  }>;
  debug?: ScrapeDebugInfo;
}

/**
 * Setup network and console capture for scraping debug.
 */
export function setupScrapeDebug(page: Page): {
  getDebugInfo: () => Promise<ScrapeDebugInfo>;
} {
  const consoleMessages: string[] = [];
  const inboundRequests: NetworkEntry[] = [];
  const outboundRequests: NetworkEntry[] = [];

  page.on('console', (msg) => {
    consoleMessages.push(`[${msg.type()}] ${msg.text()}`);
  });

  page.on('request', (req: HTTPRequest) => {
    outboundRequests.push({
      url: req.url(),
      method: req.method(),
      type: req.resourceType(),
    });
  });

  page.on('response', (res: HTTPResponse) => {
    inboundRequests.push({
      url: res.url(),
      method: res.request().method(),
      status: res.status(),
      type: res.request().resourceType(),
    });
  });

  return {
    getDebugInfo: async (): Promise<ScrapeDebugInfo> => {
      const cookies = await page.cookies();
      const html = await page.content();
      let screenshot: string | undefined;
      try {
        const buffer = await page.screenshot({ type: 'jpeg', quality: 70 });
        screenshot = Buffer.from(buffer).toString('base64');
      } catch {
        // Screenshot may fail
      }

      return {
        console: consoleMessages,
        network: {
          inbound: inboundRequests,
          outbound: outboundRequests,
        },
        cookies: cookies.map((c) => ({ name: c.name, value: c.value, domain: c.domain })),
        html: html.substring(0, 100000),
        screenshot,
      };
    },
  };
}

/**
 * Scrape elements from the page by CSS selectors.
 * Returns element data including attributes, text, HTML, and position.
 */
export async function scrapeElements(
  page: Page,
  elements: ScrapeElementSelector[],
  bestAttempt: boolean = false,
): Promise<Array<{ selector: string; results: ScrapedElement[] }>> {
  const results: Array<{ selector: string; results: ScrapedElement[] }> = [];

  for (const element of elements) {
    try {
      // Wait for selector
      await page.waitForSelector(element.selector, {
        timeout: element.timeout || 10000,
      });

      // Extract element data
      const scraped = await page.evaluate(
        (sel: string, attrs: string[] | undefined) => {
          const els = document.querySelectorAll(sel);
          return Array.from(els).map((el) => {
            const rect = el.getBoundingClientRect();
            const htmlEl = el as HTMLElement;

            // Get attributes
            const allAttrs: Record<string, string> = {};
            if (attrs && attrs.length > 0) {
              for (const attr of attrs) {
                const val = el.getAttribute(attr);
                if (val !== null) allAttrs[attr] = val;
              }
            } else {
              for (const attr of el.attributes) {
                allAttrs[attr.name] = attr.value;
              }
            }

            return {
              attributes: allAttrs,
              height: Math.round(rect.height),
              html: htmlEl.outerHTML.substring(0, 5000),
              left: Math.round(rect.left),
              text: (htmlEl.innerText || htmlEl.textContent || '').substring(0, 2000),
              top: Math.round(rect.top),
              width: Math.round(rect.width),
            };
          });
        },
        element.selector,
        element.attributes,
      );

      results.push({ selector: element.selector, results: scraped });
    } catch (err) {
      if (bestAttempt) {
        results.push({ selector: element.selector, results: [] });
      } else {
        throw err;
      }
    }
  }

  return results;
}
