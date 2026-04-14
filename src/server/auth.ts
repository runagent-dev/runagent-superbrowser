/**
 * Authentication and security middleware.
 *
 * Token-based auth (like browserless), SSRF protection,
 * session ID validation, rate limiting per IP.
 */

import type { Request, Response, NextFunction } from 'express';

/**
 * Bearer token authentication middleware.
 * If TOKEN env var is set, all requests must include it.
 * If TOKEN is not set, auth is disabled (development mode).
 */
export function tokenAuth(req: Request, res: Response, next: NextFunction): void {
  const token = process.env.TOKEN;
  if (!token) {
    next();
    return;
  }

  const authHeader = req.headers.authorization;
  const queryToken = req.query.token as string | undefined;

  if (authHeader === `Bearer ${token}` || queryToken === token) {
    next();
    return;
  }

  res.status(401).json({ error: 'Unauthorized — provide Bearer token or ?token= query param' });
}

/** Internal/private IP ranges to block for SSRF protection. */
const BLOCKED_HOSTS = [
  'localhost',
  '127.0.0.1',
  '::1',
  '0.0.0.0',
  '[::1]',
  'metadata.google.internal',
];

const BLOCKED_IP_PATTERNS = [
  /^10\./,                        // 10.0.0.0/8
  /^172\.(1[6-9]|2[0-9]|3[01])\./, // 172.16.0.0/12
  /^192\.168\./,                  // 192.168.0.0/16
  /^169\.254\./,                  // Link-local
  /^fc00:/i,                      // IPv6 unique local
  /^fd/i,                         // IPv6 unique local
  /^fe80:/i,                      // IPv6 link-local
  /^::ffff:127\./,                // IPv4-mapped localhost
  /^::ffff:10\./,                 // IPv4-mapped private
  /^::ffff:192\.168\./,           // IPv4-mapped private
];

/**
 * Validate a URL is safe to navigate to (SSRF protection).
 * Blocks internal IPs, localhost, file:// protocol, and metadata endpoints.
 */
export function validateUrl(url: string): { valid: boolean; error?: string } {
  // Block file:// protocol
  if (url.startsWith('file://')) {
    return { valid: false, error: 'file:// protocol is not allowed' };
  }

  // Must be http or https
  if (!url.startsWith('http://') && !url.startsWith('https://')) {
    return { valid: false, error: 'Only http:// and https:// URLs are allowed' };
  }

  try {
    const parsed = new URL(url);
    const host = parsed.hostname.toLowerCase();

    // Block known internal hosts
    if (BLOCKED_HOSTS.includes(host)) {
      return { valid: false, error: 'Navigation to internal hosts is not allowed' };
    }

    // Block private IP ranges
    for (const pattern of BLOCKED_IP_PATTERNS) {
      if (pattern.test(host)) {
        return { valid: false, error: 'Navigation to private IP ranges is not allowed' };
      }
    }

    // Block cloud metadata endpoints
    if (host === '169.254.169.254' || host.endsWith('.internal')) {
      return { valid: false, error: 'Navigation to cloud metadata endpoints is not allowed' };
    }

    return { valid: true };
  } catch {
    return { valid: false, error: 'Invalid URL' };
  }
}

/**
 * Validate session ID format.
 */
export function isValidSessionId(id: string): boolean {
  return /^session-\d+$/.test(id) && id.length <= 32;
}

/**
 * Simple in-memory per-IP rate limiter.
 *
 * Loopback callers (127.0.0.1, ::1) are exempted by default — the whole
 * nanobot bridge + WebSocket client traffic comes from the same local IP
 * as the server, and one agent run easily does 200+ requests per minute
 * (state fetches, element list refreshes, feedback polls, click/type
 * rounds). Counting them against the bucket caused hard-to-diagnose 429s
 * mid-run. Set RATE_LIMIT_LOOPBACK_BYPASS=false to re-enable enforcement.
 */
export class RateLimiter {
  private requests = new Map<string, { count: number; resetAt: number }>();
  private maxRequests: number;
  private windowMs: number;
  private loopbackBypass: boolean;

  constructor(maxRequests: number = 100, windowMs: number = 60000) {
    this.maxRequests = maxRequests;
    this.windowMs = windowMs;
    this.loopbackBypass = process.env.RATE_LIMIT_LOOPBACK_BYPASS !== 'false';

    // Cleanup old entries periodically
    setInterval(() => {
      const now = Date.now();
      for (const [ip, data] of this.requests) {
        if (now > data.resetAt) this.requests.delete(ip);
      }
    }, windowMs);
  }

  private isLoopback(ip: string): boolean {
    return (
      ip === '127.0.0.1' ||
      ip === '::1' ||
      ip === '::ffff:127.0.0.1' ||
      ip.startsWith('127.') ||
      ip === 'unknown' // fallback when req.ip/remoteAddress both failed
    );
  }

  check(ip: string): boolean {
    if (this.loopbackBypass && this.isLoopback(ip)) return true;
    const now = Date.now();
    const entry = this.requests.get(ip);

    if (!entry || now > entry.resetAt) {
      this.requests.set(ip, { count: 1, resetAt: now + this.windowMs });
      return true;
    }

    entry.count++;
    return entry.count <= this.maxRequests;
  }

  middleware() {
    return (req: Request, res: Response, next: NextFunction): void => {
      const ip = req.ip || req.socket.remoteAddress || 'unknown';
      if (this.check(ip)) {
        next();
      } else {
        // Include Retry-After so well-behaved clients back off without
        // the LLM having to reason about the delay.
        res.setHeader('Retry-After', '30');
        res.status(429).json({
          error: 'Too many requests — try again later',
          retry_after_seconds: 30,
          transient: true,
        });
      }
    };
  }
}
