/**
 * Screenshot compression and encoding utilities.
 */

import sharp from 'sharp';

/**
 * Compress a PNG screenshot to JPEG with specified quality.
 */
export async function compressScreenshot(
  pngBuffer: Buffer,
  quality: number = 70,
  maxWidth: number = 1280,
): Promise<Buffer> {
  return sharp(pngBuffer)
    .resize({ width: maxWidth, withoutEnlargement: true })
    .jpeg({ quality })
    .toBuffer();
}

/**
 * Convert buffer to base64 string.
 */
export function toBase64(buffer: Buffer): string {
  return buffer.toString('base64');
}

/**
 * Build a vision content message with text and screenshot.
 */
export function buildVisionContent(
  text: string,
  screenshotBase64: string,
): Array<{ type: string; text?: string; image_url?: { url: string } }> {
  return [
    { type: 'text', text },
    {
      type: 'image_url',
      image_url: { url: `data:image/jpeg;base64,${screenshotBase64}` },
    },
  ];
}
