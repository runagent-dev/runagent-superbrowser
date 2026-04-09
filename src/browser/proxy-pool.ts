/**
 * Proxy pool and browser pool manager.
 *
 * Since Puppeteer only supports proxy at browser launch (--proxy-server),
 * we maintain separate browser instances for different proxies.
 *
 * Features:
 * - Region-to-proxy mapping via env vars or config
 * - On-demand browser launch per proxy
 * - Geo-block detection to suggest the right proxy
 * - Auto-cleanup of idle proxy browsers
 *
 * Config via environment:
 *   PROXY_POOL=bd:socks5://bd-proxy:1080,in:socks5://in-proxy:1080,us:http://us-proxy:8080
 *   PROXY_DEFAULT=http://default-proxy:8080
 */

import { BrowserEngine, type BrowserConfig } from './engine.js';
import type { PageWrapper } from './page.js';

export interface ProxyEntry {
  region: string;
  url: string;
  domains?: string[];  // Optional domain patterns this proxy is good for
}

/** Common geo-block detection patterns. */
const GEO_BLOCK_PATTERNS = [
  /access.*(limited|restricted|denied|blocked)/i,
  /not available in your (region|country|area|location)/i,
  /geo.?restrict/i,
  /this (service|content|website|site) is (not|only) available/i,
  /country.*(not|un).*(support|available)/i,
  /your (ip|location|region) (is|has been) (blocked|restricted)/i,
  /403 forbidden/i,
  /unavailable in your region/i,
  /access from your country/i,
];

/** Domain → region hints for common geo-locked sites. */
const DOMAIN_REGION_HINTS: Record<string, string> = {
  'eticket.railway.gov.bd': 'bd',
  'railway.gov.bd': 'bd',
  'irctc.co.in': 'in',
  'tatkal.irctc.co.in': 'in',
  'uidai.gov.in': 'in',
  'nid.gov.bd': 'bd',
  'bbc.co.uk': 'gb',
  'nhk.or.jp': 'jp',
  'ard.de': 'de',
};

export class ProxyPool {
  private proxies: Map<string, ProxyEntry> = new Map();
  private browserPool: Map<string, BrowserEngine> = new Map();
  private defaultEngine: BrowserEngine;
  private baseConfig: Partial<BrowserConfig>;
  private defaultProxy?: string;

  constructor(defaultEngine: BrowserEngine, baseConfig: Partial<BrowserConfig> = {}) {
    this.defaultEngine = defaultEngine;
    this.baseConfig = baseConfig;
    this.defaultProxy = process.env.PROXY_DEFAULT;
    this.loadFromEnv();
  }

  /** Load proxy entries from PROXY_POOL env var. Format: region:url,region:url */
  private loadFromEnv(): void {
    const poolStr = process.env.PROXY_POOL || '';
    if (!poolStr) return;

    for (const entry of poolStr.split(',')) {
      const [region, url] = entry.trim().split(':', 2);
      if (region && url) {
        // Rejoin in case the URL had a : in it (like socks5://host:port)
        const fullUrl = entry.substring(region.length + 1).trim();
        this.proxies.set(region.toLowerCase(), {
          region: region.toLowerCase(),
          url: fullUrl,
        });
      }
    }

    if (this.proxies.size > 0) {
      console.log(`Proxy pool loaded: ${Array.from(this.proxies.keys()).join(', ')}`);
    }
  }

  /** Add a proxy entry programmatically. */
  addProxy(region: string, url: string, domains?: string[]): void {
    this.proxies.set(region.toLowerCase(), {
      region: region.toLowerCase(),
      url,
      domains,
    });
  }

  /** Get or launch a browser engine with the specified proxy. */
  async getEngine(proxyUrl?: string): Promise<BrowserEngine> {
    if (!proxyUrl) return this.defaultEngine;

    // Check if we already have a browser for this proxy
    const existing = this.browserPool.get(proxyUrl);
    if (existing?.isRunning()) return existing;

    // Launch a new browser with this proxy
    console.log(`Launching browser with proxy: ${proxyUrl}`);
    const engine = new BrowserEngine({
      ...this.baseConfig,
      proxy: proxyUrl,
    });
    await engine.launch();
    this.browserPool.set(proxyUrl, engine);
    return engine;
  }

  /** Get a browser engine for a specific region. */
  async getEngineForRegion(region: string): Promise<BrowserEngine> {
    const entry = this.proxies.get(region.toLowerCase());
    if (!entry) {
      if (this.defaultProxy) {
        return this.getEngine(this.defaultProxy);
      }
      return this.defaultEngine;
    }
    return this.getEngine(entry.url);
  }

  /** Get the best proxy for a given URL based on domain hints. */
  getRegionForUrl(url: string): string | null {
    try {
      const hostname = new URL(url).hostname.toLowerCase();
      // Check exact domain match
      if (DOMAIN_REGION_HINTS[hostname]) return DOMAIN_REGION_HINTS[hostname];
      // Check parent domain
      const parts = hostname.split('.');
      for (let i = 1; i < parts.length; i++) {
        const parent = parts.slice(i).join('.');
        if (DOMAIN_REGION_HINTS[parent]) return DOMAIN_REGION_HINTS[parent];
      }
      // Check custom domain patterns
      for (const [, entry] of this.proxies) {
        if (entry.domains?.some((d) => hostname.includes(d))) {
          return entry.region;
        }
      }
    } catch {
      // Invalid URL
    }
    return null;
  }

  /** Create a new page, optionally with a specific proxy or region. */
  async newPage(options?: { proxy?: string; region?: string; url?: string }): Promise<PageWrapper> {
    let engine = this.defaultEngine;

    if (options?.proxy) {
      engine = await this.getEngine(options.proxy);
    } else if (options?.region) {
      engine = await this.getEngineForRegion(options.region);
    } else if (options?.url) {
      const region = this.getRegionForUrl(options.url);
      if (region) {
        engine = await this.getEngineForRegion(region);
      }
    }

    return engine.newPage();
  }

  /** Detect if page content indicates a geo-block. */
  async detectGeoBlock(pageText: string): Promise<boolean> {
    const lower = pageText.toLowerCase();
    return GEO_BLOCK_PATTERNS.some((pattern) => pattern.test(lower));
  }

  /** Suggest a region based on page content and URL. */
  suggestRegion(url: string, pageText: string): string | null {
    // First check URL-based hints
    const urlRegion = this.getRegionForUrl(url);
    if (urlRegion) return urlRegion;

    // Check if the page mentions a specific country
    const lower = pageText.toLowerCase();
    const countryPatterns: Record<string, string[]> = {
      'bd': ['bangladesh', 'bangla', 'dhaka'],
      'in': ['india', 'indian', 'bharat'],
      'gb': ['united kingdom', 'british', 'england'],
      'us': ['united states', 'american'],
      'jp': ['japan', 'japanese'],
      'de': ['germany', 'german', 'deutschland'],
      'cn': ['china', 'chinese'],
      'kr': ['korea', 'korean'],
    };

    for (const [region, keywords] of Object.entries(countryPatterns)) {
      if (keywords.some((kw) => lower.includes(kw))) {
        return region;
      }
    }

    return null;
  }

  /** List all configured proxies. */
  listProxies(): ProxyEntry[] {
    return Array.from(this.proxies.values());
  }

  /** Check if a region has a configured proxy. */
  hasProxy(region: string): boolean {
    return this.proxies.has(region.toLowerCase());
  }

  /** Shut down all proxy browser instances. */
  async closeAll(): Promise<void> {
    for (const [url, engine] of this.browserPool) {
      try {
        await engine.close();
      } catch {
        // Ignore cleanup errors
      }
    }
    this.browserPool.clear();
  }
}
