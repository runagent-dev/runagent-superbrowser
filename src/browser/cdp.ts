/**
 * Type-safe CDP session wrapper.
 *
 * Direct CDP access for capabilities puppeteer doesn't expose natively.
 * Pattern from BrowserOS (browser.ts).
 */

import type { CDPSession } from 'puppeteer-core';

export interface CDPConsoleMessage {
  source: string;
  level: string;
  text: string;
  timestamp: number;
}

export class CDPWrapper {
  private consoleMessages: CDPConsoleMessage[] = [];
  private consoleEnabled = false;

  constructor(private client: CDPSession) {}

  // --- Accessibility ---

  async getAccessibilityTree(): Promise<unknown> {
    return this.client.send('Accessibility.getFullAXTree');
  }

  // --- Console ---

  async enableConsole(): Promise<void> {
    if (this.consoleEnabled) return;
    await this.client.send('Console.enable');
    this.client.on('Console.messageAdded', (params: { message: { source: string; level: string; text: string } }) => {
      this.consoleMessages.push({
        source: params.message.source,
        level: params.message.level,
        text: params.message.text,
        timestamp: Date.now(),
      });
      if (this.consoleMessages.length > 100) {
        this.consoleMessages = this.consoleMessages.slice(-100);
      }
    });
    this.consoleEnabled = true;
  }

  getConsoleMessages(): CDPConsoleMessage[] {
    return [...this.consoleMessages];
  }

  // --- Dialog handling ---

  async handleDialog(accept: boolean, promptText?: string): Promise<void> {
    await this.client.send('Page.handleJavaScriptDialog', {
      accept,
      promptText: promptText || '',
    });
  }

  // --- File upload ---

  async uploadFile(nodeId: number, filePaths: string[]): Promise<void> {
    await this.client.send('DOM.setFileInputFiles', {
      nodeId,
      files: filePaths,
    });
  }

  // --- PDF export ---

  async exportPdf(options?: {
    landscape?: boolean;
    printBackground?: boolean;
    paperWidth?: number;
    paperHeight?: number;
  }): Promise<Buffer> {
    const result = (await this.client.send('Page.printToPDF', {
      landscape: options?.landscape || false,
      printBackground: options?.printBackground ?? true,
      paperWidth: options?.paperWidth || 8.5,
      paperHeight: options?.paperHeight || 11,
      marginTop: 0.4,
      marginBottom: 0.4,
      marginLeft: 0.4,
      marginRight: 0.4,
    })) as { data: string };

    return Buffer.from(result.data, 'base64');
  }

  // --- Input dispatch ---

  async dispatchMouseEvent(
    type: 'mousePressed' | 'mouseReleased' | 'mouseMoved',
    x: number,
    y: number,
    button: 'left' | 'right' | 'middle' = 'left',
  ): Promise<void> {
    await this.client.send('Input.dispatchMouseEvent', {
      type,
      x,
      y,
      button,
      clickCount: type === 'mouseMoved' ? 0 : 1,
    });
  }

  async dispatchKeyEvent(
    type: 'keyDown' | 'keyUp' | 'char',
    key: string,
  ): Promise<void> {
    await this.client.send('Input.dispatchKeyEvent', {
      type,
      key,
      text: type === 'char' ? key : undefined,
    });
  }

  // --- Runtime evaluation ---

  async evaluate(expression: string): Promise<unknown> {
    const result = (await this.client.send('Runtime.evaluate', {
      expression,
      returnByValue: true,
    })) as { result: { value: unknown } };
    return result.result.value;
  }

  // --- Wait for condition ---

  async waitForCondition(
    jsExpression: string,
    timeout: number = 10000,
    pollInterval: number = 500,
  ): Promise<boolean> {
    const startTime = Date.now();
    while (Date.now() - startTime < timeout) {
      try {
        const result = await this.evaluate(jsExpression);
        if (result) return true;
      } catch {
        // Expression may throw, continue polling
      }
      await new Promise((r) => setTimeout(r, pollInterval));
    }
    return false;
  }

  /** Detach the CDP session. */
  async detach(): Promise<void> {
    try {
      await this.client.detach();
    } catch {
      // Already detached
    }
  }
}
