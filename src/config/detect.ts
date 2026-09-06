/**
 * Machine-profile detection + Chrome discovery (TS twin of
 * nanobot/superbrowser_config/detect.py). The Chrome candidate list mirrors
 * bin/superbrowser-doctor.js — keep all three in sync so "auto" resolves to
 * the same binary the doctor reports.
 */

import { existsSync, readFileSync } from 'node:fs';

export type Profile = 'local' | 'vm' | 'docker';

export function chromeCandidates(): string[] {
  if (process.platform === 'darwin') {
    return ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'];
  }
  if (process.platform === 'win32') {
    return [
      `${process.env['ProgramFiles'] || 'C:\\Program Files'}\\Google\\Chrome\\Application\\chrome.exe`,
      `${process.env['ProgramFiles(x86)'] || 'C:\\Program Files (x86)'}\\Google\\Chrome\\Application\\chrome.exe`,
      `${process.env.LOCALAPPDATA || ''}\\Google\\Chrome\\Application\\chrome.exe`,
    ];
  }
  return ['/usr/bin/google-chrome-stable', '/usr/bin/google-chrome'];
}

export function findChrome(): string | null {
  for (const candidate of chromeCandidates()) {
    if (candidate && existsSync(candidate)) return candidate;
  }
  return null;
}

export function inContainer(): boolean {
  if (existsSync('/.dockerenv')) return true;
  try {
    const cgroup = readFileSync('/proc/1/cgroup', 'utf-8');
    return ['docker', 'containerd', 'kubepods'].some((marker) => cgroup.includes(marker));
  } catch {
    return false;
  }
}

export function detectProfile(env: NodeJS.ProcessEnv = process.env): Profile {
  if (inContainer()) return 'docker';
  if (process.platform === 'darwin' || process.platform === 'win32') return 'local';
  // Linux: a desktop session means a laptop/workstation; a bare box is a VM.
  if (env.DISPLAY || env.WAYLAND_DISPLAY || env.XDG_CURRENT_DESKTOP) return 'local';
  return 'vm';
}
