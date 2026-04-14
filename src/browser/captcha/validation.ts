/**
 * Guards against vision-LLM hallucination.
 *
 * Three failure modes these utilities block:
 *  - Coords way outside the captcha widget (LLM invents x=9999 on a 1024px
 *    viewport); without clamping those clicks land in navbars, ads, or off
 *    the page entirely.
 *  - Tile indices scraped from prose ("try tiles around 45 or 300"); the
 *    old fallback did `text.match(/\d+/g)` and clicked whatever numeric
 *    substring it found.
 *  - Shape mismatches ("handleX":"100" instead of 100); quietly parsed as
 *    NaN and then `Math.round`'d to 0.
 *
 * Everything here is synchronous, dependency-free, and throws only through
 * typed return values — strategies decide how to recover.
 */

import { jsonrepair } from 'jsonrepair';

export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

/**
 * Clamp a point to a rect expanded by `tolerancePx`. Returns null when the
 * point is so far outside the rect it almost certainly came from an
 * hallucinated coord — callers should re-prompt or skip the action.
 *
 * A 20% outside-tolerance was picked empirically: grid captchas often sit
 * inside iframe coords that don't exactly match the host page's rect, so
 * we allow some slop before rejecting. Beyond 20% the gap is usually a
 * hallucinated integer (e.g. LLM returning 9999 on a 320-wide widget).
 */
export function clampToRect(
  x: number,
  y: number,
  rect: Rect,
  tolerancePx = Math.max(20, Math.round(Math.min(rect.width, rect.height) * 0.2)),
): { x: number; y: number; clamped: boolean } | null {
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  const minX = rect.x - tolerancePx;
  const maxX = rect.x + rect.width + tolerancePx;
  const minY = rect.y - tolerancePx;
  const maxY = rect.y + rect.height + tolerancePx;
  if (x < minX || x > maxX || y < minY || y > maxY) return null;
  const clampedX = Math.min(rect.x + rect.width, Math.max(rect.x, x));
  const clampedY = Math.min(rect.y + rect.height, Math.max(rect.y, y));
  const clamped = clampedX !== x || clampedY !== y;
  return { x: Math.round(clampedX), y: Math.round(clampedY), clamped };
}

/**
 * Strict JSON parse using jsonrepair for mild model noise (trailing commas,
 * unfenced code blocks). Returns null on any failure — NO fallback to
 * prose-number-scraping.
 */
export function strictJson<T = unknown>(text: string): T | null {
  if (!text) return null;
  const trimmed = text.trim();
  // Strip markdown fences if present.
  const fenced = trimmed.match(/```(?:json)?\s*([\s\S]*?)```/);
  const candidate = fenced ? fenced[1].trim() : trimmed;
  try {
    return JSON.parse(candidate) as T;
  } catch { /* try jsonrepair */ }
  try {
    return JSON.parse(jsonrepair(candidate)) as T;
  } catch {
    return null;
  }
}

/**
 * Parse `{tiles: number[]}` strictly. Returns an empty array when the shape
 * doesn't match; does NOT extract stray numbers from prose.
 */
export function parseTilesStrict(text: string, maxTile: number): number[] {
  const parsed = strictJson<{ tiles?: unknown }>(text);
  if (!parsed || !Array.isArray(parsed.tiles)) return [];
  const seen = new Set<number>();
  const out: number[] = [];
  for (const v of parsed.tiles) {
    if (typeof v !== 'number' || !Number.isFinite(v)) continue;
    const n = Math.trunc(v);
    if (n < 1 || n > maxTile) continue;
    if (seen.has(n)) continue;
    seen.add(n);
    out.push(n);
  }
  return out;
}

/** Shape-check `{x:number, y:number}` — rejects strings/NaN/missing fields. */
export function parseXYStrict(text: string): { x: number; y: number } | null {
  const parsed = strictJson<{ x?: unknown; y?: unknown }>(text);
  if (!parsed) return null;
  if (typeof parsed.x !== 'number' || typeof parsed.y !== 'number') return null;
  if (!Number.isFinite(parsed.x) || !Number.isFinite(parsed.y)) return null;
  return { x: parsed.x, y: parsed.y };
}

/** Shape-check `{startX, startY, endX, endY}`. */
export function parseDragStrict(text: string): {
  startX: number; startY: number; endX: number; endY: number;
} | null {
  const parsed = strictJson<Record<string, unknown>>(text);
  if (!parsed) return null;
  const keys = ['startX', 'startY', 'endX', 'endY'] as const;
  const out: Record<string, number> = {};
  for (const k of keys) {
    const v = parsed[k];
    if (typeof v !== 'number' || !Number.isFinite(v)) return null;
    out[k] = v;
  }
  return out as { startX: number; startY: number; endX: number; endY: number };
}

/** Shape-check slider-find `{handleX, handleY, targetX, targetY}`. */
export function parseSliderCoordsStrict(text: string): {
  handleX: number; handleY: number; targetX: number; targetY: number;
} | null {
  const parsed = strictJson<Record<string, unknown>>(text);
  if (!parsed) return null;
  const keys = ['handleX', 'handleY', 'targetX', 'targetY'] as const;
  const out: Record<string, number> = {};
  for (const k of keys) {
    const v = parsed[k];
    if (typeof v !== 'number' || !Number.isFinite(v)) return null;
    out[k] = v;
  }
  return out as { handleX: number; handleY: number; targetX: number; targetY: number };
}
