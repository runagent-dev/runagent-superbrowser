/**
 * Cursor-interactive element detection from BrowserOS.
 *
 * Finds elements with cursor:pointer that the accessibility tree misses.
 * These are common in modern web apps: divs with click handlers,
 * custom components, styled spans acting as buttons, etc.
 */

import type { Page } from 'puppeteer-core';

export interface CursorElement {
  tag: string;
  text: string;
  id?: string;
  className?: string;
  role?: string;
  ariaLabel?: string;
  rect: { x: number; y: number; width: number; height: number };
}

/**
 * Find all visible elements with cursor:pointer that are NOT
 * already interactive by standard ARIA roles/tags.
 */
export async function findCursorInteractiveElements(
  page: Page,
): Promise<CursorElement[]> {
  try {
    return await page.evaluate(() => {
      const STANDARD_INTERACTIVE = new Set([
        'A', 'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'DETAILS', 'SUMMARY',
      ]);
      const INTERACTIVE_ROLES = new Set([
        'button', 'link', 'tab', 'menuitem', 'menuitemcheckbox',
        'menuitemradio', 'option', 'switch', 'checkbox', 'radio',
        'combobox', 'textbox', 'searchbox', 'slider', 'spinbutton',
      ]);

      const results: Array<{
        tag: string;
        text: string;
        id?: string;
        className?: string;
        role?: string;
        ariaLabel?: string;
        rect: { x: number; y: number; width: number; height: number };
      }> = [];

      const seen = new Set<Element>();

      // Walk all visible elements
      const all = document.querySelectorAll('*');
      for (const el of all) {
        if (seen.has(el)) continue;

        const htmlEl = el as HTMLElement;
        const style = window.getComputedStyle(el);

        // Skip invisible
        if (style.display === 'none' || style.visibility === 'hidden') continue;

        // Check cursor:pointer
        if (style.cursor !== 'pointer') continue;

        // Skip standard interactive elements (already in AX tree)
        if (STANDARD_INTERACTIVE.has(el.tagName)) continue;

        // Skip elements with interactive ARIA roles
        const role = el.getAttribute('role');
        if (role && INTERACTIVE_ROLES.has(role)) continue;

        // Skip if a parent has cursor:pointer (inherited — not self-interactive)
        const parent = el.parentElement;
        if (parent) {
          const parentStyle = window.getComputedStyle(parent);
          if (parentStyle.cursor === 'pointer') {
            // Only include if this element has its own click handler
            if (!el.hasAttribute('onclick') && !el.hasAttribute('tabindex')) {
              continue;
            }
          }
        }

        // Get bounding rect
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) continue;

        // Must be in viewport
        const vw = window.innerWidth;
        const vh = window.innerHeight;
        if (rect.bottom < 0 || rect.right < 0 || rect.top > vh || rect.left > vw) continue;

        const text = (htmlEl.innerText || htmlEl.textContent || '').trim();
        if (!text && !el.getAttribute('aria-label') && !el.getAttribute('title')) continue;

        seen.add(el);
        results.push({
          tag: el.tagName.toLowerCase(),
          text: text.substring(0, 100),
          id: el.id || undefined,
          className: el.className ? String(el.className).substring(0, 50) : undefined,
          role: role || undefined,
          ariaLabel: el.getAttribute('aria-label') || undefined,
          rect: {
            x: Math.round(rect.left),
            y: Math.round(rect.top),
            width: Math.round(rect.width),
            height: Math.round(rect.height),
          },
        });

        // Limit to avoid huge lists
        if (results.length >= 50) break;
      }

      return results;
    });
  } catch {
    return [];
  }
}

/**
 * Format cursor elements for LLM consumption.
 */
export function formatCursorElements(elements: CursorElement[]): string {
  if (elements.length === 0) return '';

  const lines = elements.map((el, i) => {
    let desc = `  ${el.tag}`;
    if (el.role) desc += ` role="${el.role}"`;
    if (el.ariaLabel) desc += ` aria-label="${el.ariaLabel}"`;
    if (el.id) desc += ` id="${el.id}"`;
    desc += ` "${el.text.substring(0, 50)}"`;
    desc += ` at (${el.rect.x}, ${el.rect.y})`;
    return desc;
  });

  return `\nCursor-interactive elements (not in element tree):\n${lines.join('\n')}`;
}
