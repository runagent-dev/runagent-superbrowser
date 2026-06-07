#!/usr/bin/env node
// superbrowser-doctor — verify the Node/TS side of a SuperBrowser install and
// tell you exactly what to do next. Zero runtime deps; safe to run repeatedly.
//
//   superbrowser-doctor          # report
//
// Checks Node version, locates a usable Chrome (puppeteer-core does NOT bundle
// one), confirms the engine is built, ensures a .env exists, and pings the
// running server. Exits non-zero only on a critical failure (Node < 20).

import { existsSync, copyFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { platform, homedir } from 'node:os';
import http from 'node:http';
import process from 'node:process';

const OK = '\x1b[32m✓\x1b[0m';
const WARN = '\x1b[33m!\x1b[0m';
const BAD = '\x1b[31m✗\x1b[0m';
const line = (sym, msg) => console.log(` ${sym} ${msg}`);

const pkgRoot = join(dirname(fileURLToPath(import.meta.url)), '..');
const SERVER_URL = process.env.SUPERBROWSER_URL || `http://localhost:${process.env.PORT || 3100}`;

function checkNode() {
  const major = Number(process.versions.node.split('.')[0]);
  if (major >= 20) {
    line(OK, `Node ${process.versions.node}`);
    return true;
  }
  line(BAD, `Node ${process.versions.node} — need 20+. Install from nodejs.org or via nvm/winget/brew.`);
  return false;
}

function chromeCandidates() {
  switch (platform()) {
    case 'darwin':
      return [
        '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
        '/Applications/Chromium.app/Contents/MacOS/Chromium',
      ];
    case 'win32':
      return [
        `${process.env['ProgramFiles'] || 'C:\\Program Files'}\\Google\\Chrome\\Application\\chrome.exe`,
        `${process.env['ProgramFiles(x86)'] || 'C:\\Program Files (x86)'}\\Google\\Chrome\\Application\\chrome.exe`,
        `${process.env.LOCALAPPDATA || ''}\\Google\\Chrome\\Application\\chrome.exe`,
      ];
    default:
      return [
        '/usr/bin/google-chrome-stable',
        '/usr/bin/google-chrome',
        '/usr/bin/chromium',
        '/usr/bin/chromium-browser',
      ];
  }
}

function checkChrome() {
  const fromEnv = process.env.PUPPETEER_EXECUTABLE_PATH;
  if (fromEnv && existsSync(fromEnv)) {
    line(OK, `Chrome (PUPPETEER_EXECUTABLE_PATH) -> ${fromEnv}`);
    return;
  }
  if (fromEnv) {
    line(BAD, `PUPPETEER_EXECUTABLE_PATH set but not found: ${fromEnv}`);
  }
  const found = chromeCandidates().find((p) => existsSync(p));
  if (found) {
    line(OK, `Chrome found -> ${found}`);
    if (!fromEnv) line(WARN, `  set PUPPETEER_EXECUTABLE_PATH="${found}" in .env for stable launches`);
  } else {
    const tip =
      platform() === 'darwin'
        ? 'brew install --cask google-chrome'
        : platform() === 'win32'
          ? 'winget install Google.Chrome'
          : 'sudo apt install -y google-chrome-stable  (see README for the apt repo setup)';
    line(WARN, `No Chrome/Chromium found. puppeteer-core needs a real one — install it: ${tip}`);
  }
}

function checkBuild() {
  if (existsSync(join(pkgRoot, 'build', 'index.js'))) {
    line(OK, 'engine built (build/index.js)');
  } else {
    line(WARN, 'build/index.js missing — run `npm run build`');
  }
}

function checkEnv() {
  const env = join(pkgRoot, '.env');
  const example = join(pkgRoot, '.env.example');
  if (existsSync(env)) {
    line(OK, '.env present');
  } else if (existsSync(example)) {
    copyFileSync(example, env);
    line(OK, 'created .env from .env.example — edit the keys you care about');
  } else {
    line(WARN, 'no .env or .env.example found');
  }
}

function checkServer() {
  return new Promise((resolve) => {
    const req = http.get(`${SERVER_URL}/health`, { timeout: 3000 }, (res) => {
      res.resume();
      if (res.statusCode === 200) line(OK, `engine reachable at ${SERVER_URL}`);
      else line(WARN, `engine responded ${res.statusCode} at ${SERVER_URL}`);
      resolve();
    });
    req.on('error', () => {
      line(WARN, `engine not running at ${SERVER_URL} — start it with \`superbrowser\` (or \`npm start\`)`);
      resolve();
    });
    req.on('timeout', () => {
      req.destroy();
      line(WARN, `engine timed out at ${SERVER_URL}`);
      resolve();
    });
  });
}

console.log('SuperBrowser doctor — Node/TS side\n');
const nodeOk = checkNode();
checkChrome();
checkBuild();
checkEnv();
await checkServer();
console.log(
  nodeOk
    ? '\nReady. Start the engine with `superbrowser`; for captcha/Tier-3 also set up the Python bridge (superbrowser-doctor).'
    : '\nNode is too old — fix that first.',
);
process.exit(nodeOk ? 0 : 1);
