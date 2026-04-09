/**
 * Lifecycle hooks system from browserless.
 *
 * Four hook points:
 * - before: pre-request (return false to reject)
 * - after: post-request (timing, status, error info)
 * - page: new page creation
 * - browser: new browser launch
 */

import { EventEmitter } from 'events';
import type { IncomingMessage, ServerResponse } from 'http';
import type { Page, Browser } from 'puppeteer-core';

export interface BeforeRequestArgs {
  req: IncomingMessage;
  res?: ServerResponse;
}

export interface AfterResponseArgs {
  req: IncomingMessage;
  start: number;
  status: 'successful' | 'error' | 'timedout';
  error?: Error;
}

export interface PageHookArgs {
  meta: { url: string };
  page: Page;
}

export interface BrowserHookArgs {
  browser: Browser;
  req?: IncomingMessage;
}

export class Hooks extends EventEmitter {
  private beforeFns: Array<(args: BeforeRequestArgs) => Promise<boolean>> = [];
  private afterFns: Array<(args: AfterResponseArgs) => Promise<void>> = [];
  private pageFns: Array<(args: PageHookArgs) => Promise<void>> = [];
  private browserFns: Array<(args: BrowserHookArgs) => Promise<void>> = [];

  /** Register a before-request hook. Return false to reject the request. */
  onBefore(fn: (args: BeforeRequestArgs) => Promise<boolean>): void {
    this.beforeFns.push(fn);
  }

  /** Register an after-request hook. */
  onAfter(fn: (args: AfterResponseArgs) => Promise<void>): void {
    this.afterFns.push(fn);
  }

  /** Register a page-creation hook. */
  onPage(fn: (args: PageHookArgs) => Promise<void>): void {
    this.pageFns.push(fn);
  }

  /** Register a browser-launch hook. */
  onBrowser(fn: (args: BrowserHookArgs) => Promise<void>): void {
    this.browserFns.push(fn);
  }

  /** Run before hooks. Returns false if any hook rejects. */
  async before(args: BeforeRequestArgs): Promise<boolean> {
    for (const fn of this.beforeFns) {
      const result = await fn(args);
      if (result === false) return false;
    }
    return true;
  }

  /** Run after hooks. */
  async after(args: AfterResponseArgs): Promise<void> {
    for (const fn of this.afterFns) {
      try {
        await fn(args);
      } catch (err) {
        console.error('After hook error:', err);
      }
    }
  }

  /** Run page hooks. */
  async page(args: PageHookArgs): Promise<void> {
    for (const fn of this.pageFns) {
      try {
        await fn(args);
      } catch (err) {
        console.error('Page hook error:', err);
      }
    }
  }

  /** Run browser hooks. */
  async browser(args: BrowserHookArgs): Promise<void> {
    for (const fn of this.browserFns) {
      try {
        await fn(args);
      } catch (err) {
        console.error('Browser hook error:', err);
      }
    }
  }
}
