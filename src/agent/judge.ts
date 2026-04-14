/**
 * Independent verifier for captcha-solve claims.
 *
 * Ported pattern from browser-use/browser_use/agent/judge.py:44-225 — a
 * separate LLM call examines a pair of before/after screenshots and a
 * claim ("the reCAPTCHA was solved") and returns {verdict, reasoning}.
 *
 * Purpose: captcha solvers can return solved=true when the backend
 * actually rejected the token (e.g., token was wrong, site issued a new
 * challenge). The orchestrator calls the judge only on reported-success
 * events, not every step, to keep cost down.
 *
 * Invocation budget-wise: ~0.3c per verdict (one vision call).
 */

import type { Page } from 'puppeteer-core';
import type { LLMProvider } from '../llm/provider.js';
import { sanitizeImageBuffer } from '../browser/image-safety.js';

export interface JudgeInput {
  /** What the solver claimed. e.g., "reCAPTCHA solved via grid method". */
  claim: string;
  /** Screenshot taken BEFORE the solver ran (base64 JPEG, no prefix). */
  beforeB64?: string;
  /** Screenshot taken AFTER the solver claims success. */
  afterB64: string;
  /** Captcha type for grounding the verdict prompt. */
  captchaType?: string;
  /** URL for additional context. */
  url?: string;
}

export interface JudgeVerdict {
  /** True if the judge agrees the captcha was actually solved. */
  verdict: boolean;
  /** Short reasoning trace (max ~2 sentences). */
  reasoning: string;
  /** Copy of the raw LLM content for debugging. */
  raw?: string;
}

const JUDGE_SYSTEM = `You are an independent verifier for browser automation.
Your job: given a screenshot of a web page AFTER a captcha solver ran,
decide whether the page actually cleared the captcha challenge.

Return ONLY a JSON object of the form:
{"verdict": true|false, "reasoning": "one short sentence"}

Verdict TRUE criteria:
- The captcha widget is gone, or
- The page has navigated past the challenge, or
- An explicit success indicator (checkmark, "Verification complete") is visible.

Verdict FALSE criteria:
- The captcha widget is still visible and not marked solved.
- A new challenge appeared (e.g., a fresh image grid, "Please try again").
- The page shows a block page ("Access denied", "Please verify you are human").

Do NOT be charitable — if there's any sign the captcha is still active, return false.`;

function buildUserContent(input: JudgeInput): Array<Record<string, unknown>> {
  const parts: Array<Record<string, unknown>> = [];
  const header =
    `Claim: ${input.claim}\n` +
    (input.captchaType ? `Captcha type: ${input.captchaType}\n` : '') +
    (input.url ? `URL: ${input.url}\n` : '');
  parts.push({ type: 'text', text: header });
  if (input.beforeB64) {
    parts.push({ type: 'text', text: 'Screenshot BEFORE solver:' });
    parts.push({
      type: 'image_url',
      image_url: { url: `data:image/jpeg;base64,${input.beforeB64}` },
    });
  }
  parts.push({ type: 'text', text: 'Screenshot AFTER solver claimed success:' });
  parts.push({
    type: 'image_url',
    image_url: { url: `data:image/jpeg;base64,${input.afterB64}` },
  });
  parts.push({
    type: 'text',
    text: 'Respond with ONLY a JSON object: {"verdict": bool, "reasoning": "..."}',
  });
  return parts;
}

/** Ask the judge whether the claim holds. Falls back to verdict=false on error. */
export async function verifyCaptchaSolve(
  llm: LLMProvider,
  input: JudgeInput,
): Promise<JudgeVerdict> {
  try {
    const resp = await llm.chat([
      { role: 'system', content: JUDGE_SYSTEM },
      // Cast through unknown: ChatMessage.content accepts ContentBlock[] but
      // we pass through a shape identical to what LLMProvider.chat maps.
      { role: 'user', content: buildUserContent(input) as unknown as never },
    ], { temperature: 0.0, maxTokens: 200 });

    const raw = resp.content || '';
    const match = raw.match(/\{[^{}]*"verdict"[^{}]*\}/);
    if (!match) {
      return { verdict: false, reasoning: 'judge response did not contain a verdict JSON', raw };
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(match[0]);
    } catch {
      return { verdict: false, reasoning: 'judge verdict JSON was malformed', raw };
    }
    if (parsed && typeof parsed === 'object' && 'verdict' in parsed) {
      const verdict = Boolean((parsed as Record<string, unknown>).verdict);
      const reasoning = String((parsed as Record<string, unknown>).reasoning ?? '');
      return { verdict, reasoning, raw };
    }
    return { verdict: false, reasoning: 'judge returned no verdict field', raw };
  } catch (e) {
    return {
      verdict: false,
      reasoning: `judge call failed: ${(e as Error).message}`,
    };
  }
}

/** Convenience: capture a screenshot the caller can pass to verifyCaptchaSolve. */
export async function captureJpegB64(page: Page, quality = 75): Promise<string> {
  const buf = await page.screenshot({ type: 'jpeg', quality, fullPage: false });
  const san = await sanitizeImageBuffer(Buffer.from(buf));
  return san.buffer.toString('base64');
}
