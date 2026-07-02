/**
 * Per-session tab tracking for site-opened new tabs.
 *
 * The server's session model is one-session → one ACTIVE PageWrapper
 * (`ManagedSession.page` in http.ts). Before this layer existed, a
 * `target=_blank` / `window.open()` link created a Chrome page target
 * that NOTHING observed — every observation kept reading the opener
 * page while the real content loaded in an orphaned tab.
 *
 * SessionTabs owns the ordered tab list for a session and detects
 * site-created popups via a single per-Browser `targetcreated` listener
 * (refcounted across sessions — the proxy pool means several engines /
 * Browser instances can be live at once). Attribution is opener-based:
 * `target.opener()` survives rel=noopener for target=_blank and
 * window.open() (verified empirically against the pinned puppeteer +
 * Chrome). Opener-less page targets are almost always our own
 * `engine.newPage()` calls; a conservative fallback adopts them only
 * when exactly one session lives on that Browser and the engine
 * confirms it didn't create the target itself.
 *
 * Policy: a popup adopted into a session becomes the ACTIVE tab
 * immediately (mirrors real browser focus). The owning layer is told
 * via `onActiveChanged` so it can repoint `session.page` and the live
 * viewer's screencast. Pending TabEvents are drained into the next
 * state-bearing HTTP response so a popup that arrives after a click
 * response was already sent is surfaced one turn late, never lost.
 */

import type { Browser, Target } from 'puppeteer-core';
import { EventEmitter } from 'events';
import { PageWrapper } from './page.js';

export interface TabEvent {
  kind: 'opened' | 'closed';
  url: string;
  /** Tab index the event refers to (post-mutation list). */
  index: number;
  at: number;
  /** True when the system moved focus as part of this event. */
  autoSwitched: boolean;
}

export interface TabSummary {
  activeIndex: number;
  count: number;
  tabs: Array<{ index: number; url: string; active: boolean; openedAt: number }>;
}

/** One `targetcreated` listener per Browser, dispatching to the
 *  SessionTabs instances registered on it. */
interface BrowserTabRegistry {
  handler: (target: Target) => void;
  sessions: Map<string, SessionTabs>;
}

const registries = new Map<Browser, BrowserTabRegistry>();

/** How long the opener-less fallback defers before attributing, so an
 *  in-flight engine.newPage() / /session/create can claim its target. */
const OPENERLESS_DEFER_MS = 500;

function pageUrl(wrapper: PageWrapper): string {
  try {
    return wrapper.getRawPage().url();
  } catch {
    return '';
  }
}

export class SessionTabs {
  readonly tabs: PageWrapper[] = [];
  activeIndex = 0;

  private readonly openedAt: number[] = [];
  private pending: TabEvent[] = [];
  private lastOpened: TabEvent | null = null;
  private readonly emitter = new EventEmitter();
  private browser: Browser | null = null;
  private disposed = false;

  constructor(
    private readonly sessionId: string,
    initial: PageWrapper,
    private readonly onActiveChanged: (active: PageWrapper) => void,
  ) {
    this.tabs.push(initial);
    this.openedAt.push(Date.now());
  }

  get active(): PageWrapper {
    return this.tabs[this.activeIndex] ?? this.tabs[this.tabs.length - 1];
  }

  /** Engine that owns this session's pages (for adoptPage / the
   *  self-created-target guard). The initial tab always has it set. */
  private get engine() {
    return this.tabs[0]?.ownerEngine ?? this.active?.ownerEngine;
  }

  /** Attach the per-Browser targetcreated listener (refcounted). */
  attach(): void {
    const browser = this.engine?.getBrowser?.() ?? null;
    if (!browser) {
      console.warn(`[tabs] session ${this.sessionId}: no browser to attach to — popups will not be tracked`);
      return;
    }
    this.browser = browser;
    let registry = registries.get(browser);
    if (!registry) {
      const handler = (target: Target) => {
        void dispatchTargetCreated(browser, target);
      };
      registry = { handler, sessions: new Map() };
      registries.set(browser, registry);
      browser.on('targetcreated', handler);
    }
    registry.sessions.set(this.sessionId, this);
  }

  /** True when `raw` is already one of this session's tabs. */
  hasRawPage(raw: unknown): boolean {
    return this.tabs.some((t) => {
      try {
        return t.getRawPage() === raw;
      } catch {
        return false;
      }
    });
  }

  /** True when `target` is the target of one of this session's tabs —
   *  used for opener attribution. */
  ownsTarget(target: Target): boolean {
    return this.tabs.some((t) => {
      try {
        return t.getRawPage().target() === target;
      } catch {
        return false;
      }
    });
  }

  /**
   * Adopt a site-created popup page into this session: wrap it with the
   * opener's instrumentation, wire dialog/console capture and close
   * cleanup, and AUTO-SWITCH focus to it. Never throws.
   */
  async adopt(target: Target): Promise<void> {
    if (this.disposed) return;
    try {
      const raw = await target.page();
      if (!raw || this.hasRawPage(raw)) return;
      const engine = this.engine;
      if (!engine) return;

      const wrapper = await engine.adoptPage(raw);
      wrapper.sessionId = this.sessionId;
      // Same wiring /session/create does for the initial page — without
      // it, dialogs and console errors stop flowing after a tab switch.
      try { await wrapper.setupDialogHandler(); } catch { /* best-effort */ }
      try { await wrapper.enableConsoleCapture(); } catch { /* best-effort */ }
      raw.once('close', () => this.handleTabClosed(wrapper));

      if (this.disposed) {
        await wrapper.close().catch(() => {});
        return;
      }

      this.tabs.push(wrapper);
      this.openedAt.push(Date.now());
      const index = this.tabs.length - 1;
      this.activeIndex = index;
      // Chrome usually focuses a popup on open, but make it explicit —
      // the active tab MUST be the Chrome-focused target (see
      // bringActiveToFront).
      await this.bringActiveToFront();
      this.notifyActive();

      const evt: TabEvent = {
        kind: 'opened',
        url: pageUrl(wrapper),
        index,
        at: Date.now(),
        autoSwitched: true,
      };
      this.pending.push(evt);
      this.lastOpened = evt;
      this.emitter.emit('opened', evt);
      console.log(`[tabs] session ${this.sessionId}: adopted popup tab ${index + 1}/${this.tabs.length} → ${evt.url}`);
    } catch (err) {
      console.warn(`[tabs] session ${this.sessionId}: popup adoption failed: ${(err as Error).message}`);
    }
  }

  /** A tab's underlying page closed (site-side window.close(), or our
   *  own closeTab). Removes it; when it was active, focus falls back to
   *  the most recent remaining tab. Idempotent. */
  private handleTabClosed(wrapper: PageWrapper): void {
    const idx = this.tabs.indexOf(wrapper);
    if (idx === -1 || this.disposed) return;
    const wasActive = idx === this.activeIndex;
    const url = pageUrl(wrapper);
    this.tabs.splice(idx, 1);
    this.openedAt.splice(idx, 1);
    if (this.tabs.length === 0) return; // session teardown handles the rest
    if (idx < this.activeIndex) {
      this.activeIndex--;
    } else if (wasActive) {
      this.activeIndex = this.tabs.length - 1;
      void this.bringActiveToFront();
      this.notifyActive();
    }
    this.pending.push({
      kind: 'closed',
      url,
      index: this.activeIndex,
      at: Date.now(),
      autoSwitched: wasActive,
    });
  }

  /** Explicitly focus tab `index`. Throws RangeError on a bad index. */
  async switchTo(index: number): Promise<PageWrapper> {
    if (!Number.isInteger(index) || index < 0 || index >= this.tabs.length) {
      throw new RangeError(`tab index ${index} out of range (0..${this.tabs.length - 1})`);
    }
    this.activeIndex = index;
    await this.bringActiveToFront();
    this.notifyActive();
    return this.active;
  }

  /**
   * Make the active tab Chrome's FOCUSED target. Not cosmetic: on an
   * unfocused target, CDP input dispatch (Input.dispatchMouseEvent)
   * stalls until the protocol timeout (~180s), requestAnimationFrame
   * never fires, and lazy-load/IntersectionObserver machinery stays
   * asleep — the page must actually be the front tab for the click
   * pipeline to behave. Best-effort; never throws.
   */
  private async bringActiveToFront(): Promise<void> {
    try {
      const raw = this.active?.getRawPage() as
        | { bringToFront?: () => Promise<void> }
        | undefined;
      if (raw && typeof raw.bringToFront === 'function') {
        await raw.bringToFront();
      }
    } catch (err) {
      console.warn(
        `[tabs] session ${this.sessionId}: bringToFront failed: ${(err as Error).message}`,
      );
    }
  }

  /** Close tab `index` (default: active). Refuses to close the last
   *  tab. Returns the new active wrapper. */
  async closeTab(index?: number): Promise<PageWrapper> {
    const idx = index ?? this.activeIndex;
    if (!Number.isInteger(idx) || idx < 0 || idx >= this.tabs.length) {
      throw new RangeError(`tab index ${idx} out of range (0..${this.tabs.length - 1})`);
    }
    if (this.tabs.length === 1) {
      throw new Error('last_tab');
    }
    const wrapper = this.tabs[idx];
    await wrapper.close().catch(() => {});
    // The page 'close' event also routes here; indexOf makes it a no-op
    // the second time.
    this.handleTabClosed(wrapper);
    return this.active;
  }

  /**
   * Wait up to `graceMs` for an 'opened' event newer than `sinceTs`.
   * Used by the click handlers to catch a popup spawned by the action
   * they just dispatched. Consumes the matched event from the pending
   * notice queue (the caller reports it inline; leaving it queued would
   * double-report on the next state-bearing response).
   */
  async waitForOpenedSince(sinceTs: number, graceMs: number): Promise<TabEvent | null> {
    const take = (evt: TabEvent): TabEvent => {
      const i = this.pending.indexOf(evt);
      if (i !== -1) this.pending.splice(i, 1);
      return evt;
    };
    if (this.lastOpened && this.lastOpened.at >= sinceTs) {
      return take(this.lastOpened);
    }
    if (graceMs <= 0) return null;
    return new Promise((resolve) => {
      const onOpen = (evt: TabEvent) => {
        if (evt.at >= sinceTs) {
          cleanup();
          resolve(take(evt));
        }
      };
      const timer = setTimeout(() => {
        cleanup();
        resolve(null);
      }, graceMs);
      const cleanup = () => {
        clearTimeout(timer);
        this.emitter.off('opened', onOpen);
      };
      this.emitter.on('opened', onOpen);
    });
  }

  /**
   * Wait for a freshly opened tab to leave about:blank and reach a
   * non-loading readyState. Popups open as about:blank first and
   * navigate a beat later; observing before that reports an empty page.
   * Bounded, never throws.
   */
  static async waitForTabReady(wrapper: PageWrapper, maxMs = 4000): Promise<void> {
    const page = wrapper.getRawPage();
    const deadline = Date.now() + maxMs;
    try {
      while (Date.now() < deadline) {
        let url = '';
        try { url = page.url(); } catch { return; }
        if (url && url !== 'about:blank') break;
        await new Promise((r) => setTimeout(r, 100));
      }
      const remaining = Math.max(200, deadline - Date.now());
      await page
        .waitForFunction(() => document.readyState !== 'loading', { timeout: remaining })
        .catch(() => {});
    } catch {
      /* best-effort */
    }
  }

  /** Pending tab notices → consumed into the next state-bearing response. */
  drainEvents(): TabEvent[] {
    const out = this.pending;
    this.pending = [];
    return out;
  }

  summary(): TabSummary {
    return {
      activeIndex: this.activeIndex,
      count: this.tabs.length,
      tabs: this.tabs.map((t, i) => ({
        index: i,
        url: pageUrl(t),
        active: i === this.activeIndex,
        openedAt: this.openedAt[i],
      })),
    };
  }

  /** Close ALL tabs and release the per-Browser listener refcount. */
  async dispose(): Promise<void> {
    this.disposed = true;
    if (this.browser) {
      const registry = registries.get(this.browser);
      if (registry) {
        registry.sessions.delete(this.sessionId);
        if (registry.sessions.size === 0) {
          this.browser.off('targetcreated', registry.handler);
          registries.delete(this.browser);
        }
      }
      this.browser = null;
    }
    for (const tab of [...this.tabs]) {
      await tab.close().catch(() => {});
    }
    this.tabs.length = 0;
    this.openedAt.length = 0;
    this.pending = [];
    this.emitter.removeAllListeners();
  }

  private notifyActive(): void {
    try {
      this.onActiveChanged(this.active);
    } catch (err) {
      console.warn(`[tabs] session ${this.sessionId}: onActiveChanged failed: ${(err as Error).message}`);
    }
  }
}

/**
 * Route a new page target to the session that owns its opener. With no
 * opener, defer briefly (an in-flight engine.newPage() / second
 * /session/create claims its own target in that window), then adopt
 * only when the attribution is unambiguous: exactly one session on this
 * Browser and the engine confirms the target isn't self-created.
 * Unattributed popups are logged and left alone — never closed, since
 * the opener page may poll the popup handle.
 */
async function dispatchTargetCreated(browser: Browser, target: Target): Promise<void> {
  try {
    if (target.type() !== 'page') return;
    const registry = registries.get(browser);
    if (!registry || registry.sessions.size === 0) return;

    const opener = target.opener();
    if (opener) {
      for (const tabs of registry.sessions.values()) {
        if (tabs.ownsTarget(opener)) {
          await tabs.adopt(target);
          return;
        }
      }
      return; // opener not one of ours (another session's engine, etc.)
    }

    // Opener-less page target: usually our own newPage(). Defer, then
    // attribute conservatively.
    await new Promise((r) => setTimeout(r, OPENERLESS_DEFER_MS));
    const current = registries.get(browser);
    if (!current || current.sessions.size !== 1) return;
    const [tabs] = current.sessions.values();
    const engine = tabs.tabs[0]?.ownerEngine;
    if (engine?.isSelfCreated(target)) return;
    let raw: unknown = null;
    try { raw = await target.page(); } catch { return; }
    if (!raw) return;
    if (tabs.hasRawPage(raw)) return;
    await tabs.adopt(target);
  } catch (err) {
    console.warn(`[tabs] targetcreated dispatch failed: ${(err as Error).message}`);
  }
}
