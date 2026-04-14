/**
 * Cross-session handoff ledger.
 *
 * When a human clears a captcha via the handoff strategy, we record
 * `{sessionId, taskId, domain, solvedAt}` so a *subsequent* session for the
 * same task on the same domain doesn't ask the user again. Cookies (and
 * the orchestrator's warm-up step) usually carry the solve forward, but
 * cookies expire/lose sometimes — this ledger is the belt-and-suspenders
 * that says "the user already solved this within the last 15 min, don't
 * re-prompt them just because the cookie didn't ride along."
 *
 * Storage: `~/.superbrowser/handoff-ledger.json` (overridable via env).
 * Schema: `{ "<scope>": { "<domain>": <unix-ms> } }` where `<scope>` is
 * the taskId when one was supplied, otherwise the sessionId. Single-entry
 * per (scope, domain) — overwriting on re-solve is fine.
 *
 * Skipping read/write errors is intentional: the ledger is an
 * optimization, not a safety property.
 */

import fs from 'fs';
import os from 'os';
import path from 'path';

const RECENT_WINDOW_MS = 15 * 60 * 1000;
const FLUSH_DEBOUNCE_MS = 1000;

function defaultLedgerPath(): string {
  if (process.env.SUPERBROWSER_HANDOFF_LEDGER) return process.env.SUPERBROWSER_HANDOFF_LEDGER;
  return path.join(os.homedir(), '.superbrowser', 'handoff-ledger.json');
}

export class HandoffLedger {
  private data: Record<string, Record<string, number>> = {};
  private loaded = false;
  private flushTimer: NodeJS.Timeout | null = null;
  private filePath: string;

  constructor(filePath: string = defaultLedgerPath()) {
    this.filePath = filePath;
  }

  private ensureLoaded(): void {
    if (this.loaded) return;
    this.loaded = true;
    try {
      if (fs.existsSync(this.filePath)) {
        const parsed = JSON.parse(fs.readFileSync(this.filePath, 'utf8'));
        if (parsed && typeof parsed === 'object') {
          this.data = parsed as Record<string, Record<string, number>>;
        }
      }
    } catch { /* corrupt — start fresh */ }
  }

  private scheduleFlush(): void {
    if (this.flushTimer) return;
    this.flushTimer = setTimeout(() => {
      this.flushTimer = null;
      try {
        fs.mkdirSync(path.dirname(this.filePath), { recursive: true });
        fs.writeFileSync(this.filePath, JSON.stringify(this.data, null, 2));
      } catch { /* best-effort */ }
    }, FLUSH_DEBOUNCE_MS);
    this.flushTimer.unref?.();
  }

  /** Record a successful human-handoff against a (scope, domain). */
  record(scope: string, domain: string): void {
    if (!scope || !domain) return;
    this.ensureLoaded();
    (this.data[scope] ??= {})[domain] = Date.now();
    this.scheduleFlush();
  }

  /** True when this scope solved this domain within RECENT_WINDOW_MS. */
  wasRecentlySolved(scope: string, domain: string): boolean {
    if (!scope || !domain) return false;
    this.ensureLoaded();
    const ts = this.data[scope]?.[domain];
    if (!ts) return false;
    return (Date.now() - ts) < RECENT_WINDOW_MS;
  }
}

let _instance: HandoffLedger | null = null;
export function getHandoffLedger(): HandoffLedger {
  if (!_instance) _instance = new HandoffLedger();
  return _instance;
}
