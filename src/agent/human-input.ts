/**
 * Human-in-the-loop system.
 *
 * When the agent needs user input (credentials, captcha, confirmation,
 * card details, OTP, etc.), it pauses execution, emits a request,
 * and waits for the response before continuing.
 *
 * Flow:
 *   1. Agent action calls requestHumanInput("Need login credentials")
 *   2. Executor pauses, emits HUMAN_INPUT_NEEDED event
 *   3. External system (nanobot, HTTP client, UI) sees the request
 *   4. User provides input via provideHumanInput()
 *   5. Executor resumes with the user's response
 */

export type HumanInputType =
  | 'credentials'    // username/password
  | 'captcha'        // solve a captcha manually
  | 'confirmation'   // yes/no before purchase/submit
  | 'otp'            // one-time password / 2FA code
  | 'card'           // payment card details
  | 'text'           // free-form text input
  | 'choice';        // pick from options

export interface HumanInputRequest {
  id: string;
  type: HumanInputType;
  message: string;
  /** Screenshot of current page (base64 JPEG) for context. */
  screenshot?: string;
  /** Options for 'choice' type. */
  options?: string[];
  /** Which fields are needed for 'credentials' / 'card' type. */
  fields?: string[];
  timestamp: number;
}

export interface HumanInputResponse {
  id: string;
  /** The user's input — shape depends on type. */
  data: Record<string, string>;
  /** User chose to skip/cancel this request. */
  cancelled?: boolean;
}

/**
 * Manages human-in-the-loop input requests.
 * The executor creates one of these per task.
 */
export class HumanInputManager {
  private pendingRequest: HumanInputRequest | null = null;
  private responseResolver: ((response: HumanInputResponse) => void) | null = null;
  private requestCounter = 0;

  /**
   * Request input from the user. Blocks until response is provided.
   *
   * @param type What kind of input is needed
   * @param message Human-readable description of what's needed
   * @param screenshot Current page screenshot for context
   * @param options For 'choice' type — list of options
   * @param fields For 'credentials'/'card' — field names needed
   * @returns The user's response, or null if cancelled/timed out
   */
  async requestInput(
    type: HumanInputType,
    message: string,
    options?: {
      screenshot?: string;
      options?: string[];
      fields?: string[];
      timeout?: number;
    },
  ): Promise<HumanInputResponse | null> {
    const id = `input-${++this.requestCounter}`;
    const timeout = options?.timeout || 5 * 60 * 1000; // 5 min default

    this.pendingRequest = {
      id,
      type,
      message,
      screenshot: options?.screenshot,
      options: options?.options,
      fields: options?.fields,
      timestamp: Date.now(),
    };

    // Wait for response
    return new Promise<HumanInputResponse | null>((resolve) => {
      this.responseResolver = resolve;

      // Timeout
      setTimeout(() => {
        if (this.pendingRequest?.id === id) {
          this.pendingRequest = null;
          this.responseResolver = null;
          resolve(null);
        }
      }, timeout);
    });
  }

  /**
   * Provide a response to the pending input request.
   * Called by HTTP endpoint or nanobot tool.
   */
  provideInput(response: HumanInputResponse): boolean {
    if (!this.pendingRequest || this.pendingRequest.id !== response.id) {
      return false;
    }

    this.pendingRequest = null;
    if (this.responseResolver) {
      this.responseResolver(response);
      this.responseResolver = null;
    }
    return true;
  }

  /** Get the current pending request (if any). */
  getPendingRequest(): HumanInputRequest | null {
    return this.pendingRequest;
  }

  /** Check if there's a pending request. */
  get hasPending(): boolean {
    return this.pendingRequest !== null;
  }

  /** Cancel the pending request. */
  cancel(): void {
    if (this.responseResolver) {
      this.responseResolver({
        id: this.pendingRequest?.id || '',
        data: {},
        cancelled: true,
      });
      this.responseResolver = null;
    }
    this.pendingRequest = null;
  }
}
