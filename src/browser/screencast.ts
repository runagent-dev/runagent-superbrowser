/**
 * CDP screencast manager with reference-counted start/stop.
 *
 * Wraps Page.startScreencast / screencastFrame / screencastFrameAck /
 * stopScreencast. Only runs the screencast when at least one viewer is
 * connected (zero CPU overhead otherwise).
 *
 * Backpressure: CDP won't send the next frame until the previous one is
 * ACKed. We ACK immediately after broadcasting, so throughput is
 * naturally limited to broadcast_time + one ACK round-trip.
 */

import type { CDPSession } from 'puppeteer-core';
import type { Protocol } from 'devtools-protocol';

export interface ScreencastOptions {
  format?: 'jpeg' | 'png';
  quality?: number;
  maxWidth?: number;
  maxHeight?: number;
  everyNthFrame?: number;
}

export class ScreencastManager {
  private viewers = new Set<string>();
  private running = false;
  private client: CDPSession | null = null;
  private frameHandler: ((params: Protocol.Page.ScreencastFrameEvent) => void) | null = null;

  constructor(
    private getClient: () => Promise<CDPSession>,
    private onFrame: (data: string, timestamp: number) => void,
  ) {}

  /** Add a viewer. Starts screencast on 0 → 1 transition. */
  async addViewer(id: string): Promise<void> {
    this.viewers.add(id);
    if (this.viewers.size === 1 && !this.running) {
      await this.start();
    }
  }

  /** Remove a viewer. Stops screencast on 1 → 0 transition. */
  async removeViewer(id: string): Promise<void> {
    this.viewers.delete(id);
    if (this.viewers.size === 0 && this.running) {
      await this.stop();
    }
  }

  get viewerCount(): number {
    return this.viewers.size;
  }

  get isRunning(): boolean {
    return this.running;
  }

  private async start(): Promise<void> {
    try {
      this.client = await this.getClient();

      const quality = parseInt(process.env.SUPERBROWSER_SCREENCAST_QUALITY || '60', 10);
      const everyNthFrame = parseInt(process.env.SUPERBROWSER_SCREENCAST_EVERY_NTH || '2', 10);

      this.frameHandler = (params: Protocol.Page.ScreencastFrameEvent) => {
        const data = params.data;
        const metadata = params.metadata;
        const sessionId = params.sessionId;

        // Broadcast the frame to all connected viewers
        this.onFrame(data, metadata?.timestamp ?? Date.now());

        // ACK immediately — CDP won't send the next frame until we do.
        // This is the built-in backpressure mechanism.
        this.client?.send('Page.screencastFrameAck', { sessionId }).catch(() => {
          // If ACK fails, the page may have closed — stop will handle it.
        });
      };

      this.client.on('Page.screencastFrame', this.frameHandler);

      await this.client.send('Page.startScreencast', {
        format: 'jpeg',
        quality,
        maxWidth: 1280,
        maxHeight: 1100,
        everyNthFrame,
      });

      this.running = true;
    } catch (err) {
      console.error('[screencast] failed to start:', (err as Error).message);
      this.running = false;
    }
  }

  private async stop(): Promise<void> {
    try {
      if (this.client && this.frameHandler) {
        this.client.off('Page.screencastFrame', this.frameHandler);
      }
      if (this.client) {
        await this.client.send('Page.stopScreencast').catch(() => {});
      }
    } catch {
      // Page may already be closed — swallow.
    }
    this.running = false;
    this.frameHandler = null;
  }

  /** Clean up when the session is destroyed. */
  async destroy(): Promise<void> {
    this.viewers.clear();
    await this.stop();
  }
}
