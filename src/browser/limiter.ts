/**
 * Concurrency limiter with queue management from browserless.
 *
 * Controls how many browser sessions run simultaneously,
 * queues excess requests, and enforces timeouts.
 */

import { EventEmitter } from 'events';

interface QueuedJob {
  execute: () => Promise<unknown>;
  timeout: number;
  start: number;
  timer?: ReturnType<typeof setTimeout>;
  resolve: (value: unknown) => void;
  reject: (reason: unknown) => void;
}

export interface LimiterConfig {
  maxConcurrent: number;
  maxQueued: number;
  defaultTimeout: number; // ms
}

const DEFAULT_LIMITER_CONFIG: LimiterConfig = {
  maxConcurrent: 10,
  maxQueued: 10,
  defaultTimeout: 60000,
};

export class Limiter extends EventEmitter {
  private executing = 0;
  private queue: QueuedJob[] = [];
  private config: LimiterConfig;

  // Metrics
  private metrics = {
    successful: 0,
    failed: 0,
    timedout: 0,
    queued: 0,
    rejected: 0,
  };

  constructor(config: Partial<LimiterConfig> = {}) {
    super();
    this.config = { ...DEFAULT_LIMITER_CONFIG, ...config };
  }

  /** Check if there's capacity to accept a new request. */
  get hasCapacity(): boolean {
    return this.executing + this.queue.length < this.config.maxConcurrent + this.config.maxQueued;
  }

  /** Check if a new request would be queued. */
  get willQueue(): boolean {
    return this.executing >= this.config.maxConcurrent;
  }

  /** Number of currently executing jobs. */
  get runningCount(): number {
    return this.executing;
  }

  /** Number of queued jobs. */
  get queuedCount(): number {
    return this.queue.length;
  }

  /** Get metrics snapshot. */
  getMetrics() {
    return { ...this.metrics };
  }

  /**
   * Submit a job to the limiter.
   * Returns a promise that resolves when the job completes.
   * Throws TooManyRequests if no capacity.
   */
  async submit<T>(
    fn: () => Promise<T>,
    timeout?: number,
  ): Promise<T> {
    if (!this.hasCapacity) {
      this.metrics.rejected++;
      this.emit('rejected');
      throw new Error('Too many requests — queue is full');
    }

    const jobTimeout = timeout || this.config.defaultTimeout;

    return new Promise<T>((resolve, reject) => {
      const job: QueuedJob = {
        execute: fn as () => Promise<unknown>,
        timeout: jobTimeout,
        start: Date.now(),
        resolve: resolve as (value: unknown) => void,
        reject,
      };

      if (this.willQueue) {
        this.metrics.queued++;
        this.queue.push(job);
        this.emit('queued', this.queue.length);
      } else {
        this.run(job);
      }
    });
  }

  private async run(job: QueuedJob): Promise<void> {
    this.executing++;

    // Set timeout timer
    job.timer = setTimeout(() => {
      this.metrics.timedout++;
      this.executing--;
      job.reject(new Error(`Request timed out after ${job.timeout}ms`));
      this.emit('timeout');
      this.tryNext();
    }, job.timeout);

    try {
      const result = await job.execute();
      clearTimeout(job.timer);
      this.metrics.successful++;
      this.executing--;
      job.resolve(result);
      this.emit('success');
    } catch (err) {
      clearTimeout(job.timer!);
      this.metrics.failed++;
      this.executing--;
      job.reject(err);
      this.emit('error', err);
    }

    this.tryNext();
  }

  private tryNext(): void {
    if (this.queue.length > 0 && this.executing < this.config.maxConcurrent) {
      const next = this.queue.shift()!;
      this.run(next);
    }

    if (this.executing === 0 && this.queue.length === 0) {
      this.emit('end');
    }
  }
}
