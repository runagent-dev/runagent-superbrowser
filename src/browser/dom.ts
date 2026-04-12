/**
 * DOM tree builder and interactive element indexer.
 *
 * Injects JS to analyze the page DOM, builds a structured tree,
 * and formats it for LLM consumption.
 *
 * Pattern from nanobrowser (dom/views.ts), written from scratch.
 */

import type { Page } from 'puppeteer-core';
import { getBuildDomTreeScript } from './dom-scripts.js';

/** Attributes to include in the formatted element tree output. */
const DEFAULT_INCLUDE_ATTRIBUTES = [
  'title', 'type', 'name', 'role', 'placeholder',
  'aria-label', 'value', 'alt', 'href', 'aria-expanded',
];

export class DOMTextNode {
  readonly type = 'TEXT' as const;
  constructor(
    public text: string,
    public isVisible: boolean,
  ) {}
}

export class DOMElementNode {
  readonly type = 'ELEMENT' as const;

  constructor(
    public tagName: string,
    public xpath: string,
    public attributes: Record<string, string>,
    public text: string,
    public isInteractive: boolean,
    public isVisible: boolean,
    public isInViewport: boolean,
    public highlightIndex: number | null,
    public children: (DOMElementNode | DOMTextNode)[],
    public isNew: boolean | null = null,
  ) {}

  /** Get all text from this element until the next clickable element. */
  getAllTextTillNextClickableElement(maxDepth: number = -1): string {
    const parts: string[] = [];
    const collect = (node: DOMElementNode | DOMTextNode, depth: number): void => {
      if (maxDepth >= 0 && depth > maxDepth) return;
      if (node instanceof DOMTextNode) {
        if (node.text.trim()) parts.push(node.text.trim());
      } else if (node instanceof DOMElementNode) {
        if (node.text.trim()) parts.push(node.text.trim());
        for (const child of node.children) {
          if (child instanceof DOMElementNode && child.highlightIndex !== null) continue;
          collect(child, depth + 1);
        }
      }
    };
    collect(this, 0);
    return parts.join(' ').trim();
  }

  /**
   * Format interactive elements as a string for LLM consumption.
   *
   * Output format (proven by nanobrowser):
   *   [1]<input type="text" placeholder="Search..." />
   *   [2]<button>Login</button>
   */
  clickableElementsToString(includeAttributes: string[] = DEFAULT_INCLUDE_ATTRIBUTES): string {
    const lines: string[] = [];

    const processNode = (node: DOMElementNode | DOMTextNode, depth: number): void => {
      if (node instanceof DOMTextNode) return;

      const el = node;
      if (el.highlightIndex !== null) {
        const depthStr = '\t'.repeat(depth);

        // Collect attributes to include, de-duplicating values
        const attrs: Record<string, string> = {};
        for (const key of includeAttributes) {
          const val = el.attributes[key];
          if (val && val.trim()) {
            attrs[key] = val.trim();
          }
        }

        // Remove attribute if tag name matches role
        if (el.tagName === attrs['role']) {
          delete attrs['role'];
        }

        // Remove attribute if its value matches the element text
        const elText = el.getAllTextTillNextClickableElement(2);
        const textMatchAttrs = ['aria-label', 'placeholder', 'title'];
        for (const attr of textMatchAttrs) {
          if (attrs[attr]?.toLowerCase() === elText.toLowerCase()) {
            delete attrs[attr];
          }
        }

        // De-duplicate attribute values (keep first occurrence)
        const seenValues = new Map<string, string>();
        const keysToRemove = new Set<string>();
        for (const [key, value] of Object.entries(attrs)) {
          if (value.length > 5) {
            const existing = seenValues.get(value);
            if (existing) {
              keysToRemove.add(key);
            } else {
              seenValues.set(value, key);
            }
          }
        }
        for (const key of keysToRemove) {
          delete attrs[key];
        }

        // Build line
        const indicator = el.isNew ? `*[${el.highlightIndex}]` : `[${el.highlightIndex}]`;
        let line = `${depthStr}${indicator}<${el.tagName}`;
        const attrEntries = Object.entries(attrs);
        if (attrEntries.length > 0) {
          const attrStr = attrEntries
            .map(([k, v]) => `${k}=${capLength(v, 15)}`)
            .join(' ');
          line += ` ${attrStr}`;
        }
        if (elText) {
          line += `>${elText.substring(0, 80).trim()}`;
        }
        line += ' />';
        lines.push(line);
      }

      for (const child of el.children) {
        const nextDepth = el.highlightIndex !== null ? depth + 1 : depth;
        processNode(child, nextDepth);
      }
    };

    processNode(this, 0);
    return lines.join('\n');
  }

  /**
   * Generate an enhanced CSS selector for this element.
   * Pattern from nanobrowser (dom/views.ts enhancedCssSelectorForElement).
   */
  enhancedCssSelectorForElement(): string {
    try {
      if (!this.xpath) return '';

      let selector = xpathToCssSelector(this.xpath);

      // Add safe attributes for specificity
      const safeAttrs = new Set([
        'id', 'name', 'type', 'placeholder', 'aria-label',
        'role', 'for', 'autocomplete', 'alt', 'title',
        'data-testid', 'data-id',
      ]);

      for (const [attr, value] of Object.entries(this.attributes)) {
        if (attr === 'class' || !attr.trim()) continue;
        if (!safeAttrs.has(attr)) continue;
        if (/["'<>`\n\r\t]/.test(value)) continue;
        selector += `[${attr}="${value}"]`;
      }

      return selector;
    } catch {
      return `${this.tagName || '*'}[highlightIndex='${this.highlightIndex}']`;
    }
  }
}

export interface DOMState {
  elementTree: DOMElementNode;
  selectorMap: Map<number, DOMElementNode>;
}

export interface SelectorEntry {
  index: number;
  xpath: string;
  tagName: string;
  attributes: Record<string, string>;
  text: string;
  bounds?: { x: number; y: number; width: number; height: number } | null;
  role?: string;
}

export interface PageState extends DOMState {
  url: string;
  title: string;
  screenshot?: string;
  scrollY: number;
  scrollHeight: number;
  viewportHeight: number;
  accessibilityTree?: string;
  pendingDialogs?: DialogInfo[];
  consoleErrors?: string[];
  /** Bounds + metadata per indexed interactive element (for bbox overlays). */
  selectorEntries?: SelectorEntry[];
}

export interface DialogInfo {
  type: string;
  message: string;
  defaultValue?: string;
}

/**
 * Build the DOM tree from a puppeteer page.
 * Returns a structured tree with interactive elements indexed.
 *
 * If `priorSelectorMap` is passed, elements whose (xpath + attrs) don't
 * appear in the prior map are marked `isNew = true` so the LLM view
 * renders them as `*[n]<tag/>` — browser-use's signal for "this appeared
 * after your last action". The marker resets the next time buildDomTree
 * runs, so it represents "new since the last step" only.
 */
export async function buildDomTree(
  page: Page,
  viewportExpansion: number = 0,
  priorSelectorMap?: Map<number, DOMElementNode> | null,
): Promise<DOMState & { url: string; title: string; scrollY: number; scrollHeight: number; viewportHeight: number; selectorEntries: RawDomResult['selectorEntries']; detectorStats?: RawDomResult['detectorStats'] }> {
  const script = getBuildDomTreeScript();
  const raw = await page.evaluate(`(${script})(${viewportExpansion})`) as RawDomResult;

  const tree = parseRawNode(raw.tree);
  if (!tree || tree instanceof DOMTextNode) {
    throw new Error('Failed to build DOM tree: root is not an element');
  }

  // Build selector map from raw entries
  const selectorMap = new Map<number, DOMElementNode>();
  buildSelectorMap(tree, selectorMap);

  // Mark new elements: hash prior map by (xpath + attrs) and flag any
  // current element whose hash isn't in the prior set.
  if (priorSelectorMap && priorSelectorMap.size > 0) {
    const priorHashes = new Set<string>();
    for (const el of priorSelectorMap.values()) {
      priorHashes.add(identityKey(el));
    }
    for (const el of selectorMap.values()) {
      if (!priorHashes.has(identityKey(el))) {
        el.isNew = true;
      }
    }
  }

  return {
    elementTree: tree,
    selectorMap,
    url: raw.url,
    title: raw.title,
    scrollY: raw.scrollY,
    scrollHeight: raw.scrollHeight,
    viewportHeight: raw.viewportHeight,
    selectorEntries: raw.selectorEntries,
    detectorStats: raw.detectorStats,
  };
}

/** Stable identity key: xpath + canonical attrs. Used for new-element diff. */
function identityKey(el: DOMElementNode): string {
  const attrs = Object.keys(el.attributes || {})
    .sort()
    .map((k) => `${k}=${el.attributes[k]}`)
    .join('&');
  return `${el.xpath}|${attrs}`;
}

// --- Internal helpers ---

interface RawDomResult {
  tree: RawNode;
  selectorEntries: Array<{
    index: number;
    xpath: string;
    tagName: string;
    attributes: Record<string, string>;
    text: string;
    bounds?: { x: number; y: number; width: number; height: number } | null;
    role?: string;
  }>;
  url: string;
  title: string;
  scrollY: number;
  scrollHeight: number;
  viewportHeight: number;
  detectorStats?: {
    candidates: number;
    occluded: number;
    shadowed: number;
    indexed: number;
  };
}

interface RawNode {
  type: 'ELEMENT' | 'TEXT';
  text?: string;
  tagName?: string;
  xpath?: string;
  attributes?: Record<string, string>;
  isInteractive?: boolean;
  isVisible?: boolean;
  isInViewport?: boolean;
  highlightIndex?: number | null;
  children?: RawNode[];
}

function parseRawNode(raw: RawNode | null): DOMElementNode | DOMTextNode | null {
  if (!raw) return null;

  if (raw.type === 'TEXT') {
    return new DOMTextNode(raw.text || '', raw.isVisible !== false);
  }

  const children: (DOMElementNode | DOMTextNode)[] = [];
  if (raw.children) {
    for (const child of raw.children) {
      const parsed = parseRawNode(child);
      if (parsed) children.push(parsed);
    }
  }

  return new DOMElementNode(
    raw.tagName || 'div',
    raw.xpath || '',
    raw.attributes || {},
    raw.text || '',
    raw.isInteractive || false,
    raw.isVisible !== false,
    raw.isInViewport || false,
    raw.highlightIndex ?? null,
    children,
  );
}

function buildSelectorMap(node: DOMElementNode | DOMTextNode, map: Map<number, DOMElementNode>): void {
  if (node instanceof DOMTextNode) return;
  if (node.highlightIndex !== null) {
    map.set(node.highlightIndex, node);
  }
  for (const child of node.children) {
    buildSelectorMap(child, map);
  }
}

function capLength(text: string, max: number): string {
  if (text.length <= max) return text;
  return text.substring(0, max) + '...';
}

function xpathToCssSelector(xpath: string): string {
  const parts = xpath.split('/').filter(Boolean);
  const cssParts: string[] = [];
  for (const part of parts) {
    const match = part.match(/^(\w+)\[(\d+)\]$/);
    if (match) {
      cssParts.push(`${match[1]}:nth-of-type(${match[2]})`);
    } else {
      cssParts.push(part);
    }
  }
  return cssParts.join(' > ');
}
