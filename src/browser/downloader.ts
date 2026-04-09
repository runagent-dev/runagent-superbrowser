/**
 * File download handling.
 *
 * Uses CDP Page.setDownloadBehavior and download events.
 * Pattern from browserless.
 */

import type { CDPSession } from 'puppeteer-core';
import * as fs from 'fs';
import * as path from 'path';

export interface DownloadResult {
  filename: string;
  path: string;
  size: number;
}

export class DownloadManager {
  private downloadDir: string;
  private completedDownloads: DownloadResult[] = [];

  constructor(downloadDir: string) {
    this.downloadDir = downloadDir;
    if (!fs.existsSync(downloadDir)) {
      fs.mkdirSync(downloadDir, { recursive: true });
    }
  }

  /** Setup download behavior via CDP. */
  async setup(client: CDPSession): Promise<void> {
    await client.send('Page.setDownloadBehavior', {
      behavior: 'allow',
      downloadPath: this.downloadDir,
    });

    client.on('Page.downloadWillBegin', (event: { suggestedFilename?: string; url?: string }) => {
      const filename = event.suggestedFilename || 'download';
      console.log(`Download started: ${filename}`);
    });

    client.on('Page.downloadProgress', (event: { state: string; receivedBytes?: number; totalBytes?: number }) => {
      if (event.state === 'completed') {
        console.log('Download completed');
      }
    });
  }

  /**
   * Wait for a download to complete by polling the download directory.
   * Returns info about the most recently downloaded file.
   */
  async waitForDownload(timeout: number = 30000): Promise<DownloadResult | null> {
    const startFiles = new Set(fs.readdirSync(this.downloadDir));
    const startTime = Date.now();

    while (Date.now() - startTime < timeout) {
      await new Promise((r) => setTimeout(r, 1000));

      const currentFiles = fs.readdirSync(this.downloadDir);
      for (const file of currentFiles) {
        // Skip temp Chrome download files
        if (file.endsWith('.crdownload') || file.endsWith('.tmp')) continue;
        if (!startFiles.has(file)) {
          const filePath = path.join(this.downloadDir, file);
          const stat = fs.statSync(filePath);
          const result: DownloadResult = {
            filename: file,
            path: filePath,
            size: stat.size,
          };
          this.completedDownloads.push(result);
          return result;
        }
      }
    }

    return null;
  }

  /** Get all completed downloads. */
  getDownloads(): DownloadResult[] {
    return [...this.completedDownloads];
  }
}
