/**
 * Tests for SessionTabs — the per-session tab list behind new-tab
 * awareness.
 *
 * The suite is pure-unit (no Puppeteer launch), matching the rest of
 * tests/: PageWrappers are faked with just the surface SessionTabs
 * touches (getRawPage().url(), close()). Popup ADOPTION (targetcreated
 * → adopt) needs a real Browser and is covered by the live fixture
 * drive in the verification recipe, not here.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { SessionTabs, type TabEvent } from '../src/browser/tab-manager.js';
import type { PageWrapper } from '../src/browser/page.js';

interface FakeWrapper {
  url: string;
  closed: boolean;
  getRawPage: () => { url: () => string };
  close: () => Promise<void>;
}

function fakeWrapper(url: string): FakeWrapper {
  const w: FakeWrapper = {
    url,
    closed: false,
    getRawPage: () => ({ url: () => w.url }),
    close: async () => {
      w.closed = true;
    },
  };
  return w;
}

const asWrapper = (w: FakeWrapper) => w as unknown as PageWrapper;

/** Push a second tab in via the private-ish surface the adopt path uses.
 *  We can't run adopt() without a Browser, so tests append directly and
 *  emit the same bookkeeping through switchTo(). */
function injectTab(tabs: SessionTabs, w: FakeWrapper): number {
  tabs.tabs.push(asWrapper(w));
  (tabs as unknown as { openedAt: number[] }).openedAt.push(Date.now());
  return tabs.tabs.length - 1;
}

function injectOpenedEvent(tabs: SessionTabs, index: number, url: string): TabEvent {
  const evt: TabEvent = {
    kind: 'opened', url, index, at: Date.now(), autoSwitched: true,
  };
  const priv = tabs as unknown as {
    pending: TabEvent[];
    lastOpened: TabEvent | null;
    emitter: { emit: (e: string, v: TabEvent) => void };
  };
  priv.pending.push(evt);
  priv.lastOpened = evt;
  priv.emitter.emit('opened', evt);
  return evt;
}

describe('SessionTabs', () => {
  let initial: FakeWrapper;
  let switched: PageWrapper[];
  let tabs: SessionTabs;

  beforeEach(() => {
    initial = fakeWrapper('https://example.com/');
    switched = [];
    tabs = new SessionTabs('session-test', asWrapper(initial), (active) => {
      switched.push(active);
    });
  });

  it('starts with one active tab', () => {
    expect(tabs.tabs.length).toBe(1);
    expect(tabs.activeIndex).toBe(0);
    expect(tabs.active).toBe(asWrapper(initial));
    const s = tabs.summary();
    expect(s.count).toBe(1);
    expect(s.tabs[0].url).toBe('https://example.com/');
    expect(s.tabs[0].active).toBe(true);
  });

  it('switchTo moves focus and notifies', async () => {
    const popup = fakeWrapper('https://example.com/popup');
    const idx = injectTab(tabs, popup);
    const active = await tabs.switchTo(idx);
    expect(active).toBe(asWrapper(popup));
    expect(tabs.activeIndex).toBe(idx);
    expect(switched).toEqual([asWrapper(popup)]);
  });

  it('switchTo rejects out-of-range indices', async () => {
    await expect(tabs.switchTo(3)).rejects.toThrow(RangeError);
    await expect(tabs.switchTo(-1)).rejects.toThrow(RangeError);
    await expect(tabs.switchTo(1.5)).rejects.toThrow(RangeError);
  });

  it('closeTab refuses the last tab', async () => {
    await expect(tabs.closeTab()).rejects.toThrow('last_tab');
    expect(initial.closed).toBe(false);
  });

  it('closeTab on the active tab falls back to most recent remaining', async () => {
    const a = fakeWrapper('https://a.example/');
    const b = fakeWrapper('https://b.example/');
    injectTab(tabs, a);
    const idxB = injectTab(tabs, b);
    await tabs.switchTo(idxB);
    switched.length = 0;

    const active = await tabs.closeTab(idxB);
    expect(b.closed).toBe(true);
    // Most recent remaining is `a` (index 1 after splice).
    expect(active).toBe(asWrapper(a));
    expect(tabs.activeIndex).toBe(1);
    expect(switched).toEqual([asWrapper(a)]);
    // A 'closed' notice is queued for the next state-bearing response.
    const events = tabs.drainEvents();
    expect(events.length).toBe(1);
    expect(events[0].kind).toBe('closed');
    expect(events[0].url).toBe('https://b.example/');
  });

  it('closeTab on a background tab keeps focus and reindexes', async () => {
    const a = fakeWrapper('https://a.example/');
    const b = fakeWrapper('https://b.example/');
    const idxA = injectTab(tabs, a);
    const idxB = injectTab(tabs, b);
    await tabs.switchTo(idxB);
    switched.length = 0;

    const active = await tabs.closeTab(idxA);
    // Focus stays on b, whose index shifted down by one.
    expect(active).toBe(asWrapper(b));
    expect(tabs.activeIndex).toBe(1);
    expect(switched).toEqual([]); // no focus change → no notify
  });

  it('waitForOpenedSince resolves immediately for an already-arrived popup and consumes the notice', async () => {
    const popup = fakeWrapper('https://popup.example/');
    const idx = injectTab(tabs, popup);
    const before = Date.now() - 5;
    injectOpenedEvent(tabs, idx, popup.url);

    const evt = await tabs.waitForOpenedSince(before, 50);
    expect(evt?.kind).toBe('opened');
    expect(evt?.url).toBe('https://popup.example/');
    // Consumed — must not double-report via drainEvents.
    expect(tabs.drainEvents()).toEqual([]);
  });

  it('waitForOpenedSince times out when nothing opened', async () => {
    const t0 = Date.now();
    const evt = await tabs.waitForOpenedSince(t0, 30);
    expect(evt).toBeNull();
  });

  it('waitForOpenedSince ignores events older than sinceTs', async () => {
    const popup = fakeWrapper('https://old.example/');
    const idx = injectTab(tabs, popup);
    injectOpenedEvent(tabs, idx, popup.url);
    await new Promise((r) => setTimeout(r, 5));

    const evt = await tabs.waitForOpenedSince(Date.now(), 20);
    expect(evt).toBeNull();
    // The stale notice is still pending for the next response.
    expect(tabs.drainEvents().length).toBe(1);
  });

  it('waitForOpenedSince catches an event arriving inside the grace window', async () => {
    const t0 = Date.now();
    const pending = tabs.waitForOpenedSince(t0, 200);
    setTimeout(() => {
      const popup = fakeWrapper('https://late.example/');
      const idx = injectTab(tabs, popup);
      injectOpenedEvent(tabs, idx, popup.url);
    }, 20);
    const evt = await pending;
    expect(evt?.url).toBe('https://late.example/');
  });

  it('drainEvents empties the queue', () => {
    const popup = fakeWrapper('https://popup.example/');
    const idx = injectTab(tabs, popup);
    injectOpenedEvent(tabs, idx, popup.url);
    expect(tabs.drainEvents().length).toBe(1);
    expect(tabs.drainEvents()).toEqual([]);
  });

  it('dispose closes every tab', async () => {
    const a = fakeWrapper('https://a.example/');
    const b = fakeWrapper('https://b.example/');
    injectTab(tabs, a);
    injectTab(tabs, b);
    await tabs.dispose();
    expect(initial.closed).toBe(true);
    expect(a.closed).toBe(true);
    expect(b.closed).toBe(true);
    expect(tabs.tabs.length).toBe(0);
  });
});
