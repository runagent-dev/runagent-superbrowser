/**
 * LLM types for chat completion.
 */

export interface ContentBlockText {
  type: 'text';
  text: string;
}

export interface ContentBlockImage {
  type: 'image_url';
  image_url: { url: string };
}

export type ContentBlock = ContentBlockText | ContentBlockImage;

export interface ChatMessage {
  role: 'system' | 'user' | 'assistant';
  content: string | ContentBlock[];
}

export interface ChatOptions {
  temperature?: number;
  maxTokens?: number;
  model?: string;
  /**
   * Force structured JSON output when supported by the provider.
   * 'json_object': generic JSON (OpenAI JSON mode).
   * Ignored by providers that don't support it (falls through to raw text).
   */
  responseFormat?: 'json_object';
}

export interface TokenUsage {
  promptTokens: number;
  completionTokens: number;
  totalTokens: number;
}

export interface ChatResponse {
  content: string;
  finishReason: string;
  usage: TokenUsage;
}
