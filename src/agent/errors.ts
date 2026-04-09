/**
 * Custom error hierarchy for the agent system.
 *
 * Adapted from nanobrowser's error classes. Provides typed errors
 * for better API responses and debugging.
 */

/** LLM authentication failure (401). */
export class AuthError extends Error {
  constructor(message: string, public readonly cause?: unknown) {
    super(message);
    this.name = 'AuthError';
    if (Error.captureStackTrace) Error.captureStackTrace(this, AuthError);
  }
}

/** LLM forbidden (403). */
export class ForbiddenError extends Error {
  constructor(message: string, public readonly cause?: unknown) {
    super(message);
    this.name = 'ForbiddenError';
    if (Error.captureStackTrace) Error.captureStackTrace(this, ForbiddenError);
  }
}

/** LLM bad request (400). */
export class BadRequestError extends Error {
  constructor(message: string, public readonly cause?: unknown) {
    super(message);
    this.name = 'BadRequestError';
    if (Error.captureStackTrace) Error.captureStackTrace(this, BadRequestError);
  }
}

/** URL blocked by firewall or SSRF protection. */
export class UrlBlockedError extends Error {
  constructor(message: string, public readonly url?: string) {
    super(message);
    this.name = 'UrlBlockedError';
    if (Error.captureStackTrace) Error.captureStackTrace(this, UrlBlockedError);
  }
}

/** Puppeteer script execution timed out. */
export class ScriptTimeoutError extends Error {
  constructor(message: string, public readonly timeoutMs?: number) {
    super(message);
    this.name = 'ScriptTimeoutError';
    if (Error.captureStackTrace) Error.captureStackTrace(this, ScriptTimeoutError);
  }
}

/** Maximum execution steps reached. */
export class MaxStepsError extends Error {
  constructor(message: string, public readonly steps?: number) {
    super(message);
    this.name = 'MaxStepsError';
    if (Error.captureStackTrace) Error.captureStackTrace(this, MaxStepsError);
  }
}

/** Maximum consecutive failures reached. */
export class MaxFailuresError extends Error {
  constructor(message: string, public readonly failures?: number) {
    super(message);
    this.name = 'MaxFailuresError';
    if (Error.captureStackTrace) Error.captureStackTrace(this, MaxFailuresError);
  }
}

/** LLM response could not be parsed. */
export class ResponseParseError extends Error {
  constructor(message: string, public readonly cause?: unknown) {
    super(message);
    this.name = 'ResponseParseError';
    if (Error.captureStackTrace) Error.captureStackTrace(this, ResponseParseError);
  }
}

/** Task was cancelled by user or system. */
export class TaskCancelledError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'TaskCancelledError';
    if (Error.captureStackTrace) Error.captureStackTrace(this, TaskCancelledError);
  }
}

// --- Detection functions ---

export function isAuthError(error: unknown): boolean {
  if (error instanceof AuthError) return true;
  if (!(error instanceof Error)) return false;
  const msg = error.message.toLowerCase();
  return (
    error.constructor?.name === 'AuthenticationError' ||
    msg.includes('authentication') ||
    msg.includes(' 401') ||
    msg.includes('api key')
  );
}

export function isForbiddenError(error: unknown): boolean {
  if (error instanceof ForbiddenError) return true;
  if (!(error instanceof Error)) return false;
  return error.message.includes(' 403') && error.message.toLowerCase().includes('forbidden');
}

export function isBadRequestError(error: unknown): boolean {
  if (error instanceof BadRequestError) return true;
  if (!(error instanceof Error)) return false;
  const msg = error.message.toLowerCase();
  return (
    error.constructor?.name === 'BadRequestError' ||
    msg.includes(' 400') ||
    msg.includes('badrequest') ||
    msg.includes('invalid parameter')
  );
}

export function isUrlBlockedError(error: unknown): boolean {
  return error instanceof UrlBlockedError;
}

export function isScriptTimeoutError(error: unknown): boolean {
  return error instanceof ScriptTimeoutError;
}

export function isAbortedError(error: unknown): boolean {
  if (!(error instanceof Error)) return false;
  return error.name === 'AbortError' || error.message.includes('Aborted');
}

/**
 * Map an error to an HTTP status code for API responses.
 */
export function errorToStatusCode(error: unknown): number {
  if (isAuthError(error)) return 401;
  if (isForbiddenError(error)) return 403;
  if (isBadRequestError(error)) return 400;
  if (isUrlBlockedError(error)) return 403;
  if (isScriptTimeoutError(error)) return 408;
  if (error instanceof TaskCancelledError) return 499;
  return 500;
}
