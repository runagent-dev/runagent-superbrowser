/**
 * Conversation history management with token-aware trimming.
 *
 * Pattern from nanobrowser (messages/service.ts),
 * enhanced with BrowserOS context overflow compaction.
 */

import type { ChatMessage } from '../llm/types.js';
import type { PageState } from '../browser/dom.js';
import type { ActionResult, StepInfo } from './types.js';
import { buildStateMessage } from './state.js';

/** Approximate tokens per character. */
const CHARS_PER_TOKEN = 3;
/**
 * Token cost for an inline image. Dynamic — roughly bytes/750 for a JPEG
 * data URI (base64 inflates ~33% so decoded-bytes ~= base64chars*0.75; a
 * 1MB image ~= 1000 tokens on OpenAI's vision pricing).
 *
 * `fallback` keeps the old behavior for external URLs where we can't
 * measure — was 800 before, keep it as the default.
 */
function imageTokenCost(url: string | undefined, fallback = 800): number {
  if (!url) return fallback;
  if (!url.startsWith('data:')) return fallback;
  const comma = url.indexOf(',');
  if (comma === -1) return fallback;
  const b64Bytes = url.length - comma - 1;
  const cost = Math.ceil(b64Bytes / 750);
  return Math.max(200, Math.min(1600, cost));
}

export class MessageManager {
  private messages: ChatMessage[] = [];
  private maxInputTokens: number;

  constructor(maxInputTokens: number = 60000) {
    this.maxInputTokens = maxInputTokens;
  }

  /**
   * Initialize with system prompt and task.
   *
   * From nanobrowser MessageManager — proper layering:
   * system → task (wrapped in security tags) → example → history marker
   */
  initTask(systemPrompt: string, task: string, sensitiveData?: Record<string, string>): void {
    this.messages = [
      { role: 'system', content: systemPrompt },
      { role: 'user', content: `<nano_user_request>\n${task}\n</nano_user_request>` },
    ];

    // Add sensitive data placeholder info (from nanobrowser)
    if (sensitiveData && Object.keys(sensitiveData).length > 0) {
      const placeholders = Object.keys(sensitiveData)
        .map((k) => `  <secret>${k}</secret>`)
        .join('\n');
      this.messages.push({
        role: 'user',
        content: `The following sensitive data placeholders are available. Use them when filling forms:\n${placeholders}`,
      });
    }

    // History start marker
    this.messages.push({
      role: 'user',
      content: '[Task execution history starts below]',
    });
  }

  /** Add the current browser state as a user message. */
  addStateMessage(
    state: PageState,
    useVision: boolean,
    stepInfo: StepInfo,
  ): void {
    const msg = buildStateMessage(state, useVision, stepInfo);
    this.messages.push(msg);
    this.trimToFit();
  }

  /** Add the model's response as an assistant message. */
  addModelOutput(output: string): void {
    this.messages.push({ role: 'assistant', content: output });
  }

  /** Add plan guidance from the planner. */
  addPlanMessage(plan: string, position?: number): void {
    const msg: ChatMessage = {
      role: 'user',
      content: `[Planner guidance]\n${plan}`,
    };
    if (position !== undefined && position < this.messages.length) {
      this.messages.splice(position, 0, msg);
    } else {
      this.messages.push(msg);
    }
  }

  /** Add action results as a user message. */
  addActionResults(results: ActionResult[]): void {
    const relevant = results.filter((r) => r.includeInMemory || r.error);
    if (relevant.length === 0) return;

    const text = relevant
      .map((r) => {
        if (r.error) return `Error: ${r.error}`;
        return r.extractedContent || 'Action completed';
      })
      .join('\n');

    this.messages.push({
      role: 'user',
      content: `[Action results]\n${text}`,
    });
  }

  /** Add a follow-up task. */
  addNewTask(task: string): void {
    this.messages.push({
      role: 'user',
      content: `[New task]\n${task}`,
    });
  }

  /** Get all messages. */
  getMessages(): ChatMessage[] {
    return this.messages;
  }

  /** Get the message count. */
  length(): number {
    return this.messages.length;
  }

  /**
   * Trim messages to fit within the token budget.
   *
   * Strategy (enhanced with BrowserOS context overflow pattern):
   * 1. Remove image content blocks first (most expensive, ~800 tokens each)
   * 2. Remove accessibility tree sections from older messages
   * 3. Remove oldest non-system messages
   * 4. Keep: system prompt, task, last 3 state messages minimum
   */
  private trimToFit(): void {
    let tokens = this.estimateTokens();
    if (tokens <= this.maxInputTokens) return;

    // Phase 1: Strip images from older messages (keep last 2 images)
    let imageCount = 0;
    for (let i = this.messages.length - 1; i >= 0; i--) {
      const msg = this.messages[i];
      if (Array.isArray(msg.content)) {
        const hasImage = msg.content.some((b) => b.type === 'image_url');
        if (hasImage) imageCount++;
        if (imageCount > 2) {
          // Remove image blocks from this message
          msg.content = msg.content.filter((b) => b.type !== 'image_url');
          if (msg.content.length === 0) {
            msg.content = '[State message - image removed for context management]';
          }
          tokens = this.estimateTokens();
          if (tokens <= this.maxInputTokens) return;
        }
      }
    }

    // Phase 2: Remove oldest non-system, non-task messages (keep first 2 and last 6)
    const minKeepStart = 2; // system + task
    const minKeepEnd = 6; // last 3 state+response pairs
    while (
      tokens > this.maxInputTokens &&
      this.messages.length > minKeepStart + minKeepEnd
    ) {
      this.messages.splice(minKeepStart, 1);
      tokens = this.estimateTokens();
    }
  }

  /** Estimate total token count for all messages. */
  private estimateTokens(): number {
    let total = 0;
    for (const msg of this.messages) {
      if (typeof msg.content === 'string') {
        total += Math.ceil(msg.content.length / CHARS_PER_TOKEN);
      } else if (Array.isArray(msg.content)) {
        for (const block of msg.content) {
          if (block.type === 'text') {
            total += Math.ceil(block.text.length / CHARS_PER_TOKEN);
          } else if (block.type === 'image_url') {
            total += imageTokenCost(block.image_url?.url);
          }
        }
      }
    }
    return total;
  }
}
