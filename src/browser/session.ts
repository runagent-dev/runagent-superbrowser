/**
 * Session management from browserless.
 *
 * Tracks browser sessions with IDs, connection counts,
 * user data dirs, timeouts, and reconnection support.
 */

import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';
import * as crypto from 'crypto';
import type { PageWrapper } from './page.js';

export interface SessionInfo {
  id: string;
  trackingId?: string;
  startedOn: number;
  numbConnected: number;
  userDataDir: string | null;
  isTempDataDir: boolean;
  ttl: number;
  keepUntil: number;
}

export class SessionManager {
  private sessions = new Map<string, SessionInfo>();
  private pages = new Map<string, PageWrapper>();
  private timers = new Map<string, ReturnType<typeof setTimeout>>();
  private dataDir: string;

  constructor(dataDir?: string) {
    this.dataDir = dataDir || path.join(os.tmpdir(), 'superbrowser-data-dirs');
    if (!fs.existsSync(this.dataDir)) {
      fs.mkdirSync(this.dataDir, { recursive: true });
    }
  }

  /** Create a new session. */
  createSession(options?: {
    trackingId?: string;
    userDataDir?: string;
    ttl?: number;
  }): SessionInfo {
    const id = crypto.randomUUID();
    const isTempDataDir = !options?.userDataDir;
    const userDataDir = options?.userDataDir || this.createTempDataDir(id);

    // Validate tracking ID if provided
    if (options?.trackingId) {
      this.validateTrackingId(options.trackingId);
    }

    const session: SessionInfo = {
      id,
      trackingId: options?.trackingId,
      startedOn: Date.now(),
      numbConnected: 1,
      userDataDir,
      isTempDataDir,
      ttl: options?.ttl || 0,
      keepUntil: 0,
    };

    this.sessions.set(id, session);
    return session;
  }

  /** Get a session by ID. */
  getSession(id: string): SessionInfo | undefined {
    return this.sessions.get(id);
  }

  /** Find session by tracking ID. */
  findByTrackingId(trackingId: string): SessionInfo | undefined {
    for (const session of this.sessions.values()) {
      if (session.trackingId === trackingId) return session;
    }
    return undefined;
  }

  /** Register a page wrapper with a session. */
  registerPage(sessionId: string, page: PageWrapper): void {
    this.pages.set(sessionId, page);
  }

  /** Get the page for a session. */
  getPage(sessionId: string): PageWrapper | undefined {
    return this.pages.get(sessionId);
  }

  /** Increment connection count (reconnection). */
  reconnect(id: string): SessionInfo | undefined {
    const session = this.sessions.get(id);
    if (session) {
      session.numbConnected++;
    }
    return session;
  }

  /** Decrement connection count. */
  disconnect(id: string): void {
    const session = this.sessions.get(id);
    if (!session) return;

    session.numbConnected--;

    if (session.numbConnected <= 0) {
      if (session.keepUntil > 0) {
        // Deferred cleanup
        const timer = setTimeout(() => {
          this.cleanupSession(id);
        }, session.keepUntil);
        this.timers.set(id, timer);
      } else {
        this.cleanupSession(id);
      }
    }
  }

  /** Set keep-until timeout for a session (ms). */
  setKeepUntil(id: string, ms: number): void {
    const session = this.sessions.get(id);
    if (session) {
      session.keepUntil = ms;
    }
  }

  /** Kill sessions by ID, trackingId, or 'all'. */
  async killSessions(target: string): Promise<number> {
    let killed = 0;

    if (target === 'all') {
      for (const id of Array.from(this.sessions.keys())) {
        await this.cleanupSession(id);
        killed++;
      }
      return killed;
    }

    // Try by session ID
    if (this.sessions.has(target)) {
      await this.cleanupSession(target);
      return 1;
    }

    // Try by tracking ID
    for (const [id, session] of this.sessions) {
      if (session.trackingId === target) {
        await this.cleanupSession(id);
        killed++;
      }
    }

    return killed;
  }

  /** Get all active sessions. */
  getActiveSessions(): SessionInfo[] {
    return Array.from(this.sessions.values());
  }

  /** Get active session count. */
  get activeCount(): number {
    return this.sessions.size;
  }

  private async cleanupSession(id: string): Promise<void> {
    const session = this.sessions.get(id);
    if (!session) return;

    // Clear any pending timer
    const timer = this.timers.get(id);
    if (timer) {
      clearTimeout(timer);
      this.timers.delete(id);
    }

    // Close page
    const page = this.pages.get(id);
    if (page) {
      try {
        await page.close();
      } catch {
        // Page may already be closed
      }
      this.pages.delete(id);
    }

    // Clean up temp data dir
    if (session.isTempDataDir && session.userDataDir) {
      try {
        fs.rmSync(session.userDataDir, { recursive: true, force: true });
      } catch {
        // May already be gone
      }
    }

    this.sessions.delete(id);
  }

  private createTempDataDir(sessionId: string): string {
    const dir = path.join(this.dataDir, `session-${sessionId}`);
    fs.mkdirSync(dir, { recursive: true });
    return dir;
  }

  /** Validate tracking ID format (alphanumeric + dashes/underscores, max 32 chars). */
  private validateTrackingId(id: string): void {
    if (id === 'all') throw new Error("Tracking ID cannot be 'all'");
    if (id.length > 32) throw new Error('Tracking ID must be 32 chars or less');
    if (!/^[a-zA-Z0-9_-]+$/.test(id)) {
      throw new Error('Tracking ID must be alphanumeric with dashes/underscores only');
    }
  }
}
