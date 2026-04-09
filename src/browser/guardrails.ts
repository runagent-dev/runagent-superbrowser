/**
 * Security guardrails for content sanitization.
 *
 * Adapted from nanobrowser's guardrails service. Detects and neutralizes
 * prompt injection attempts, task override instructions, and sensitive
 * data patterns in web page content before it reaches the LLM.
 */

import crypto from 'crypto';

// --- Types ---

export enum ThreatType {
  TASK_OVERRIDE = 'task_override',
  PROMPT_INJECTION = 'prompt_injection',
  SENSITIVE_DATA = 'sensitive_data',
  DANGEROUS_ACTION = 'dangerous_action',
}

export interface SecurityPattern {
  pattern: RegExp;
  type: ThreatType;
  description: string;
  replacement?: string;
}

export interface SanitizationResult {
  sanitized: string;
  threats: ThreatType[];
  modified: boolean;
}

export interface ValidationResult {
  isValid: boolean;
  threats?: ThreatType[];
  message?: string;
}

// --- Patterns ---

const SECURITY_PATTERNS: SecurityPattern[] = [
  // Task override attempts
  {
    pattern: /\b(ignore|forget|disregard)[\s\-_]*(previous|all|above)[\s\-_]*(instructions?|tasks?|commands?)\b/gi,
    type: ThreatType.TASK_OVERRIDE,
    description: 'Attempt to override previous instructions',
    replacement: '[BLOCKED_OVERRIDE_ATTEMPT]',
  },
  {
    pattern: /\b(your?|the)[\s\-_]*new[\s\-_]*(task|instruction|goal|objective)[\s\-_]*(is|are|:)/gi,
    type: ThreatType.TASK_OVERRIDE,
    description: 'Attempt to inject new task',
    replacement: '[BLOCKED_TASK_INJECTION]',
  },
  {
    pattern: /\b(now|instead|actually)[\s\-_]+(you must|you should|you will)[\s\-_]+/gi,
    type: ThreatType.TASK_OVERRIDE,
    description: 'Attempt to redirect agent behavior',
    replacement: '[BLOCKED_REDIRECT]',
  },
  {
    pattern: /\bultimate[-_ ]+task\b/gi,
    type: ThreatType.TASK_OVERRIDE,
    description: 'Reference to ultimate task',
    replacement: '',
  },

  // Prompt injection — tags and system references
  {
    pattern: /\bsystem[\s\-_]*(prompt|message|instruction)/gi,
    type: ThreatType.PROMPT_INJECTION,
    description: 'Reference to system prompt',
    replacement: '[BLOCKED_SYSTEM_REFERENCE]',
  },
  {
    pattern: /\bnano[-_ ]+untrusted[-_ ]+content\b/gi,
    type: ThreatType.PROMPT_INJECTION,
    description: 'Attempt to fake untrusted content tags',
    replacement: '',
  },
  {
    pattern: /\bnano[-_ ]+user[-_ ]+request\b/gi,
    type: ThreatType.PROMPT_INJECTION,
    description: 'Attempt to fake user request tags',
    replacement: '',
  },
  {
    pattern: /\buntrusted[-_]+content\b/gi,
    type: ThreatType.PROMPT_INJECTION,
    description: 'Reference to untrusted content',
    replacement: '',
  },

  // Suspicious XML/HTML tags
  {
    pattern: /<\/?[\s]*(?:instruction|command|system|task|override|ignore|plan|execute|request)[\s]*>/gi,
    type: ThreatType.PROMPT_INJECTION,
    description: 'Suspicious XML/HTML tags',
    replacement: '',
  },
  {
    pattern: /\]\]>|<!--[\s\S]*?-->|<!\[CDATA\[[\s\S]*?\]\]>/gi,
    type: ThreatType.PROMPT_INJECTION,
    description: 'XML injection attempt',
    replacement: '',
  },

  // Sensitive data patterns
  {
    pattern: /\b\d{3}-\d{2}-\d{4}\b/g,
    type: ThreatType.SENSITIVE_DATA,
    description: 'Potential SSN detected',
    replacement: '[REDACTED_SSN]',
  },
  {
    pattern: /\b(?:\d{4}[\s-]?){3}\d{4}\b/g,
    type: ThreatType.SENSITIVE_DATA,
    description: 'Potential credit card number',
    replacement: '[REDACTED_CC]',
  },
];

const STRICT_PATTERNS: SecurityPattern[] = [
  {
    pattern: /\b(password|pwd|passwd|api[\s_-]*key|secret|token)\s*[:=]\s*["']?[\w-]+["']?/gi,
    type: ThreatType.SENSITIVE_DATA,
    description: 'Credential detected',
    replacement: '[REDACTED_CREDENTIAL]',
  },
  {
    pattern: /\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b/g,
    type: ThreatType.SENSITIVE_DATA,
    description: 'Email address detected',
    replacement: '[EMAIL]',
  },
  {
    pattern: /\b(bypass|circumvent|avoid|skip)[\s\-_]*(security|safety|filter|check)/gi,
    type: ThreatType.PROMPT_INJECTION,
    description: 'Security bypass attempt',
    replacement: '[BLOCKED_BYPASS]',
  },
];

function getPatterns(strict: boolean): SecurityPattern[] {
  return strict ? [...SECURITY_PATTERNS, ...STRICT_PATTERNS] : SECURITY_PATTERNS;
}

// --- Sanitizer ---

/** Remove empty XML/HTML tags left after sanitization. */
export function cleanEmptyTags(content: string): string {
  let result = content.replace(/<(\w+)[^>]*>\s*<\/\1>/g, '');
  result = result.replace(/<\s*\/?\s*>/g, '');
  return result;
}

/** Sanitize untrusted content by removing dangerous patterns. */
export function sanitizeContent(content: string | undefined, strict = false): SanitizationResult {
  if (!content || content.trim() === '') {
    return { sanitized: '', threats: [], modified: false };
  }

  // Normalize unicode and remove zero-width chars
  let sanitized = content.normalize('NFKC').replace(/[\u200B-\u200D\uFEFF]/g, '');
  const detectedThreats = new Set<ThreatType>();
  let wasModified = false;

  const patterns = getPatterns(strict);

  for (const sp of patterns) {
    try {
      const regex = new RegExp(sp.pattern.source, sp.pattern.flags);
      if (regex.test(sanitized)) {
        detectedThreats.add(sp.type);
        const replacementRegex = new RegExp(sp.pattern.source, sp.pattern.flags);
        const before = sanitized.length;
        sanitized = sanitized.replace(replacementRegex, sp.replacement || '');
        if (sanitized.length !== before) wasModified = true;
      }
    } catch {
      // Continue with other patterns
    }
  }

  if (wasModified) {
    sanitized = sanitized
      .replace(/[^\S\r\n]+/g, ' ')
      .replace(/\n{3,}/g, '\n\n')
      .trim();
    sanitized = cleanEmptyTags(sanitized);
  }

  return {
    sanitized,
    threats: Array.from(detectedThreats),
    modified: wasModified,
  };
}

/** Detect threats without modifying content. */
export function detectThreats(content: string, strict = false): ThreatType[] {
  if (!content || content.trim() === '') return [];

  const detectedThreats = new Set<ThreatType>();
  const patterns = getPatterns(strict);

  for (const sp of patterns) {
    try {
      const regex = new RegExp(sp.pattern.source, sp.pattern.flags);
      if (regex.test(content)) {
        detectedThreats.add(sp.type);
      }
    } catch {
      // Continue
    }
  }

  return Array.from(detectedThreats);
}

// --- Main Service ---

export class SecurityGuardrails {
  private strictMode: boolean;
  private enabled: boolean;

  constructor(config?: { strictMode?: boolean; enabled?: boolean }) {
    this.strictMode = config?.strictMode ?? false;
    this.enabled = config?.enabled ?? true;
  }

  sanitize(content: string | undefined, options?: { strict?: boolean }): SanitizationResult {
    if (!this.enabled) return { sanitized: content || '', threats: [], modified: false };
    return sanitizeContent(content, options?.strict ?? this.strictMode);
  }

  detectThreats(content: string, options?: { strict?: boolean }): ThreatType[] {
    if (!this.enabled) return [];
    return detectThreats(content, options?.strict ?? this.strictMode);
  }

  validate(content: string, options?: { strict?: boolean }): ValidationResult {
    if (!this.enabled) return { isValid: true };

    const threats = this.detectThreats(content, options);
    if (threats.length === 0) return { isValid: true };

    const effectiveStrict = options?.strict ?? this.strictMode;
    if (effectiveStrict) {
      return {
        isValid: false,
        threats,
        message: `Content contains ${threats.length} security threat(s)`,
      };
    }

    const criticalThreats = threats.filter(
      (t) => t === ThreatType.TASK_OVERRIDE || t === ThreatType.DANGEROUS_ACTION,
    );

    return {
      isValid: criticalThreats.length === 0,
      threats,
      message:
        criticalThreats.length > 0
          ? `Content contains ${criticalThreats.length} critical threat(s)`
          : `Content contains ${threats.length} non-critical threat(s)`,
    };
  }

  setEnabled(enabled: boolean): void {
    this.enabled = enabled;
  }

  setStrictMode(strict: boolean): void {
    this.strictMode = strict;
  }
}

/** Default singleton instance. */
export const guardrails = new SecurityGuardrails();
