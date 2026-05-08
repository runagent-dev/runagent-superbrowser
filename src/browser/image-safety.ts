/**
 * Image-safety harness. Every screenshot that heads to an LLM goes through
 * here first. Purpose: never hand OpenAI / Gemini a payload that will 400.
 *
 * Two classes of rejection this prevents:
 *   1. Size: the most common cause of "Non-transient LLM error with image
 *      content" — Gemini in particular rejects images >~1.5MB silently.
 *      We clamp to 2MB default, 1.5MB for Gemini.
 *   2. Metadata: some Gemini validators reject JPEGs with embedded ICC
 *      color profiles or EXIF. sharp strips both on re-encode by default.
 *
 * Vision cost also scales with pixel count, so capping dimensions at
 * 1568px/side is both a safety and a cost measure.
 */

import sharp from 'sharp';

/** Hard dimension cap — longest side in pixels. OpenAI's vision scales
 *  linearly with pixel count above this, so anything larger pays more
 *  without adding fidelity. */
export const MAX_SIDE_PX = 1568;

/** Default byte budget. 2MB is comfortably below OpenAI's 20MB limit and
 *  leaves headroom for the JSON envelope. */
export const MAX_BYTES = 2_000_000;

/** Starting JPEG quality. Lower than Puppeteer's default 70 so first pass
 *  usually fits under MAX_BYTES without re-encoding. */
export const START_QUALITY = 70;

/** Floor quality — below this images visibly degrade, so we halve
 *  dimensions instead. */
export const MIN_QUALITY = 35;

/** Gemini's OpenAI-compatible endpoint has tighter size limits than
 *  OpenAI proper. Re-sanitize with this cap for Gemini requests. */
export const GEMINI_MAX_BYTES = 1_500_000;

/** Gemini's compat layer rejects requests with too many inline images.
 *  Keep the last N; older screenshots are stale anyway. */
export const GEMINI_MAX_IMAGES_PER_REQUEST = 3;

export interface SanitizedImage {
  buffer: Buffer;
  mime: 'image/jpeg';
  width: number;
  height: number;
}

export interface SanitizeOptions {
  maxBytes?: number;
}

/**
 * Re-encode a screenshot buffer to JPEG within the byte/dimension budget.
 * Strips EXIF/ICC/color profile as a side effect of the re-encode.
 *
 * Strategy: decode → downscale to MAX_SIDE_PX if larger → JPEG q70 →
 * iteratively drop quality by 10 until under maxBytes. If still over at
 * MIN_QUALITY, halve dimensions once and retry from q70. After that we
 * return what we have; a >2MB screenshot at 784px/q35 is already an
 * outlier and further shrinking defeats the point.
 */
export async function sanitizeImageBuffer(
  buf: Buffer,
  opts: SanitizeOptions = {},
): Promise<SanitizedImage> {
  const maxBytes = opts.maxBytes ?? MAX_BYTES;

  // Decode once and read metadata so subsequent encodes don't re-decode.
  const meta = await sharp(buf).metadata();
  let width = meta.width ?? 0;
  let height = meta.height ?? 0;

  // If metadata is missing (rare, corrupt input), fall back to a single
  // q70 JPEG encode and return whatever we get.
  if (!width || !height) {
    const fallback = await sharp(buf).jpeg({ quality: START_QUALITY, mozjpeg: true }).toBuffer();
    return { buffer: fallback, mime: 'image/jpeg', width: 0, height: 0 };
  }

  const longest = Math.max(width, height);
  let scale = longest > MAX_SIDE_PX ? MAX_SIDE_PX / longest : 1;

  // Up to two downscale rounds (initial + halve-on-overflow).
  for (let round = 0; round < 2; round++) {
    const targetW = Math.max(1, Math.round(width * scale));
    const targetH = Math.max(1, Math.round(height * scale));

    // sharp pipeline: rotate() honors EXIF orientation (we strip EXIF, so
    // this is important before strip), resize only if shrinking, then JPEG.
    const base = sharp(buf)
      .rotate()
      .resize({ width: targetW, height: targetH, fit: 'inside', withoutEnlargement: true });

    let quality = START_QUALITY;
    let out = await base.clone().jpeg({ quality, mozjpeg: true }).toBuffer();
    while (out.byteLength > maxBytes && quality > MIN_QUALITY) {
      quality -= 10;
      out = await base.clone().jpeg({ quality, mozjpeg: true }).toBuffer();
    }
    if (out.byteLength <= maxBytes || round === 1) {
      return { buffer: out, mime: 'image/jpeg', width: targetW, height: targetH };
    }
    // Still over budget at MIN_QUALITY — halve dims and retry.
    scale = scale * 0.5;
  }

  // Unreachable — loop above returns on round 1 regardless.
  const final = await sharp(buf)
    .jpeg({ quality: MIN_QUALITY, mozjpeg: true })
    .toBuffer();
  return { buffer: final, mime: 'image/jpeg', width, height };
}

export interface SanitizedBase64 {
  b64: string;
  mime: 'image/jpeg';
  width: number;
  height: number;
  bytes: number;
}

/**
 * Base64-in / base64-out wrapper. Also strips stray whitespace and
 * newlines from the input — Gemini's OpenAI-compat layer is strict about
 * `data:image/jpeg;base64,<no-whitespace>` and will 400 on pretty-printed
 * or line-wrapped base64.
 */
export async function sanitizeBase64Image(
  b64: string,
  opts: SanitizeOptions = {},
): Promise<SanitizedBase64> {
  // Strip data-URI prefix if present, then all whitespace.
  const cleaned = b64.replace(/^data:[^;]+;base64,/, '').replace(/\s+/g, '');
  const buf = Buffer.from(cleaned, 'base64');
  const san = await sanitizeImageBuffer(buf, opts);
  const outB64 = san.buffer.toString('base64');
  return {
    b64: outB64,
    mime: 'image/jpeg',
    width: san.width,
    height: san.height,
    bytes: san.buffer.byteLength,
  };
}

/**
 * Heuristic: did this error come from the model saying "your prompt is
 * too long" rather than "your prompt is malformed"?
 *
 * Used by LLMProvider to decide whether to trim context and retry vs.
 * propagate. Kept as string-match because the OpenAI SDK surfaces these
 * as plain 400 errors with human-readable messages — no structured code.
 */
export function isTokenOverflow400(err: unknown): boolean {
  const msg = err instanceof Error ? err.message : String(err ?? '');
  return /maximum context length|context_length_exceeded|reduce the length|too many tokens|input is too long|prompt is too long/i.test(msg);
}
