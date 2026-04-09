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
      const response = await this.client.chat.completions.create({
        model,
        messages: openaiMessages,
        temperature,
        max_tokens: maxTokens,
      });

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
