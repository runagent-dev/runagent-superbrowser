/**
 * URL firewall with allow/deny lists.
 *
 * Adapted from nanobrowser's firewall storage. Provides user-configurable
 * URL policies on top of the basic SSRF protection in auth.ts.
 *
 * Configuration via environment variables:
 *   FIREWALL_ALLOW_LIST=example.com,api.example.com
 *   FIREWALL_DENY_LIST=evil.com,malware.org
 *   FIREWALL_ENABLED=true  (default)
 */

/** Hard-coded dangerous URL patterns that are always blocked. */
const ALWAYS_BLOCKED_PATTERNS = [
  /^chrome:\/\//i,
  /^chrome-extension:\/\//i,
  /^javascript:/i,
  /^data:/i,
  /^file:\/\//i,
  /^about:/i,
  /^view-source:/i,
  /^blob:/i,
];

/** Normalize a URL by stripping protocol, trimming, and lowercasing. */
function normalizeUrl(url: string): string {
  return url
    .trim()
    .toLowerCase()
    .replace(/^https?:\/\//, '')
    .replace(/\/+$/, '');
}

/** Extract the domain from a normalized URL (no protocol). */
function extractDomain(normalizedUrl: string): string {
  return normalizedUrl.split('/')[0].split(':')[0];
}

/** Check if a domain matches a pattern (including subdomain matching). */
function domainMatches(urlDomain: string, patternDomain: string): boolean {
  if (urlDomain === patternDomain) return true;
  // Subdomain matching: *.example.com matches sub.example.com
  return urlDomain.endsWith('.' + patternDomain);
}

export interface FirewallConfig {
  allowList: string[];
  denyList: string[];
  enabled: boolean;
}

export class UrlFirewall {
  private allowList: string[];
  private denyList: string[];
  private enabled: boolean;

  constructor(config?: Partial<FirewallConfig>) {
    this.enabled = config?.enabled ?? (process.env.FIREWALL_ENABLED !== 'false');

    // Parse lists from config or environment variables
    this.allowList = (
      config?.allowList ??
      (process.env.FIREWALL_ALLOW_LIST?.split(',').filter(Boolean) || [])
    ).map(normalizeUrl);

    this.denyList = (
      config?.denyList ??
      (process.env.FIREWALL_DENY_LIST?.split(',').filter(Boolean) || [])
    ).map(normalizeUrl);
  }

  /**
   * Check if a URL is allowed by the firewall.
   *
   * Logic:
   * 1. Always block dangerous protocols (chrome://, javascript:, data:, file://)
   * 2. If deny list contains the domain → block
   * 3. If allow list is non-empty and domain is NOT in it → block
   * 4. Otherwise → allow
   */
  isAllowed(url: string): { allowed: boolean; reason?: string } {
    if (!this.enabled) return { allowed: true };

    const trimmed = url.trim();

    // Check hard-coded dangerous patterns
    for (const pattern of ALWAYS_BLOCKED_PATTERNS) {
      if (pattern.test(trimmed)) {
        return { allowed: false, reason: `Blocked protocol: ${trimmed.split(':')[0]}` };
      }
    }

    const normalized = normalizeUrl(trimmed);
    const domain = extractDomain(normalized);

    // Check deny list (domain-level matching)
    for (const denied of this.denyList) {
      const deniedDomain = extractDomain(denied);
      if (domainMatches(domain, deniedDomain)) {
        return { allowed: false, reason: `Domain ${domain} is in deny list` };
      }
    }

    // If allow list is configured, only allow listed domains
    if (this.allowList.length > 0) {
      const isInAllowList = this.allowList.some((allowed) => {
        const allowedDomain = extractDomain(allowed);
        return domainMatches(domain, allowedDomain);
      });

      if (!isInAllowList) {
        return { allowed: false, reason: `Domain ${domain} is not in allow list` };
      }
    }

    return { allowed: true };
  }

  /** Add a URL/domain to the allow list. */
  addToAllowList(url: string): void {
    const normalized = normalizeUrl(url);
    if (!this.allowList.includes(normalized)) {
      this.allowList.push(normalized);
      // Remove from deny list if present
      this.denyList = this.denyList.filter((d) => d !== normalized);
    }
  }

  /** Remove a URL/domain from the allow list. */
  removeFromAllowList(url: string): void {
    const normalized = normalizeUrl(url);
    this.allowList = this.allowList.filter((d) => d !== normalized);
  }

  /** Add a URL/domain to the deny list. */
  addToDenyList(url: string): void {
    const normalized = normalizeUrl(url);
    if (!this.denyList.includes(normalized)) {
      this.denyList.push(normalized);
      // Remove from allow list if present
      this.allowList = this.allowList.filter((d) => d !== normalized);
    }
  }

  /** Remove a URL/domain from the deny list. */
  removeFromDenyList(url: string): void {
    const normalized = normalizeUrl(url);
    this.denyList = this.denyList.filter((d) => d !== normalized);
  }

  /** Enable or disable the firewall. */
  setEnabled(enabled: boolean): void {
    this.enabled = enabled;
  }

  /** Get current configuration. */
  getConfig(): FirewallConfig {
    return {
      allowList: [...this.allowList],
      denyList: [...this.denyList],
      enabled: this.enabled,
    };
  }
}

/** Default singleton instance. */
export const firewall = new UrlFirewall();
