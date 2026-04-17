/**
 * Low-level CDP keyboard input dispatch from BrowserOS.
 *
 * Dispatches raw Input.dispatchKeyEvent for precise keyboard control.
 * Handles key combos like "Control+A", "Meta+Shift+P", special keys, etc.
 */

import type { CDPSession } from 'puppeteer-core';
import { Modifiers } from './input-mouse.js';

/** Key code mappings for special keys. */
const KEY_CODES: Record<string, { key: string; code: string; keyCode: number }> = {
  Enter: { key: 'Enter', code: 'Enter', keyCode: 13 },
  Tab: { key: 'Tab', code: 'Tab', keyCode: 9 },
  Backspace: { key: 'Backspace', code: 'Backspace', keyCode: 8 },
  Delete: { key: 'Delete', code: 'Delete', keyCode: 46 },
  Escape: { key: 'Escape', code: 'Escape', keyCode: 27 },
  ArrowUp: { key: 'ArrowUp', code: 'ArrowUp', keyCode: 38 },
  ArrowDown: { key: 'ArrowDown', code: 'ArrowDown', keyCode: 40 },
  ArrowLeft: { key: 'ArrowLeft', code: 'ArrowLeft', keyCode: 37 },
  ArrowRight: { key: 'ArrowRight', code: 'ArrowRight', keyCode: 39 },
  Home: { key: 'Home', code: 'Home', keyCode: 36 },
  End: { key: 'End', code: 'End', keyCode: 35 },
  PageUp: { key: 'PageUp', code: 'PageUp', keyCode: 33 },
  PageDown: { key: 'PageDown', code: 'PageDown', keyCode: 34 },
  Space: { key: ' ', code: 'Space', keyCode: 32 },
  F1: { key: 'F1', code: 'F1', keyCode: 112 },
  F2: { key: 'F2', code: 'F2', keyCode: 113 },
  F5: { key: 'F5', code: 'F5', keyCode: 116 },
  F12: { key: 'F12', code: 'F12', keyCode: 123 },
};

/** Modifier key names. */
const MODIFIER_KEYS = new Set(['Control', 'Alt', 'Shift', 'Meta', 'Cmd', 'Command']);

/**
 * Type text character by character via CDP key events.
 *
 * Defaults to humanized typing (variable per-keystroke delay 30-120ms,
 * occasional typo + backspace, longer pauses after punctuation, rare
 * thinking-delay). Detectors that score keystroke-interval variance
 * (Keystroke Dynamics) flag uniform `delay=30` as automated.
 *
 * Set `{ humanize: false }` to opt out for deterministic internal paths
 * (e.g., pasting known-good text into a form field).
 */
export async function typeText(
  client: CDPSession,
  text: string,
  delay: number = 30,
  options?: { humanize?: boolean; sessionId?: string },
): Promise<void> {
  // Humanize by default; opt-out preserves the old fixed-cadence path.
  // Uses humanTypeOrPaste which auto-detects clipboard-worthy inputs
  // (URLs, emails, long strings) and simulates Ctrl+V for those.
  if (options?.humanize !== false) {
    const { humanTypeOrPaste } = await import('./humanize.js');
    await humanTypeOrPaste(client, text, { sessionId: options?.sessionId });
    return;
  }
  for (const char of text) {
    if (char === '\n' || char === '\r') {
      // Send Enter key
      await dispatchKey(client, 'Enter');
    } else {
      // Character input: keyDown → char → keyUp
      const keyCode = char.charCodeAt(0);
      await client.send('Input.dispatchKeyEvent', {
        type: 'keyDown',
        key: char,
        code: `Key${char.toUpperCase()}`,
        windowsVirtualKeyCode: keyCode,
        nativeVirtualKeyCode: keyCode,
      });
      await client.send('Input.dispatchKeyEvent', {
        type: 'char',
        text: char,
        key: char,
        code: `Key${char.toUpperCase()}`,
      });
      await client.send('Input.dispatchKeyEvent', {
        type: 'keyUp',
        key: char,
        code: `Key${char.toUpperCase()}`,
        windowsVirtualKeyCode: keyCode,
        nativeVirtualKeyCode: keyCode,
      });
    }
    if (delay > 0) {
      await new Promise((r) => setTimeout(r, delay));
    }
  }
}

/**
 * Dispatch a single key press (keyDown + keyUp).
 */
export async function dispatchKey(
  client: CDPSession,
  key: string,
  modifiers: number = 0,
): Promise<void> {
  const info = KEY_CODES[key] || { key, code: `Key${key.toUpperCase()}`, keyCode: key.charCodeAt(0) };

  await client.send('Input.dispatchKeyEvent', {
    type: 'keyDown',
    key: info.key,
    code: info.code,
    windowsVirtualKeyCode: info.keyCode,
    nativeVirtualKeyCode: info.keyCode,
    modifiers,
  });

  await client.send('Input.dispatchKeyEvent', {
    type: 'keyUp',
    key: info.key,
    code: info.code,
    windowsVirtualKeyCode: info.keyCode,
    nativeVirtualKeyCode: info.keyCode,
    modifiers,
  });
}

/**
 * Parse and execute a key combo string like "Control+A", "Meta+Shift+P".
 * Pattern from BrowserOS keyboard.ts.
 */
export async function pressKeyCombo(
  client: CDPSession,
  combo: string,
): Promise<void> {
  const parts = combo.split('+');
  let modifierBitmask = 0;
  const modifierKeys: string[] = [];
  let mainKey = '';

  for (const part of parts) {
    const normalized = normalizeModifier(part);
    if (MODIFIER_KEYS.has(normalized)) {
      modifierKeys.push(normalized);
      if (normalized === 'Control' || normalized === 'Cmd' || normalized === 'Command') {
        modifierBitmask |= Modifiers.Control;
      } else if (normalized === 'Alt') {
        modifierBitmask |= Modifiers.Alt;
      } else if (normalized === 'Shift') {
        modifierBitmask |= Modifiers.Shift;
      } else if (normalized === 'Meta') {
        modifierBitmask |= Modifiers.Meta;
      }
    } else {
      mainKey = part;
    }
  }

  // If no main key, it might be just a modifier (unlikely)
  if (!mainKey && modifierKeys.length > 0) {
    mainKey = modifierKeys.pop()!;
  }

  // Press modifier keys down
  for (const mod of modifierKeys) {
    await client.send('Input.dispatchKeyEvent', {
      type: 'keyDown',
      key: mod,
      code: `${mod}Left`,
      modifiers: modifierBitmask,
    });
  }

  // Press and release main key
  await dispatchKey(client, mainKey, modifierBitmask);

  // Release modifier keys
  for (const mod of modifierKeys.reverse()) {
    await client.send('Input.dispatchKeyEvent', {
      type: 'keyUp',
      key: mod,
      code: `${mod}Left`,
      modifiers: 0,
    });
  }
}

/**
 * Clear a text field: Select All (Ctrl+A) → Backspace.
 * With triple-click fallback if field not empty after first attempt.
 * Pattern from BrowserOS keyboard.ts clearField.
 */
export async function clearField(
  client: CDPSession,
  x: number,
  y: number,
): Promise<void> {
  // First attempt: Ctrl+A → Backspace
  await pressKeyCombo(client, 'Control+a');
  await new Promise((r) => setTimeout(r, 50));
  await dispatchKey(client, 'Backspace');
  await new Promise((r) => setTimeout(r, 50));

  // If still has content, try triple-click + Backspace
  // The caller should check if the field is actually empty
}

function normalizeModifier(key: string): string {
  const lower = key.toLowerCase();
  if (lower === 'cmd' || lower === 'command') return 'Meta';
  if (lower === 'ctrl') return 'Control';
  return key.charAt(0).toUpperCase() + key.slice(1);
}
