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
 * 16-hex-char SHA1 prefix is small enough to round-trip over HTTP.
 */

import { createHash } from 'crypto';
import type { DOMElementNode } from './dom.js';

function firstChars(s: string | undefined, n: number): string {
  if (!s) return '';
  const trimmed = s.replace(/\s+/g, ' ').trim();
  return trimmed.length > n ? trimmed.slice(0, n) : trimmed;
}

function xpathTail(xpath: string, segments: number = 2): string {
  const parts = xpath.split('/').filter(Boolean);
  return parts.slice(-segments).join('/');
}

export function fingerprintElement(el: DOMElementNode): string {
  const tag = el.tagName.toLowerCase();
  const role = el.attributes?.['role'] ?? '';
  const aria = el.attributes?.['aria-label'] ?? '';
  const placeholder = el.attributes?.['placeholder'] ?? '';
  const title = el.attributes?.['title'] ?? '';
  const name = el.attributes?.['name'] ?? '';
  const text = el.getAllTextTillNextClickableElement(2);

  const primaryLabel = firstChars(aria || placeholder || title || text || name, 40);
  const tail = xpathTail(el.xpath, 2);

  const payload = `${tag}|${role}|${primaryLabel}|${tail}`;
  return createHash('sha1').update(payload).digest('hex').slice(0, 16);
}

/** Produce {index: fingerprint} for every interactive element in the selectorMap. */
export function fingerprintMap(
  selectorMap: Map<number, DOMElementNode>,
): Record<number, string> {
  const out: Record<number, string> = {};
  for (const [idx, el] of selectorMap) {
    out[idx] = fingerprintElement(el);
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
