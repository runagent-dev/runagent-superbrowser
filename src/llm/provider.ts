/**
 * LLM provider abstraction.
 *
 * Uses OpenAI SDK in compatibility mode — works with OpenAI,
 * Anthropic (via proxy), and any OpenAI-compatible endpoint.
 */

import OpenAI from 'openai';
import type { ChatMessage, ChatOptions, ChatResponse } from './types.js';

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

/** Check if a model is a reasoning/newer model that needs max_completion_tokens. */
function needsMaxCompletionTokens(model: string): boolean {
  const lower = model.toLowerCase();
  return REASONING_MODEL_PREFIXES.some((prefix) => lower.startsWith(prefix));
}

export class LLMProvider {
  private client: OpenAI;
  private model: string;
  private defaultTemperature: number;
  private defaultMaxTokens: number;

  constructor(config: LLMConfig) {
    this.model = config.model;
    this.defaultTemperature = config.defaultTemperature ?? 0.1;
    this.defaultMaxTokens = config.defaultMaxTokens ?? 4096;

    // Detect provider from model name and configure accordingly
    let baseURL = config.baseUrl;
    let apiKey = config.apiKey;

    if (!baseURL) {
      if (config.model.startsWith('claude')) {
        // Anthropic — use their OpenAI-compatible endpoint
        baseURL = 'https://api.anthropic.com/v1/';
      }
      // Default: OpenAI
    }

    this.client = new OpenAI({
      apiKey,
      baseURL,
    });
  }

  /** Send a chat completion request. */
  async chat(messages: ChatMessage[], options?: ChatOptions): Promise<ChatResponse> {
    const model = options?.model || this.model;
    const temperature = options?.temperature ?? this.defaultTemperature;
    const maxTokens = options?.maxTokens ?? this.defaultMaxTokens;

    // Convert messages to OpenAI format
    const openaiMessages = messages.map((m) => {
      if (typeof m.content === 'string') {
        return { role: m.role, content: m.content };
      }
      // Vision content blocks
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
      };
    }) as OpenAI.Chat.ChatCompletionMessageParam[];

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

      // JSON-mode output: supported by OpenAI (gpt-4+), Anthropic via
      // OpenAI-compatible endpoint, and most OSS endpoints. Providers that
      // don't recognize the field ignore it silently, so passing it is safe.
      if (options?.responseFormat === 'json_object') {
        params.response_format = { type: 'json_object' };
      }

      const response = await this.client.chat.completions.create(
        params as unknown as OpenAI.Chat.ChatCompletionCreateParamsNonStreaming,
      );

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
    } catch (error) {
      const msg = error instanceof Error ? error.message : String(error);
      throw new Error(`LLM call failed: ${msg}`);
    }
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

        // Don't retry on auth errors or bad requests
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
}
