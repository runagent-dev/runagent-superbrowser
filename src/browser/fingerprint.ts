/**
 * Element identity fingerprint — used to catch stale-index clicks.
 *
 * Problem this fixes: the LLM sees the screenshot/elements at time T1
 * with [10] = "Login button". By T2 when the click fires, the DOM has
 * re-rendered and [10] is now "Signup button". Clicking [10] succeeds,
 * but the user experience is wrong. Nanobot has no signal for this.
 *
 * Fix: record a per-index identity fingerprint whenever state is fetched
 * for the LLM. At click time, compare the cached fingerprint against the
 * current [N]'s fingerprint. If they differ, the DOM shifted — reject
 * the click with a structured error and point at the new index of the
 * element the LLM was targeting (if it still exists).
 *
 * Fingerprint design: stable under cosmetic DOM jitter (attribute order,
 * whitespace) but sensitive to actual element swaps. Includes:
 *   - tagName + role
 *   - first 40 chars of aria-label / placeholder / title / visible text
 *   - xpath tail (last 2 segments — whole xpath is too brittle to
 *     sibling reorder, but the tail of it usually survives)
 *   - boundsBucket (50px-bucketed top-left + size) — distinguishes
 *     visually-identical sibling elements (12 filter checkboxes with
 *     the same aria-label are at different on-screen positions).
 *     Without this, after a filter re-render that re-sorts a list, the
 *     new occupant of [N] hashed identically to the old occupant and
 *     the stale-guard let the click through silently.
 *   - siblingPos — position among same-tag siblings of the immediate
 *     parent. Stable under list filtering when the parent's children
 *     stay the same set; complements boundsBucket on layouts that
 *     keep the same screen position but swap which element occupies it.
 * 16-hex-char SHA1 prefix is small enough to round-trip over HTTP.
 */

import { createHash } from 'crypto';
import type { DOMElementNode, SelectorEntry } from './dom.js';

function firstChars(s: string | undefined, n: number): string {
  if (!s) return '';
  const trimmed = s.replace(/\s+/g, ' ').trim();
  return trimmed.length > n ? trimmed.slice(0, n) : trimmed;
}

function xpathTail(xpath: string, segments: number = 2): string {
  const parts = xpath.split('/').filter(Boolean);
  return parts.slice(-segments).join('/');
}

/** Last segment's bracket index — e.g., `/div[3]/button[1]` → "1".
 *  Stable under list filtering when the parent's child set is fixed
 *  (filter chip toggled but same chips present), differs when a new
 *  element shows up at the same xpath_tail.
 */
function siblingPos(xpath: string): string {
  const parts = xpath.split('/').filter(Boolean);
  const last = parts[parts.length - 1] ?? '';
  const m = last.match(/\[(\d+)\]\s*$/);
  return m ? m[1] : '';
}

/** 50-pixel-bucket from selector-entry bounds. Returns "" when bounds
 *  unknown (callers without selectorEntries access). Buckets absorb
 *  anti-aliasing/scroll jitter; survive only real position shifts.
 */
function boundsBucket(entry?: SelectorEntry | null): string {
  if (!entry || !entry.bounds) return '';
  const b = entry.bounds;
  if (typeof b.x !== 'number' || typeof b.y !== 'number') return '';
  const bx = Math.floor(b.x / 50);
  const by = Math.floor(b.y / 50);
  const bw = Math.floor((b.width || 0) / 50);
  const bh = Math.floor((b.height || 0) / 50);
  return `${bx},${by},${bw},${bh}`;
}

export function fingerprintElement(
  el: DOMElementNode,
  entry?: SelectorEntry | null,
): string {
  const tag = el.tagName.toLowerCase();
  const role = el.attributes?.['role'] ?? '';
  const aria = el.attributes?.['aria-label'] ?? '';
  const placeholder = el.attributes?.['placeholder'] ?? '';
  const title = el.attributes?.['title'] ?? '';
  const name = el.attributes?.['name'] ?? '';
  const text = el.getAllTextTillNextClickableElement(2);

  const primaryLabel = firstChars(aria || placeholder || title || text || name, 40);
  const tail = xpathTail(el.xpath, 2);
  const sibPos = siblingPos(el.xpath);
  const bounds = boundsBucket(entry);

  const payload = `${tag}|${role}|${primaryLabel}|${tail}|${sibPos}|${bounds}`;
  return createHash('sha1').update(payload).digest('hex').slice(0, 16);
}

/** Produce {index: fingerprint} for every interactive element in the
 *  selectorMap. When `selectorEntries` is provided, fingerprints include
 *  the spatial bounds bucket (drops collision rate near zero).
 */
export function fingerprintMap(
  selectorMap: Map<number, DOMElementNode>,
  selectorEntries?: SelectorEntry[] | null,
): Record<number, string> {
  // Index entries by `.index` so per-element lookup is O(1).
  const entriesByIndex = new Map<number, SelectorEntry>();
  if (selectorEntries) {
    for (const e of selectorEntries) {
      if (typeof e.index === 'number') entriesByIndex.set(e.index, e);
    }
  }
  const out: Record<number, string> = {};
  for (const [idx, el] of selectorMap) {
    out[idx] = fingerprintElement(el, entriesByIndex.get(idx) ?? null);
  }
  return out;
}

/** Invert a fingerprint map so we can look up "which index does fingerprint X live at now". */
export function invertFingerprintMap(
  map: Record<number, string>,
): Record<string, number> {
  const inv: Record<string, number> = {};
  for (const [idx, fp] of Object.entries(map)) {
    // First-wins: if two elements share a fingerprint (unlikely at 64-bit hash),
    // prefer the lower index for deterministic suggestions.
    if (!(fp in inv)) inv[fp] = Number(idx);
  }
  return inv;
}
