/**
 * Ad/tracker request blocking.
 *
 * The bug this exists to prevent: the blocklist used to be substring
 * regexes tested against the ENTIRE request URL, two of them being the
 * bare words `/analytics/` and `/adservice/`. Modern bundlers name code
 * chunks after their source module, so a first-party application bundle
 * routinely carries those words in its path. On chase.com the match was
 *
 *   https://www.chase.com/_next/static/analytics-OctagonAnalyticsLite.<hash>.chunk.js
 *
 * — a Next.js chunk from the site's own origin. Aborting it killed
 * hydration outright: the page rendered a cookie banner over an empty
 * document, 0 characters of body text and 0 interactive elements, and
 * the agent was handed that as a screenshot. Measured directly: with
 * the old patterns the page yielded 0 text / 0 scrollHeight / 0
 * elements; with these rules, 12,719 / 6,017 / 148.
 *
 * Two rules keep that from recurring:
 *
 *   1. Match on the request HOSTNAME, never on the URL string. A path
 *      is authored by the site; a hostname identifies the party.
 *   2. Never block a request that is same-site with the page, whatever
 *      the list says. A site's own subdomains serve its own code.
 */

/** Hostnames whose requests are advertising or third-party telemetry.
 *  Every entry is a third party by construction — that is the whole
 *  point. Adding a first-party host here would be a category error. */
const BLOCKED_HOSTS: RegExp[] = [
  /^(.+\.)?doubleclick\.net$/,
  /^(.+\.)?google-analytics\.com$/,
  /^(.+\.)?googletagmanager\.com$/,
  /^(.+\.)?googleadservices\.com$/,
  /^(.+\.)?googlesyndication\.com$/,
  /^(.+\.)?adnxs\.com$/,
  /^(.+\.)?scorecardresearch\.com$/,
  /^connect\.facebook\.net$/,
];

/**
 * Best-effort registrable domain: the last two labels.
 *
 * Deliberately not PSL-backed. The only consumer is the same-site
 * guard below, and the failure mode of being too coarse on a
 * multi-label suffix (`foo.co.uk` → `co.uk`) is that two unrelated
 * hosts look same-site and a request is ALLOWED. Allowing a tracker is
 * a wasted request; blocking a bundle is a blank page. The imprecision
 * points the safe way.
 */
export function registrableDomain(host: string): string {
  const labels = host.toLowerCase().replace(/\.$/, '').split('.');
  return labels.length <= 2 ? labels.join('.') : labels.slice(-2).join('.');
}

/** Do these two hosts belong to the same site? */
export function isSameSite(a: string, b: string): boolean {
  if (!a || !b) return false;
  return registrableDomain(a) === registrableDomain(b);
}

/**
 * Should this request be aborted?
 *
 * `pageUrl` is the document the request belongs to. Pass it whenever
 * it is known — an unknown or `about:blank` page simply forfeits the
 * same-site guard, which is safe because `BLOCKED_HOSTS` holds no
 * first-party hosts to begin with.
 */
export function shouldBlockRequest(url: string, pageUrl?: string): boolean {
  let host: string;
  try {
    host = new URL(url).hostname.toLowerCase();
  } catch {
    // Unparseable (data:, blob:, chrome-extension:) — never our business.
    return false;
  }
  if (!host) return false;

  if (pageUrl) {
    let pageHost = '';
    try {
      pageHost = new URL(pageUrl).hostname.toLowerCase();
    } catch { /* about:blank and friends */ }
    // Rule 2. A site's own subdomains serve its own code, and no
    // blocklist entry is worth a blank page.
    if (pageHost && isSameSite(host, pageHost)) return false;
  }

  return BLOCKED_HOSTS.some((p) => p.test(host));
}
