/**
 * Grid geometry helpers for captcha solvers.
 *
 * Vision LLMs return absolute pixel coordinates for clicks. To feed
 * VisionMemory the "which tile did I just click" signal needed for
 * human-like click chains, we map (x, y, rect) → tile index. Default
 * grid is 3×3 (reCAPTCHA classic); 4×4 is the other common layout.
 */

import type { Rect } from './validation.js';

export interface GridShape {
  cols: number;
  rows: number;
}

/**
 * Map an absolute click coordinate to a 0..(cols*rows-1) tile index.
 * Returns `null` when the coordinate is outside the rect — caller should
 * treat it as "click was outside the grid, can't record a tile".
 *
 * Layout: row-major, top-left = 0. So for a 3×3 grid:
 *   0 1 2
 *   3 4 5
 *   6 7 8
 */
export function tileIndexFromCoord(
  rect: Rect,
  x: number,
  y: number,
  shape: GridShape = { cols: 3, rows: 3 },
): number | null {
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  if (rect.width <= 0 || rect.height <= 0) return null;
  const relX = x - rect.x;
  const relY = y - rect.y;
  if (relX < 0 || relY < 0 || relX > rect.width || relY > rect.height) return null;
  const col = Math.min(shape.cols - 1, Math.max(0, Math.floor((relX / rect.width) * shape.cols)));
  const row = Math.min(shape.rows - 1, Math.max(0, Math.floor((relY / rect.height) * shape.rows)));
  return row * shape.cols + col;
}
