/**
 * Token counting utilities.
 */

/** Approximate token count from character count. */
export function estimateTokens(text: string): number {
  return Math.ceil(text.length / 3);
}

/** Approximate token cost of a base64 JPEG screenshot. */
export const IMAGE_TOKEN_COST = 800;
