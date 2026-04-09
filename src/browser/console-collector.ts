/**
 * Enhanced console collector from BrowserOS.
 *
 * Collects from Runtime.consoleAPICalled, Runtime.exceptionThrown,
 * and Log.entryAdded with level filtering and search.
 */

import type { CDPSession } from 'puppeteer-core';

export interface CollectedLog {
  level: 'error' | 'warning' | 'info' | 'debug' | 'log';
  text: string;
  source: 'console' | 'exception' | 'browser';
  timestamp: number;
  url?: string;
  lineNumber?: number;
}

export class ConsoleCollector {
  private logs: CollectedLog[] = [];
  private maxLogs: number;
  private enabled = false;

  constructor(maxLogs: number = 200) {
    this.maxLogs = maxLogs;
  }

  /** Enable console collection on a CDP session. */
  async enable(client: CDPSession): Promise<void> {
    if (this.enabled) return;

    // Enable required domains
    await Promise.all([
      client.send('Runtime.enable').catch(() => {}),
      client.send('Log.enable').catch(() => {}),
    ]);

    // Runtime.consoleAPICalled — standard console.log/warn/error/info
    client.on('Runtime.consoleAPICalled' as any, (params: any) => {
      const level = this.mapConsoleLevel(params.type);
      const text = (params.args || [])
        .map((arg: any) => arg.value ?? arg.description ?? '')
        .join(' ');

      this.addLog({
        level,
        text,
        source: 'console',
        timestamp: Date.now(),
      });
    });

    // Runtime.exceptionThrown — uncaught exceptions
    client.on('Runtime.exceptionThrown' as any, (params: any) => {
      const exception = params.exceptionDetails;
      const text = exception?.exception?.description ||
        exception?.text ||
        'Unknown exception';

      this.addLog({
        level: 'error',
        text,
        source: 'exception',
        timestamp: Date.now(),
        url: exception?.url,
        lineNumber: exception?.lineNumber,
      });
    });

    // Log.entryAdded — browser-level logs
    client.on('Log.entryAdded' as any, (params: any) => {
      const entry = params.entry;
      this.addLog({
        level: this.mapLogLevel(entry.level),
        text: entry.text || '',
        source: 'browser',
        timestamp: Date.now(),
        url: entry.url,
        lineNumber: entry.lineNumber,
      });
    });

    this.enabled = true;
  }

  /** Get logs filtered by level and/or search text. */
  getLogs(options?: {
    level?: 'error' | 'warning' | 'info' | 'debug';
    search?: string;
    limit?: number;
  }): CollectedLog[] {
    let filtered = this.logs;

    if (options?.level) {
      const level = options.level;
      filtered = filtered.filter((l) => l.level === level);
    }

    if (options?.search) {
      const search = options.search.toLowerCase();
      filtered = filtered.filter((l) => l.text.toLowerCase().includes(search));
    }

    const limit = options?.limit || 50;
    return filtered.slice(-limit);
  }

  /** Get error logs only. */
  getErrors(limit: number = 10): CollectedLog[] {
    return this.getLogs({ level: 'error', limit });
  }

  /** Clear all collected logs. */
  clear(): void {
    this.logs = [];
  }

  /** Get log count. */
  get count(): number {
    return this.logs.length;
  }

  private addLog(log: CollectedLog): void {
    this.logs.push(log);
    if (this.logs.length > this.maxLogs) {
      this.logs = this.logs.slice(-this.maxLogs);
    }
  }

  private mapConsoleLevel(type: string): CollectedLog['level'] {
    switch (type) {
      case 'error': return 'error';
      case 'warning': case 'warn': return 'warning';
      case 'info': return 'info';
      case 'debug': case 'trace': return 'debug';
      default: return 'log';
    }
  }

  private mapLogLevel(level: string): CollectedLog['level'] {
    switch (level) {
      case 'error': return 'error';
      case 'warning': return 'warning';
      case 'info': return 'info';
      case 'verbose': return 'debug';
      default: return 'log';
    }
  }
}
