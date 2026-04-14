/**
 * Per-domain captcha strategy statistics — persisted, simple Bayesian
 * scoring used by the registry to re-rank strategies for the current host.
 *
 * Why: the static priority order is a guess. In practice, certain
 * strategies routinely win on certain hosts (turnstile on
 * `challenges.cloudflare.com`, slider_drag on Temu lookalikes) and
 * routinely lose on others. Persisting outcomes lets the next solve on
 * the same host try the strategy that previously worked first — without
 * any training step or ML infrastructure.
 *
 * Storage: a single JSON file at SUPERBROWSER_STATS_PATH or
 * `~/.superbrowser/domain-stats.json`. Keyed by hostname (no scheme/path).
 * Loaded lazily on first read; flushed debounced after each write so a
 * crash mid-run loses at most ~2s of stats.
 *
 * Cooldown: a strategy that has lost ≥5 times in a row on a host gets
 * skipped for 30 minutes — long enough that an outage clears, short
 * enough that a fixed bug becomes selectable again.
 */

import fs from 'fs';
import os from 'os';
import path from 'path';

export interface StrategyStat {
  tries: number;
  wins: number;
  avgMs: number;
  lastFailedAt: number;
  consecutiveFails: number;
}

const COOLDOWN_FAIL_COUNT = 5;
const COOLDOWN_DURATION_MS = 30 * 60 * 1000;
const FLUSH_DEBOUNCE_MS = 2000;

function defaultStatsPath(): string {
  if (process.env.SUPERBROWSER_STATS_PATH) return process.env.SUPERBROWSER_STATS_PATH;
  return path.join(os.homedir(), '.superbrowser', 'domain-stats.json');
}

/** Hostname only (drop port, scheme, path). Falls back to '_unknown' on
 *  malformed URLs so we never throw out of a strategy registry call. */
export function hostKey(pageUrl: string | undefined | null): string {
  if (!pageUrl) return '_unknown';
  try {
    return new URL(pageUrl).hostname || '_unknown';
  } catch {
    return '_unknown';
  }
}

export class DomainStatsStore {
  private data: Record<string, Record<string, StrategyStat>> = {};
  private loaded = false;
  private flushTimer: NodeJS.Timeout | null = null;
  private filePath: string;

  constructor(filePath: string = defaultStatsPath()) {
    this.filePath = filePath;
  }

  private ensureLoaded(): void {
    if (this.loaded) return;
    this.loaded = true;
    try {
      if (fs.existsSync(this.filePath)) {
        const raw = fs.readFileSync(this.filePath, 'utf8');
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === 'object') {
          this.data = parsed as Record<string, Record<string, StrategyStat>>;
        }
      }
    } catch {
      // Corrupt or unreadable — start fresh, don't crash.
    }
  }

  private scheduleFlush(): void {
    if (this.flushTimer) return;
    this.flushTimer = setTimeout(() => {
      this.flushTimer = null;
      try {
        fs.mkdirSync(path.dirname(this.filePath), { recursive: true });
        fs.writeFileSync(this.filePath, JSON.stringify(this.data, null, 2));
      } catch {
        // Best-effort. Stats survive the run via in-memory state.
      }
    }, FLUSH_DEBOUNCE_MS);
    // unref() so a pending flush doesn't keep the process alive at exit.
    this.flushTimer.unref?.();
  }

  /** Record one strategy attempt outcome on a host. */
  recordOutcome(host: string, strategyName: string, won: boolean, durationMs: number): void {
    this.ensureLoaded();
    const hostStats = (this.data[host] ??= {});
    const s = (hostStats[strategyName] ??= {
      tries: 0, wins: 0, avgMs: 0, lastFailedAt: 0, consecutiveFails: 0,
    });
    s.tries += 1;
    if (won) {
      s.wins += 1;
      s.consecutiveFails = 0;
    } else {
      s.consecutiveFails += 1;
      s.lastFailedAt = Date.now();
    }
    // Online mean: avg' = avg + (x - avg)/n
    s.avgMs = s.avgMs + (durationMs - s.avgMs) / s.tries;
    this.scheduleFlush();
  }

  /**
   * Score in [0, 1] using Beta(wins+1, losses+1) mean — Laplace smoothing.
   * `priorScore` (typically the static priority normalized to [0,1]) blends
   * in when there are no observations yet so untried strategies don't all
   * collapse to 0.5 and lose their hand-tuned ordering.
   */
  scoreFor(host: string, strategyName: string, priorScore: number): number {
    this.ensureLoaded();
    const s = this.data[host]?.[strategyName];
    if (!s || s.tries === 0) return priorScore;
    const beta = (s.wins + 1) / (s.tries + 2);
    // Blend: heavy prior weight when few observations.
    const observationWeight = Math.min(1, s.tries / 10);
    return priorScore * (1 - observationWeight) + beta * observationWeight;
  }

  /**
   * True when the strategy is in cooldown for this host (≥5 consecutive
   * fails within COOLDOWN_DURATION_MS). Caller should skip it.
   */
  isInCooldown(host: string, strategyName: string): boolean {
    this.ensureLoaded();
    const s = this.data[host]?.[strategyName];
    if (!s) return false;
    if (s.consecutiveFails < COOLDOWN_FAIL_COUNT) return false;
    return (Date.now() - s.lastFailedAt) < COOLDOWN_DURATION_MS;
  }

  /** Test/debug accessor. */
  snapshot(): Record<string, Record<string, StrategyStat>> {
    this.ensureLoaded();
    return JSON.parse(JSON.stringify(this.data));
  }
}

/** Process-wide singleton. Strategies/registry use this. Tests can ignore. */
let _instance: DomainStatsStore | null = null;
export function getDomainStats(): DomainStatsStore {
  if (!_instance) _instance = new DomainStatsStore();
  return _instance;
}
