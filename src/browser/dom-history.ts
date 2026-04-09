/**
 * DOM element tracking via hash-based identity.
 *
 * Adapted from nanobrowser's dom/history/ system. Allows finding
 * and comparing DOM elements across page mutations by hashing
 * their branch path, attributes, and xpath.
 */

import crypto from 'crypto';
import { DOMElementNode } from './dom.js';

// --- Types ---

export interface Coordinates {
  x: number;
  y: number;
}

export interface CoordinateSet {
  topLeft: Coordinates;
  topRight: Coordinates;
  bottomLeft: Coordinates;
  bottomRight: Coordinates;
  center: Coordinates;
  width: number;
  height: number;
}

export interface ViewportInfo {
  scrollX: number | null;
  scrollY: number | null;
  width: number;
  height: number;
}

export class HashedDomElement {
  constructor(
    public branchPathHash: string,
    public attributesHash: string,
    public xpathHash: string,
  ) {}
}

export class DOMHistoryElement {
  constructor(
    public tagName: string,
    public xpath: string,
    public highlightIndex: number | null,
    public entireParentBranchPath: string[],
    public attributes: Record<string, string>,
    public shadowRoot = false,
    public cssSelector: string | null = null,
    public pageCoordinates: CoordinateSet | null = null,
    public viewportCoordinates: CoordinateSet | null = null,
    public viewportInfo: ViewportInfo | null = null,
  ) {}

  toDict(): Record<string, unknown> {
    return {
      tagName: this.tagName,
      xpath: this.xpath,
      highlightIndex: this.highlightIndex,
      entireParentBranchPath: this.entireParentBranchPath,
      attributes: this.attributes,
      shadowRoot: this.shadowRoot,
      cssSelector: this.cssSelector,
      pageCoordinates: this.pageCoordinates,
      viewportCoordinates: this.viewportCoordinates,
      viewportInfo: this.viewportInfo,
    };
  }
}

// --- Hashing ---

function createSHA256Hash(input: string): string {
  return crypto.createHash('sha256').update(input).digest('hex');
}

function parentBranchPathHash(path: string[]): string {
  if (path.length === 0) return '';
  return createSHA256Hash(path.join('/'));
}

function attributesHash(attributes: Record<string, string>): string {
  const str = Object.entries(attributes)
    .map(([key, value]) => `${key}=${value}`)
    .join('');
  return createSHA256Hash(str);
}

function xpathHash(xpath: string): string {
  return createSHA256Hash(xpath);
}

/** Get the branch path from parent elements. */
function getParentBranchPath(domElement: DOMElementNode): string[] {
  const parents: DOMElementNode[] = [];
  let current = domElement;

  // Walk up if parent is available (requires parent reference on DOMElementNode)
  // For now, just use the element's own tagName as the path
  parents.push(current);
  return parents.map((p) => p.tagName);
}

// --- Public API ---

/** Hash a DOM element for comparison. */
export function hashDomElement(domElement: DOMElementNode): HashedDomElement {
  const branchPath = getParentBranchPath(domElement);
  return new HashedDomElement(
    parentBranchPathHash(branchPath),
    attributesHash(domElement.attributes),
    xpathHash(domElement.xpath),
  );
}

/** Hash a history element for comparison. */
export function hashDomHistoryElement(historyElement: DOMHistoryElement): HashedDomElement {
  return new HashedDomElement(
    parentBranchPathHash(historyElement.entireParentBranchPath),
    attributesHash(historyElement.attributes),
    xpathHash(historyElement.xpath),
  );
}

/** Convert a live DOM element to a history element for storage. */
export function convertToHistoryElement(domElement: DOMElementNode): DOMHistoryElement {
  const branchPath = getParentBranchPath(domElement);
  return new DOMHistoryElement(
    domElement.tagName,
    domElement.xpath,
    domElement.highlightIndex,
    branchPath,
    domElement.attributes,
  );
}

/**
 * Find a history element in a DOM tree by matching hashes.
 * Returns the matching DOMElementNode or null.
 */
export function findHistoryElementInTree(
  historyElement: DOMHistoryElement,
  tree: DOMElementNode,
): DOMElementNode | null {
  const targetHash = hashDomHistoryElement(historyElement);

  const search = (node: DOMElementNode): DOMElementNode | null => {
    if (node.highlightIndex != null) {
      const nodeHash = hashDomElement(node);
      if (
        nodeHash.branchPathHash === targetHash.branchPathHash &&
        nodeHash.attributesHash === targetHash.attributesHash &&
        nodeHash.xpathHash === targetHash.xpathHash
      ) {
        return node;
      }
    }
    for (const child of node.children) {
      if (child instanceof DOMElementNode) {
        const result = search(child);
        if (result) return result;
      }
    }
    return null;
  };

  return search(tree);
}

/**
 * Compare whether a history element and a live DOM element
 * represent the same element.
 */
export function compareElements(
  historyElement: DOMHistoryElement,
  domElement: DOMElementNode,
): boolean {
  const histHash = hashDomHistoryElement(historyElement);
  const domHash = hashDomElement(domElement);
  return (
    histHash.branchPathHash === domHash.branchPathHash &&
    histHash.attributesHash === domHash.attributesHash &&
    histHash.xpathHash === domHash.xpathHash
  );
}

/** Convenience namespace for all history operations. */
export const HistoryTreeProcessor = {
  convertToHistoryElement,
  findHistoryElementInTree,
  compareElements,
  hashDomElement,
  hashDomHistoryElement,
  getParentBranchPath,
};
