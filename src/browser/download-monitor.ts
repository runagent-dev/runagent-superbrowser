/**
 * Download monitoring via CDP events from BrowserOS.
 *
 * Uses Browser.downloadWillBegin and Browser.downloadProgress
 * for reliable download tracking.
 */

import type { CDPSession } from 'puppeteer-core';

export interface DownloadEvent {
  guid: string;
  url: string;
  suggestedFilename: string;
  state: 'inProgress' | 'completed' | 'canceled';
  receivedBytes: number;
  totalBytes: number;
  startTime: number;
  endTime?: number;
}

export class DownloadMonitor {
  private downloads = new Map<string, DownloadEvent>();
  private enabled = false;

  /** Enable download monitoring on a CDP session. */
  async enable(client: CDPSession, downloadPath: string): Promise<void> {
    if (this.enabled) return;

    // Set download behavior
    await client.send('Browser.setDownloadBehavior' as any, {
      behavior: 'allow',
      downloadPath,
      eventsEnabled: true,
    }).catch(async () => {
      // Fallback to Page.setDownloadBehavior
      await client.send('Page.setDownloadBehavior', {
        behavior: 'allow',
        downloadPath,
      });
    });

    // Listen for download start
    client.on('Browser.downloadWillBegin' as any, (params: any) => {
      this.downloads.set(params.guid, {
        guid: params.guid,
        url: params.url,
        suggestedFilename: params.suggestedFilename,
        state: 'inProgress',
        receivedBytes: 0,
        totalBytes: 0,
        startTime: Date.now(),
      });
    });

    // Also listen to Page-level events (some Chrome versions)
    client.on('Page.downloadWillBegin' as any, (params: any) => {
      const guid = params.guid || `page-${Date.now()}`;
      this.downloads.set(guid, {
        guid,
        url: params.url || '',
        suggestedFilename: params.suggestedFilename || 'download',
        state: 'inProgress',
        receivedBytes: 0,
        totalBytes: 0,
        startTime: Date.now(),
      });
    });

    // Listen for download progress
    client.on('Browser.downloadProgress' as any, (params: any) => {
      const download = this.downloads.get(params.guid);
      if (download) {
        download.state = params.state;
        download.receivedBytes = params.receivedBytes || 0;
        download.totalBytes = params.totalBytes || 0;
        if (params.state === 'completed' || params.state === 'canceled') {
          download.endTime = Date.now();
        }
      }
    });

    client.on('Page.downloadProgress' as any, (params: any) => {
      // Match by any inProgress download
      for (const download of this.downloads.values()) {
        if (download.state === 'inProgress') {
          download.state = params.state;
          download.receivedBytes = params.receivedBytes || 0;
          download.totalBytes = params.totalBytes || 0;
          if (params.state === 'completed' || params.state === 'canceled') {
            download.endTime = Date.now();
          }
          break;
        }
      }
    });

    this.enabled = true;
  }

  /**
   * Wait for a download to complete.
   * Returns the download info or null if timeout.
   */
  async waitForDownload(timeout: number = 30000): Promise<DownloadEvent | null> {
    const startTime = Date.now();

    while (Date.now() - startTime < timeout) {
      for (const download of this.downloads.values()) {
        if (download.state === 'completed') {
          return download;
        }
      }
      await new Promise((r) => setTimeout(r, 500));
    }

    return null;
  }

  /** Get all tracked downloads. */
  getDownloads(): DownloadEvent[] {
    return Array.from(this.downloads.values());
  }

  /** Get active (in-progress) downloads. */
  getActiveDownloads(): DownloadEvent[] {
    return Array.from(this.downloads.values()).filter(
      (d) => d.state === 'inProgress',
    );
  }

  /** Clear download history. */
  clear(): void {
    this.downloads.clear();
  }
}
