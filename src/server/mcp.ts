/**
 * MCP server — exposes browser tools for nanobot integration.
 *
 * Uses @modelcontextprotocol/sdk with stdio transport.
 */

import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import {
  ListToolsRequestSchema,
  CallToolRequestSchema,
} from '@modelcontextprotocol/sdk/types.js';
import type { BrowserEngine } from '../browser/engine.js';
import type { LLMProvider } from '../llm/provider.js';
import { BrowserExecutor } from '../agent/executor.js';
import * as fs from 'fs';

const TOOL_DEFINITIONS = [
  {
    name: 'browse_website',
    description: 'Browse a website and perform actions interactively. Handles navigation, clicking, form filling, scrolling, and content extraction. Returns the result of the browsing task.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        task: { type: 'string', description: 'What to do on the website (e.g., "Find the pricing page and extract plan details")' },
        url: { type: 'string', description: 'Starting URL (optional — will search Google if not provided)' },
      },
      required: ['task'],
    },
  },
  {
    name: 'fill_form',
    description: 'Navigate to a page and fill a form with the provided data. Supports text inputs, selects, checkboxes, and file uploads.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        url: { type: 'string', description: 'URL of the form page' },
        form_data: { type: 'object', description: 'Field name → value pairs to fill in the form' },
        submit: { type: 'boolean', description: 'Whether to submit the form after filling (default: true)' },
      },
      required: ['url', 'form_data'],
    },
  },
  {
    name: 'take_screenshot',
    description: 'Navigate to a URL and take a screenshot of the page. Returns the screenshot as base64 JPEG.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        url: { type: 'string', description: 'URL to screenshot' },
      },
      required: ['url'],
    },
  },
  {
    name: 'extract_content',
    description: 'Navigate to a URL and extract specific information from the page.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        url: { type: 'string', description: 'URL to extract content from' },
        goal: { type: 'string', description: 'What information to extract (e.g., "Get all product names and prices")' },
      },
      required: ['url', 'goal'],
    },
  },
  {
    name: 'download_file',
    description: 'Navigate to a page, find and click a download link, and save the file.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        url: { type: 'string', description: 'URL of the page with the download link' },
        link_description: { type: 'string', description: 'Description of which link/button to click for download' },
      },
      required: ['url', 'link_description'],
    },
  },
  {
    name: 'search_and_act',
    description: 'Search Google for a query and interact with the results to complete a task.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        query: { type: 'string', description: 'Search query and what to do with the results' },
      },
      required: ['query'],
    },
  },
  {
    name: 'export_pdf',
    description: 'Navigate to a URL and export the page as a PDF file.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        url: { type: 'string', description: 'URL of the page to export' },
        output_path: { type: 'string', description: 'Where to save the PDF file' },
      },
      required: ['url'],
    },
  },
  {
    name: 'evaluate_script',
    description: 'Navigate to a URL and run a JavaScript snippet, returning the result.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        url: { type: 'string', description: 'URL of the page' },
        script: { type: 'string', description: 'JavaScript code to evaluate in the page context' },
      },
      required: ['url', 'script'],
    },
  },
];

export async function createMCPServer(
  engine: BrowserEngine,
  llm: LLMProvider,
): Promise<void> {
  const server = new Server(
    { name: 'superbrowser', version: '0.1.0' },
    { capabilities: { tools: {} } },
  );

  // List available tools
  server.setRequestHandler(ListToolsRequestSchema, async () => ({
    tools: TOOL_DEFINITIONS,
  }));

  // Handle tool calls
  server.setRequestHandler(CallToolRequestSchema, async (request) => {
    const { name, arguments: args } = request.params;
    const input = (args || {}) as Record<string, unknown>;

    try {
      switch (name) {
        case 'browse_website': {
          const page = await engine.newPage();
          if (input.url) await page.navigate(input.url as string);
          const executor = new BrowserExecutor(page, llm);
          const result = await executor.executeTask(input.task as string);
          await page.close();
          return {
            content: [
              {
                type: 'text' as const,
                text: result.success
                  ? result.finalAnswer || 'Task completed successfully'
                  : `Task failed: ${result.error}`,
              },
            ],
          };
        }

        case 'fill_form': {
          const page = await engine.newPage();
          await page.navigate(input.url as string);
          const formData = input.form_data as Record<string, string>;
          const submit = input.submit !== false;
          const task = `Fill the form with the following data: ${JSON.stringify(formData)}${submit ? ' and submit it' : ''}`;
          const executor = new BrowserExecutor(page, llm);
          const result = await executor.executeTask(task);
          await page.close();
          return {
            content: [{ type: 'text' as const, text: result.finalAnswer || result.error || 'Form filled' }],
          };
        }

        case 'take_screenshot': {
          const page = await engine.newPage();
          await page.navigate(input.url as string);
          const b64 = await page.screenshotBase64();
          await page.close();
          return {
            content: [
              { type: 'text' as const, text: `Screenshot of ${input.url}` },
              { type: 'image' as const, data: b64, mimeType: 'image/jpeg' },
            ],
          };
        }

        case 'extract_content': {
          const page = await engine.newPage();
          await page.navigate(input.url as string);
          const executor = new BrowserExecutor(page, llm);
          const result = await executor.executeTask(`Extract the following from this page: ${input.goal}`);
          await page.close();
          return {
            content: [{ type: 'text' as const, text: result.finalAnswer || result.error || 'No content extracted' }],
          };
        }

        case 'download_file': {
          const page = await engine.newPage();
          await page.navigate(input.url as string);
          const executor = new BrowserExecutor(page, llm);
          const result = await executor.executeTask(`Find and click the download link: ${input.link_description}`);
          await page.close();
          return {
            content: [{ type: 'text' as const, text: result.finalAnswer || result.error || 'Download attempted' }],
          };
        }

        case 'search_and_act': {
          const page = await engine.newPage();
          const executor = new BrowserExecutor(page, llm);
          const result = await executor.executeTask(`Search Google for "${input.query}" and complete the task`);
          await page.close();
          return {
            content: [{ type: 'text' as const, text: result.finalAnswer || result.error || 'Search completed' }],
          };
        }

        case 'export_pdf': {
          const page = await engine.newPage();
          await page.navigate(input.url as string);
          const buffer = await page.exportPdf();
          const savePath = (input.output_path as string) || '/tmp/superbrowser/downloads/page.pdf';
          const dir = savePath.substring(0, savePath.lastIndexOf('/'));
          if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
          fs.writeFileSync(savePath, buffer);
          await page.close();
          return {
            content: [{ type: 'text' as const, text: `PDF saved to ${savePath} (${(buffer.length / 1024).toFixed(1)} KB)` }],
          };
        }

        case 'evaluate_script': {
          const page = await engine.newPage();
          await page.navigate(input.url as string);
          const result = await page.evaluateScript(input.script as string);
          await page.close();
          const resultStr = typeof result === 'object' ? JSON.stringify(result, null, 2) : String(result);
          return {
            content: [{ type: 'text' as const, text: `Script result:\n${resultStr}` }],
          };
        }

        default:
          return {
            content: [{ type: 'text' as const, text: `Unknown tool: ${name}` }],
            isError: true,
          };
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      return {
        content: [{ type: 'text' as const, text: `Error: ${msg}` }],
        isError: true,
      };
    }
  });

  // Connect via stdio transport
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error('SuperBrowser MCP server started (stdio)');
}
