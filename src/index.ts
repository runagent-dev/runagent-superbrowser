/**
 * SuperBrowser entry point.
 *
 * Starts the browser engine, HTTP API server, and MCP server.
 */

import 'dotenv/config';
import { BrowserEngine } from './browser/engine.js';
import { LLMProvider } from './llm/provider.js';
import { createServer } from 'http';
import { createHttpServer } from './server/http.js';
import { createMCPServer } from './server/mcp.js';
import { attachWebSocketServer } from './server/websocket.js';
import { BrowserExecutor } from './agent/executor.js';

async function main(): Promise<void> {
  const mode = process.argv[2] || 'http'; // 'http', 'mcp', or 'task'

  // Initialize browser engine
  const engine = new BrowserEngine({
    headless: process.env.HEADLESS !== 'false',
    downloadDir: process.env.DOWNLOAD_DIR || '/tmp/superbrowser/downloads',
    executablePath: process.env.PUPPETEER_EXECUTABLE_PATH || undefined,
  });
  await engine.launch();
  console.log('Browser engine launched');

  // Initialize LLM provider (optional — only needed for /task endpoint and MCP mode)
  // When using nanobot as the brain, nanobot has its own LLM config
  const apiKey = process.env.ANTHROPIC_API_KEY || process.env.OPENAI_API_KEY || '';
  let llm: LLMProvider | null = null;

  if (apiKey) {
    llm = new LLMProvider({
      apiKey,
      model: process.env.LLM_MODEL || 'gpt-4o',
    });
  }

  if (!llm && (mode === 'mcp' || mode === 'task')) {
    console.error('Error: ANTHROPIC_API_KEY or OPENAI_API_KEY required for mcp/task mode');
    console.error('For session-only mode (nanobot as brain), just run: npm start');
    process.exit(1);
  }

  if (mode === 'mcp') {
    // MCP server mode (for nanobot integration)
    await createMCPServer(engine, llm!);
  } else if (mode === 'task') {
    // Direct task execution mode
    const task = process.argv.slice(3).join(' ');
    if (!task) {
      console.error('Usage: superbrowser task "Go to google.com and search for AI news"');
      process.exit(1);
    }

    const page = await engine.newPage();
    const executor = new BrowserExecutor(page, llm!);
    const result = await executor.executeTask(task);

    console.log('\n=== Result ===');
    console.log(JSON.stringify(result, null, 2));

    await page.close();
    await engine.close();
  } else {
    // HTTP server mode (default)
    const port = parseInt(process.env.PORT || '3100', 10);
    const app = createHttpServer(engine, llm, {
      maxConcurrent: parseInt(process.env.CONCURRENT || '10', 10),
      maxQueued: parseInt(process.env.QUEUED || '10', 10),
      defaultTimeout: parseInt(process.env.TIMEOUT || '60000', 10),
    });

    // Create HTTP server and attach WebSocket
    const httpServer = createServer(app);
    const sessionsGetter = (app as any)._getSessions;
    if (sessionsGetter) {
      attachWebSocketServer(httpServer, sessionsGetter);
    }

    httpServer.listen(port, () => {
      console.log(`SuperBrowser HTTP server running on port ${port}`);
      console.log(`\n  === High-level APIs ===`);
      console.log(`  POST /task       - Execute an agentic browser task (Navigator+Planner)`);
      console.log(`  POST /screenshot - Take a screenshot (browserless-compatible)`);
      console.log(`  POST /pdf        - Export page as PDF`);
      console.log(`  POST /content    - Get rendered HTML`);
      console.log(`  POST /scrape     - Scrape elements with debug data`);
      console.log(`  POST /function   - Execute arbitrary puppeteer code`);
      console.log(`\n  === Session APIs (step-by-step control) ===`);
      console.log(`  POST   /session/create       - Open browser session`);
      console.log(`  POST   /session/:id/navigate  - Navigate within session`);
      console.log(`  GET    /session/:id/screenshot - Take screenshot`);
      console.log(`  GET    /session/:id/state      - Get DOM tree + screenshot`);
      console.log(`  POST   /session/:id/click      - Click element by index or coords`);
      console.log(`  POST   /session/:id/type       - Type text into field`);
      console.log(`  POST   /session/:id/keys       - Send keyboard keys`);
      console.log(`  POST   /session/:id/scroll     - Scroll page`);
      console.log(`  POST   /session/:id/select     - Select dropdown option`);
      console.log(`  POST   /session/:id/evaluate   - Execute JavaScript (DOM context)`);
      console.log(`  POST   /session/:id/script     - Execute Puppeteer script (full page API)`);
      console.log(`  POST   /session/:id/dialog     - Handle alert/confirm/prompt`);
      console.log(`  GET    /session/:id/markdown    - Extract page as markdown`);
      console.log(`  GET    /session/:id/pdf         - Export page as PDF`);
      console.log(`  DELETE /session/:id             - Close session`);
      console.log(`  GET    /sessions               - List active sessions`);
      console.log(`\n  GET  /health  - Health check`);
      console.log(`  GET  /metrics - Session metrics`);
    });
  }

  // Graceful shutdown
  process.on('SIGINT', async () => {
    console.log('\nShutting down...');
    await engine.close();
    process.exit(0);
  });

  process.on('SIGTERM', async () => {
    await engine.close();
    process.exit(0);
  });
}

main().catch((err) => {
  console.error('Fatal error:', err);
  process.exit(1);
});
