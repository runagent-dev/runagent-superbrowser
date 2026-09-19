/**
 * Ad/tracker blocking must never abort a site's own code.
 *
 * The bug these guard: the blocklist tested substring regexes against
 * the whole request URL, including the bare words `analytics` and
 * `adservice`. Bundlers name chunks after their source module, so a
 * first-party application bundle matched. On chase.com that aborted
 * `_next/static/analytics-OctagonAnalyticsLite.<hash>.chunk.js`,
 * hydration never ran, and the agent was handed a blank page.
 */

import { describe, it, expect } from 'vitest';
import {
  shouldBlockRequest, isSameSite, registrableDomain,
} from '../src/browser/request-blocking.js';

const PAGE = 'https://www.chase.com/personal/investments/retirement/'
  + 'retirement-calculators/401K-403B-calculator';

describe('shouldBlockRequest', () => {
  it('does not block the first-party bundle chunk that caused the outage', () => {
    expect(shouldBlockRequest(
      'https://www.chase.com/_next/static/analytics-OctagonAnalyticsLite.506ffc8f.chunk.js',
      PAGE,
    )).toBe(false);
  });

  it('does not block a first-party subdomain', () => {
    // analytics.chase.com is Chase's own telemetry endpoint. Same site,
    // so the blocklist does not get a vote.
    expect(shouldBlockRequest('https://analytics.chase.com/events/analytics/public/v4/events/logs', PAGE))
      .toBe(false);
  });

  it('does not block a first-party path merely containing "adservice"', () => {
    expect(shouldBlockRequest('https://www.chase.com/assets/adservice-banner.js', PAGE)).toBe(false);
  });

  it('does not block a first-party bundle DIRECTORY named for analytics', () => {
    // Measured on nike.com, where the old patterns would have aborted 15
    // first-party requests: the whole code-split bundle lives under
    // /static/analytics-client/, so every numbered chunk matched. A
    // file-name match was not the only shape of this bug.
    const nike = 'https://www.nike.com/';
    for (const u of [
      'https://www.nike.com/static/analytics-client/public/analytics-client.min.js',
      'https://www.nike.com/static/analytics-client/public/9970.chunk.5cf733.js',
      'https://www.nike.com/static/analytics-client/public/6168.chunk.e56ac6.js',
    ]) {
      expect(shouldBlockRequest(u, nike), u).toBe(false);
    }
  });

  it('does not block first-party telemetry on the site\'s own subdomain', () => {
    // capitalone.com routes Snowplow through its own host. Blocking a
    // site's own subdomain is how a "tracker" rule reaches page code.
    expect(shouldBlockRequest(
      'https://potomac-clickstream.capitalone.com/com.snowplowanalytics.snowplow/tp2',
      'https://www.capitalone.com/',
    )).toBe(false);
  });

  it('still blocks third-party ad and tracker hosts', () => {
    for (const u of [
      'https://googleads.g.doubleclick.net/pagead/viewthroughconversion/1036322744/',
      'https://www.googletagmanager.com/gtag/js?id=DC-9087442',
      'https://www.googleadservices.com/pagead/conversion/16708929165/',
      'https://www.google-analytics.com/collect',
      'https://connect.facebook.net/en_US/fbevents.js',
    ]) {
      expect(shouldBlockRequest(u, PAGE), u).toBe(true);
    }
  });

  it('blocks a tracker even when the page URL is unknown', () => {
    // The same-site guard is a safety net, not the mechanism: the host
    // list carries no first-party hosts, so it stands on its own.
    expect(shouldBlockRequest('https://www.google-analytics.com/collect')).toBe(true);
  });

  it('does not block a bare hostname that merely ends in a listed word', () => {
    // `notdoubleclick.net` is a different site; anchoring must hold.
    expect(shouldBlockRequest('https://notdoubleclick.net/x.js', PAGE)).toBe(false);
  });

  it('ignores non-http schemes rather than guessing', () => {
    expect(shouldBlockRequest('data:text/javascript,void 0', PAGE)).toBe(false);
    expect(shouldBlockRequest('blob:https://www.chase.com/abc', PAGE)).toBe(false);
  });
});

describe('same-site guard', () => {
  it('treats subdomains of one site as same-site', () => {
    expect(isSameSite('analytics.chase.com', 'www.chase.com')).toBe(true);
  });

  it('treats unrelated hosts as cross-site', () => {
    expect(isSameSite('doubleclick.net', 'www.chase.com')).toBe(false);
  });

  it('errs toward allowing on a multi-label suffix', () => {
    // Too coarse here (co.uk), which ALLOWS a request rather than
    // blocking one. A wasted request beats a blank page.
    expect(registrableDomain('a.example.co.uk')).toBe('co.uk');
  });
});
