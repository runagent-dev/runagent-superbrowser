/**
 * Action registry with Zod schema validation.
 *
 * Pattern from nanobrowser (builder.ts).
 */

import { z } from 'zod';
import type { PageWrapper } from '../../browser/page.js';
import type { DOMState } from '../../browser/dom.js';
import type { ActionResult } from '../types.js';

export interface ActionSchema {
  name: string;
  description: string;
  schema: z.ZodTypeAny;
}

export class Action {
  constructor(
    private config: {
      name: string;
      description: string;
      schema: z.ZodTypeAny;
      hasIndex?: boolean;
      handler: (input: unknown, page: PageWrapper, state: DOMState) => Promise<ActionResult>;
    },
  ) {}

  get name(): string {
    return this.config.name;
  }

  get description(): string {
    return this.config.description;
  }

  get hasIndex(): boolean {
    return this.config.hasIndex || false;
  }

  /** Validate input and execute the action. */
  async execute(input: unknown, page: PageWrapper, state: DOMState): Promise<ActionResult> {
    // Validate input against schema
    const parsed = this.config.schema.safeParse(input);
    if (!parsed.success) {
      return {
        success: false,
        error: `Invalid input for ${this.name}: ${parsed.error.message}`,
      };
    }

    try {
      return await this.config.handler(parsed.data, page, state);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      return { success: false, error: `Action ${this.name} failed: ${msg}` };
    }
  }

  /** Get formatted prompt description for the system prompt. */
  getPrompt(): string {
    const shape = this.config.schema instanceof z.ZodObject
      ? (this.config.schema as z.ZodObject<z.ZodRawShape>).shape
      : {};

    const params = Object.entries(shape).map(([key, val]) => {
      const zodVal = val as z.ZodTypeAny;
      const desc = zodVal.description || '';
      const optional = zodVal.isOptional();
      return `'${key}': ${desc}${optional ? ' (optional)' : ''}`;
    });

    const paramStr = params.length > 0
      ? `{${params.join(', ')}}`
      : '{}';

    return `${this.description}:\n  {${this.name}: ${paramStr}}`;
  }
}

export class ActionRegistry {
  private actions = new Map<string, Action>();

  register(action: Action): void {
    this.actions.set(action.name, action);
  }

  get(name: string): Action | undefined {
    return this.actions.get(name);
  }

  /** Execute a named action with validation. */
  async execute(
    name: string,
    params: unknown,
    page: PageWrapper,
    state: DOMState,
  ): Promise<ActionResult> {
    const action = this.actions.get(name);
    if (!action) {
      return { success: false, error: `Unknown action: ${name}` };
    }
    return action.execute(params, page, state);
  }

  /** Get formatted prompt listing all available actions. */
  getPrompt(): string {
    return Array.from(this.actions.values())
      .map((a) => a.getPrompt())
      .join('\n\n');
  }

  /** Get all action names. */
  names(): string[] {
    return Array.from(this.actions.keys());
  }
}
