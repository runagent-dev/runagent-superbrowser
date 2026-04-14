/**
 * LLM provider abstraction.
 *
 * Uses OpenAI SDK in compatibility mode — works with OpenAI,
 * Anthropic (via proxy), Gemini's OpenAI-compat endpoint, and OpenRouter.
 *
 * Per-provider request shaping lives in `shapeRequest()`. This is the
 * layer OpenRouter gives for free on their side — we do the same work
 * here so swapping API keys between providers stops changing error
 * behavior. The big one: Gemini's OpenAI-compat layer 400s on
 * `response_format: json_object`, which the caller can't know about.
 */
import OpenAI from 'openai';
import type { ChatMessage, ChatOptions, ChatResponse } from './types.js';
import {
  GEMINI_MAX_BYTES,
  GEMINI_MAX_IMAGES_PER_REQUEST,
  isTokenOverflow400,
  sanitizeBase64Image,
} from '../browser/image-safety.js';

export interface LLMConfig {
  apiKey: string;
  baseUrl?: string;
  model: string;
  defaultTemperature?: number;
  defaultMaxTokens?: number;
}

/**
 * Models that require max_completion_tokens instead of max_tokens,
 * and don't support temperature.
 */
const REASONING_MODEL_PREFIXES = [
  'o1', 'o3', 'o4',
  'gpt-5', 'gpt-6',
];

type Provider = 'openai' | 'gemini' | 'anthropic' | 'openrouter';

/** Check if a model is a reasoning/newer model that needs max_completion_tokens. */
function needsMaxCompletionTokens(model: string): boolean {
  const lower = model.toLowerCase();
  return REASONING_MODEL_PREFIXES.some((prefix) => lower.startsWith(prefix));
}

function detectProvider(model: string, baseURL?: string): Provider {
  const m = model.toLowerCase();
  const b = (baseURL ?? '').toLowerCase();
  if (b.includes('openrouter.ai')) return 'openrouter';
  if (b.includes('generativelanguage.googleapis.com') || m.startsWith('gemini')) return 'gemini';
  if (m.startsWith('claude') || b.includes('api.anthropic.com')) return 'anthropic';
  return 'openai';
}

type OpenAIMessage = OpenAI.Chat.ChatCompletionMessageParam;
type OpenAIRequest = OpenAI.Chat.ChatCompletionCreateParamsNonStreaming;

export class LLMProvider {
  private client: OpenAI;
  private model: string;
  private defaultTemperature: number;
  private defaultMaxTokens: number;
  private provider: Provider;
  private shaperLogged = false;

  constructor(config: LLMConfig) {
    this.model = config.model;
    this.defaultTemperature = config.defaultTemperature ?? 0.1;
    this.defaultMaxTokens = config.defaultMaxTokens ?? 4096;

    // Detect provider from model name and configure accordingly
    let baseURL = config.baseUrl;
    const apiKey = config.apiKey;

    if (!baseURL) {
      if (config.model.startsWith('claude')) {
        // Anthropic — use their OpenAI-compatible endpoint
        baseURL = 'https://api.anthropic.com/v1/';
      } else if (config.model.toLowerCase().startsWith('gemini')) {
        // Gemini's OpenAI-compatible endpoint
        baseURL = 'https://generativelanguage.googleapis.com/v1beta/openai/';
      }
      // Default: OpenAI
    }

    this.provider = detectProvider(config.model, baseURL);
    this.client = new OpenAI({ apiKey, baseURL });
  }

  /**
   * Apply provider-specific request shaping. Mutates nothing the caller
   * holds — returns a new params object with a normalized messages array.
   *
   * Gemini handling is the important path. OpenRouter already does this
   * upstream (that's why the user's Gemini-through-OpenRouter requests
   * succeed but direct Gemini 400s).
   */
  private async shapeRequest(
    params: Record<string, unknown>,
    messages: OpenAIMessage[],
    wantsJson: boolean,
  ): Promise<{ params: Record<string, unknown>; messages: OpenAIMessage[] }> {
    if (this.provider !== 'gemini') {
      return { params, messages };
    }

    const shaped = { ...params };
    let shapedMessages: OpenAIMessage[] = messages;
    const notes: string[] = [];

    // 1. Gemini's OpenAI-compat layer 400s on response_format. Drop it
    // and steer the model to JSON via prompt instead.
    if (shaped.response_format) {
      delete shaped.response_format;
      notes.push('dropped response_format');
      if (wantsJson) {
        shapedMessages = appendJsonNudgeToLastUser(shapedMessages);
        notes.push('appended JSON nudge to last user msg');
      }
    }

    // 2-4. Cap image count and re-sanitize each image at Gemini's tighter
    // byte cap. Strip whitespace from data URIs while we're in there.
    const { messages: cappedMessages, droppedImages, resanitizedCount } =
      await enforceGeminiImageCaps(shapedMessages);
    shapedMessages = cappedMessages;
    if (droppedImages > 0) notes.push(`dropped ${droppedImages} older image(s)`);
    if (resanitizedCount > 0) notes.push(`re-sanitized ${resanitizedCount} image(s) @ ${GEMINI_MAX_BYTES}B cap`);

    shaped.messages = shapedMessages;

    if (!this.shaperLogged && notes.length > 0) {
      console.log(`[llm-shape] Gemini: ${notes.join('; ')}`);
      this.shaperLogged = true;
    }

    return { params: shaped, messages: shapedMessages };
  }

  /** Send a chat completion request. */
  async chat(messages: ChatMessage[], options?: ChatOptions): Promise<ChatResponse> {
    const model = options?.model || this.model;
    const temperature = options?.temperature ?? this.defaultTemperature;
    const maxTokens = options?.maxTokens ?? this.defaultMaxTokens;

    // Convert messages to OpenAI format
    const openaiMessages: OpenAIMessage[] = messages.map((m) => {
      if (typeof m.content === 'string') {
        return { role: m.role, content: m.content } as OpenAIMessage;
      }
      return {
        role: m.role,
        content: m.content.map((block) => {
          if (block.type === 'text') {
            return { type: 'text' as const, text: block.text || '' };
          }
          return {
            type: 'image_url' as const,
            image_url: { url: block.image_url?.url || '' },
          };
        }),
      } as OpenAIMessage;
    });

    try {
      // Build params — handle max_tokens vs max_completion_tokens for newer models
      const params: Record<string, unknown> = {
        model,
        messages: openaiMessages,
      };

      if (needsMaxCompletionTokens(model)) {
        // Reasoning models: use max_completion_tokens, omit temperature
        params.max_completion_tokens = maxTokens;
      } else {
        // Standard models: use max_tokens + temperature
        params.max_tokens = maxTokens;
        params.temperature = temperature;
      }

      const wantsJson = options?.responseFormat === 'json_object';
      if (wantsJson) {
        params.response_format = { type: 'json_object' };
      }

      // Provider-specific shaping (Gemini strips response_format, caps images, etc.)
      const shaped = await this.shapeRequest(params, openaiMessages, wantsJson);

      return await this.callOpenAI(shaped.params as unknown as OpenAIRequest);
    } catch (error) {
      const msg = error instanceof Error ? error.message : String(error);
      throw new Error(`LLM call failed: ${msg}`);
    }
  }

  private async callOpenAI(params: OpenAIRequest): Promise<ChatResponse> {
    const response = await this.client.chat.completions.create(params);
    const choice = response.choices[0];
    return {
      content: choice?.message?.content || '',
      finishReason: choice?.finish_reason || 'stop',
      usage: {
        promptTokens: response.usage?.prompt_tokens || 0,
        completionTokens: response.usage?.completion_tokens || 0,
        totalTokens: response.usage?.total_tokens || 0,
      },
    };
  }

  /** Chat with retry on transient errors. */
  async chatWithRetry(
    messages: ChatMessage[],
    options?: ChatOptions,
    maxRetries: number = 3,
  ): Promise<ChatResponse> {
    let lastError: Error | null = null;

    for (let attempt = 0; attempt < maxRetries; attempt++) {
      try {
        return await this.chat(messages, options);
      } catch (error) {
        lastError = error instanceof Error ? error : new Error(String(error));

        // 400 = bad request. Two subtypes we care about:
        //   (a) token overflow — retry once after aggressively trimming.
        //       Our trim mirrors nanobot's strip-images fallback but fires
        //       earlier, so nanobot's retry-without-images almost never
        //       runs in practice.
        //   (b) everything else (auth, malformed) — propagate; retrying
        //       won't help and will burn the rate limit.
        if (lastError.message.includes('400') && isTokenOverflow400(lastError)) {
          if (attempt === 0) {
            const trimmed = trimForOverflow(messages);
            if (trimmed) {
              console.log('[llm-retry] token overflow — retrying with trimmed context');
              try {
                return await this.chat(trimmed, options);
              } catch (retryErr) {
                lastError = retryErr instanceof Error ? retryErr : new Error(String(retryErr));
              }
            }
          }
          throw lastError;
        }
        if (lastError.message.includes('401') || lastError.message.includes('403') || lastError.message.includes('400')) {
          throw lastError;
        }

        // Exponential backoff
        const delay = Math.min(1000 * Math.pow(2, attempt), 10000);
        await new Promise((r) => setTimeout(r, delay));
      }
    }

    throw lastError || new Error('Max retries exceeded');
  }

  getModel(): string {
    return this.model;
  }

  getProvider(): Provider {
    return this.provider;
  }
}

/**
 * Append a JSON-mode nudge to the last user message's text content.
 * Used by Gemini shaper since we strip response_format there.
 */
function appendJsonNudgeToLastUser(messages: OpenAIMessage[]): OpenAIMessage[] {
  const nudge = '\n\nRespond with a single JSON object only. No prose, no code fences.';
  const out = messages.slice();
  for (let i = out.length - 1; i >= 0; i--) {
    const m = out[i];
    if (m.role !== 'user') continue;
    if (typeof m.content === 'string') {
      out[i] = { ...m, content: m.content + nudge };
    } else if (Array.isArray(m.content)) {
      const parts = m.content.slice();
      // Append to the last text block, or add one if there isn't any.
      let attached = false;
      for (let j = parts.length - 1; j >= 0; j--) {
        const p = parts[j];
        if (p && typeof p === 'object' && 'type' in p && p.type === 'text') {
          parts[j] = { ...p, text: (p.text ?? '') + nudge };
          attached = true;
          break;
        }
      }
      if (!attached) parts.push({ type: 'text', text: nudge.trimStart() });
      out[i] = { ...m, content: parts };
    }
    break;
  }
  return out;
}

/**
 * Enforce Gemini's per-request image caps: at most
 * GEMINI_MAX_IMAGES_PER_REQUEST image blocks across all messages (we keep
 * the newest), and each one re-sanitized at GEMINI_MAX_BYTES.
 */
async function enforceGeminiImageCaps(
  messages: OpenAIMessage[],
): Promise<{ messages: OpenAIMessage[]; droppedImages: number; resanitizedCount: number }> {
  // Find all image blocks indexed by (messageIndex, partIndex), newest last.
  type ImgRef = { mi: number; pi: number };
  const refs: ImgRef[] = [];
  for (let mi = 0; mi < messages.length; mi++) {
    const c = messages[mi].content;
    if (!Array.isArray(c)) continue;
    for (let pi = 0; pi < c.length; pi++) {
      const p = c[pi] as { type?: string };
      if (p?.type === 'image_url') refs.push({ mi, pi });
    }
  }

  let droppedImages = 0;
  let resanitizedCount = 0;

  // Drop oldest images beyond the cap.
  const toDrop = new Set<string>();
  if (refs.length > GEMINI_MAX_IMAGES_PER_REQUEST) {
    const dropCount = refs.length - GEMINI_MAX_IMAGES_PER_REQUEST;
    for (let i = 0; i < dropCount; i++) {
      toDrop.add(`${refs[i].mi}:${refs[i].pi}`);
      droppedImages++;
    }
  }

  // Build new messages with dropped images filtered and surviving images
  // re-sanitized at Gemini's tighter cap.
  const out: OpenAIMessage[] = [];
  for (let mi = 0; mi < messages.length; mi++) {
    const msg = messages[mi];
    const c = msg.content;
    if (!Array.isArray(c)) {
      out.push(msg);
      continue;
    }
    const newParts: unknown[] = [];
    for (let pi = 0; pi < c.length; pi++) {
      const part = c[pi] as { type?: string; image_url?: { url?: string }; text?: string };
      if (part?.type === 'image_url') {
        if (toDrop.has(`${mi}:${pi}`)) continue;
        const url = part.image_url?.url ?? '';
        // Only re-sanitize inline data URIs; external URLs pass through.
        if (url.startsWith('data:')) {
          try {
            const san = await sanitizeBase64Image(url, { maxBytes: GEMINI_MAX_BYTES });
            newParts.push({
              type: 'image_url',
              image_url: { url: `data:${san.mime};base64,${san.b64}` },
            });
            resanitizedCount++;
            continue;
          } catch {
            // Fall through to the original block if sanitation fails.
          }
        }
      }
      newParts.push(part);
    }
    out.push({ ...msg, content: newParts as typeof c } as OpenAIMessage);
  }

  return { messages: out, droppedImages, resanitizedCount };
}

/**
 * Aggressive trim for the token-overflow retry path. Drops all image
 * blocks and keeps only the most recent ~N text messages. Returns null
 * if there's nothing useful to trim (can't help).
 */
function trimForOverflow(messages: ChatMessage[]): ChatMessage[] | null {
  if (messages.length === 0) return null;

  // Keep system + last 4 messages, strip images from survivors.
  const keepTail = 4;
  const head: ChatMessage[] = [];
  for (const m of messages) {
    if (m.role === 'system') head.push(m);
    else break;
  }
  const tail = messages.slice(Math.max(messages.length - keepTail, head.length));

  const strip = (m: ChatMessage): ChatMessage => {
    if (typeof m.content === 'string') return m;
    const textOnly = m.content
      .filter((b) => b.type === 'text')
      .map((b) => b.text ?? '')
      .join(' ')
      .trim();
    return { ...m, content: textOnly || '[image content removed due to context overflow]' };
  };

  const trimmed = [...head, ...tail.map(strip)];
  if (trimmed.length === messages.length && trimmed.every((m, i) => m === messages[i])) {
    return null;
  }
  return trimmed;
}
