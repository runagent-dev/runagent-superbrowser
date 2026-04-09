/**
 * Element coordinate resolution with 3-tier fallback from BrowserOS.
 *
 * Resolves the center coordinates of a DOM element using:
 * 1. DOM.getContentQuads (most precise)
 * 2. DOM.getBoxModel (content box fallback)
 * 3. Runtime.callFunctionOn with getBoundingClientRect (JS fallback)
 */

import type { CDPSession, Page } from 'puppeteer-core';

export interface ElementCoords {
  x: number;
  y: number;
}

/**
 * Get the center coordinates of an element via CDP.
 * Uses 3-tier fallback for maximum reliability.
 *
 * @param client CDP session
 * @param backendNodeId CDP backend node ID (from AX tree or DOM)
 */
export async function getElementCenter(
  client: CDPSession,
  backendNodeId: number,
): Promise<ElementCoords> {
  // Scroll into view first
  try {
    await client.send('DOM.scrollIntoViewIfNeeded', { backendNodeId });
  } catch {
    // scrollIntoView may fail for some elements — continue
  }

  // Tier 1: DOM.getContentQuads (most precise)
  try {
    const { quads } = (await client.send('DOM.getContentQuads', {
      backendNodeId,
    })) as { quads: number[][] };

    if (quads && quads.length > 0 && quads[0].length >= 8) {
      const quad = quads[0];
      // Quad is 4 points (8 values): x1,y1, x2,y2, x3,y3, x4,y4
      const x = (quad[0] + quad[2] + quad[4] + quad[6]) / 4;
      const y = (quad[1] + quad[3] + quad[5] + quad[7]) / 4;
      if (x > 0 || y > 0) return { x: Math.round(x), y: Math.round(y) };
    }
  } catch {
    // Fallthrough to next tier
  }

  // Tier 2: DOM.getBoxModel (content box)
  try {
    const { model } = (await client.send('DOM.getBoxModel', {
      backendNodeId,
    })) as { model: { content: number[] } };

    if (model && model.content && model.content.length >= 8) {
      const c = model.content;
      const x = (c[0] + c[2] + c[4] + c[6]) / 4;
      const y = (c[1] + c[3] + c[5] + c[7]) / 4;
      if (x > 0 || y > 0) return { x: Math.round(x), y: Math.round(y) };
    }
  } catch {
    // Fallthrough to next tier
  }

  // Tier 3: JS fallback via Runtime.callFunctionOn
  try {
    // Resolve backendNodeId to a remote object
    const { object } = (await client.send('DOM.resolveNode', {
      backendNodeId,
    })) as { object: { objectId: string } };

    if (object?.objectId) {
      const result = (await client.send('Runtime.callFunctionOn', {
        objectId: object.objectId,
        functionDeclaration: `function() {
          const rect = this.getBoundingClientRect();
          return {
            x: Math.round(rect.left + rect.width / 2),
            y: Math.round(rect.top + rect.height / 2),
            width: Math.round(rect.width),
            height: Math.round(rect.height),
          };
        }`,
        returnByValue: true,
      })) as { result: { value: { x: number; y: number; width: number; height: number } } };

      if (result?.result?.value) {
        return { x: result.result.value.x, y: result.result.value.y };
      }
    }
  } catch {
    // All tiers failed
  }

  throw new Error(`Could not resolve coordinates for element (backendNodeId: ${backendNodeId})`);
}

/**
 * Get element center coordinates from the page using a CSS selector.
 * Falls back to evaluate getBoundingClientRect.
 */
export async function getElementCenterBySelector(
  page: Page,
  selector: string,
): Promise<ElementCoords | null> {
  try {
    const result = await page.evaluate((sel: string) => {
      const el = document.querySelector(sel);
      if (!el) return null;
      const rect = el.getBoundingClientRect();
      return {
        x: Math.round(rect.left + rect.width / 2),
        y: Math.round(rect.top + rect.height / 2),
      };
    }, selector);
    return result;
  } catch {
    return null;
  }
}

/**
 * Scroll an element into view using CDP.
 */
export async function scrollIntoView(
  client: CDPSession,
  backendNodeId: number,
): Promise<void> {
  try {
    await client.send('DOM.scrollIntoViewIfNeeded', { backendNodeId });
  } catch {
    // Try JS fallback
    try {
      const { object } = (await client.send('DOM.resolveNode', {
        backendNodeId,
      })) as { object: { objectId: string } };

      if (object?.objectId) {
        await client.send('Runtime.callFunctionOn', {
          objectId: object.objectId,
          functionDeclaration: `function() { this.scrollIntoView({ block: 'center', behavior: 'instant' }); }`,
        });
      }
    } catch {
      // scrollIntoView not critical
    }
  }
}

/**
 * Focus an element via CDP DOM.focus.
 * Pattern from BrowserOS browser.ts.
 */
export async function focusElement(
  client: CDPSession,
  backendNodeId: number,
): Promise<void> {
  await scrollIntoView(client, backendNodeId);
  try {
    await client.send('DOM.focus', { backendNodeId });
  } catch {
    // Fallback: JS focus
    try {
      const { object } = (await client.send('DOM.resolveNode', {
        backendNodeId,
      })) as { object: { objectId: string } };
      if (object?.objectId) {
        await client.send('Runtime.callFunctionOn', {
          objectId: object.objectId,
          functionDeclaration: `function() { this.focus(); }`,
        });
      }
    } catch {
      // Focus may not work on all elements
    }
  }
}

/**
 * Perform a search in the DOM via CDP.
 * Supports CSS selectors, XPath, and text search.
 * Pattern from BrowserOS dom.ts.
 */
export async function domSearchCDP(
  client: CDPSession,
  query: string,
  limit: number = 20,
): Promise<Array<{ nodeId: number; outerHTML: string }>> {
  const results: Array<{ nodeId: number; outerHTML: string }> = [];

  try {
    // Get document root
    const { root } = (await client.send('DOM.getDocument', { depth: 0 })) as {
      root: { nodeId: number };
    };

    // Perform search
    const { searchId, resultCount } = (await client.send('DOM.performSearch', {
      query,
      includeUserAgentShadowDOM: false,
    })) as { searchId: string; resultCount: number };

    if (resultCount > 0) {
      const count = Math.min(resultCount, limit);
      const { nodeIds } = (await client.send('DOM.getSearchResults', {
        searchId,
        fromIndex: 0,
        toIndex: count,
      })) as { nodeIds: number[] };

      for (const nodeId of nodeIds) {
        try {
          const { outerHTML } = (await client.send('DOM.getOuterHTML', {
            nodeId,
          })) as { outerHTML: string };
          results.push({ nodeId, outerHTML: outerHTML.substring(0, 500) });
        } catch {
          // Node may have been removed
        }
      }

      // Clean up search
      await client.send('DOM.discardSearchResults', { searchId }).catch(() => {});
    }
  } catch {
    // DOM search not available
  }

  return results;
}
