/**
 * Accessibility tree snapshots.
 *
 * Uses CDP Accessibility.getFullAXTree() for semantic page understanding.
 * Pattern from BrowserOS (take_enhanced_snapshot tool).
 */

import type { Page } from 'puppeteer-core';

interface AXNode {
  nodeId: string;
  role: { type: string; value: string };
  name?: { type: string; value: string };
  value?: { type: string; value: string };
  description?: { type: string; value: string };
  children?: string[];
  properties?: Array<{ name: string; value: { type: string; value: unknown } }>;
  parentId?: string;
  ignored?: boolean;
}

/**
 * Get a formatted accessibility tree snapshot from the page.
 * Returns a human-readable string for LLM consumption.
 */
export async function getAccessibilitySnapshot(page: Page): Promise<string> {
  const client = await page.createCDPSession();

  try {
    const { nodes } = (await client.send('Accessibility.getFullAXTree')) as {
      nodes: AXNode[];
    };

    // Build node map
    const nodeMap = new Map<string, AXNode>();
    for (const node of nodes) {
      nodeMap.set(node.nodeId, node);
    }

    // Find root nodes (no parent)
    const roots = nodes.filter((n) => !n.parentId && !n.ignored);

    // Format tree
    const lines: string[] = [];
    const visited = new Set<string>();

    function formatNode(node: AXNode, depth: number): void {
      if (visited.has(node.nodeId)) return;
      visited.add(node.nodeId);

      // Skip ignored nodes and generic containers
      if (node.ignored) return;
      const role = node.role?.value || '';
      if (['none', 'generic', 'InlineTextBox', 'LineBreak'].includes(role)) {
        // Process children but don't display this node
        if (node.children) {
          for (const childId of node.children) {
            const child = nodeMap.get(childId);
            if (child) formatNode(child, depth);
          }
        }
        return;
      }

      const indent = '  '.repeat(depth);
      const name = node.name?.value || '';
      const value = node.value?.value || '';

      let line = `${indent}[${role}]`;
      if (name) line += ` "${name}"`;
      if (value) line += ` value="${value}"`;

      // Add relevant properties
      if (node.properties) {
        for (const prop of node.properties) {
          if (['checked', 'selected', 'expanded', 'disabled', 'required'].includes(prop.name)) {
            if (prop.value.value) {
              line += ` ${prop.name}`;
            }
          }
        }
      }

      lines.push(line);

      // Process children
      if (node.children) {
        for (const childId of node.children) {
          const child = nodeMap.get(childId);
          if (child) formatNode(child, depth + 1);
        }
      }
    }

    for (const root of roots) {
      formatNode(root, 0);
    }

    return lines.join('\n').substring(0, 10000);
  } finally {
    await client.detach().catch(() => {});
  }
}
