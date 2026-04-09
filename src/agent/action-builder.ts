/**
 * Build the default action registry with all available actions.
 */

import { ActionRegistry } from './actions/registry.js';

// Navigation
import { navigateAction, searchGoogleAction, goBackAction } from './actions/navigation.js';

// Interaction
import {
  clickElementAction, inputTextAction, selectOptionAction, sendKeysAction,
  getDropdownOptionsAction, selectDropdownByTextAction,
} from './actions/interaction.js';

// Scroll
import {
  scrollDownAction, scrollUpAction, scrollToPercentAction,
  scrollToTopAction, scrollToBottomAction, scrollToTextAction,
} from './actions/scroll.js';

// Tabs
import { openTabAction, switchTabAction, closeTabAction } from './actions/tabs.js';

// Control
import { doneAction, waitAction, cacheContentAction, createAskHumanAction } from './actions/control.js';
import type { HumanInputManager } from './human-input.js';

// Advanced (BrowserOS)
import { handleDialogAction, uploadFileAction, evaluateScriptAction } from './actions/advanced.js';

// Captcha
import { detectCaptchaAction, screenshotCaptchaAction } from './actions/captcha.js';

// Extraction (BrowserOS)
import {
  extractMarkdownAction,
  exportPdfAction,
  domSearchAction,
  waitForConditionAction,
  getConsoleErrorsAction,
  getAccessibilityTreeAction,
} from './actions/extraction.js';

/**
 * Create and populate an ActionRegistry with all default actions.
 */
export function buildDefaultActionRegistry(humanInput?: HumanInputManager): ActionRegistry {
  const registry = new ActionRegistry();

  // Core actions (from nanobrowser patterns)
  registry.register(doneAction);
  registry.register(navigateAction);
  registry.register(searchGoogleAction);
  registry.register(goBackAction);
  registry.register(clickElementAction);
  registry.register(inputTextAction);
  registry.register(selectOptionAction);
  registry.register(sendKeysAction);
  registry.register(getDropdownOptionsAction);
  registry.register(selectDropdownByTextAction);
  registry.register(scrollDownAction);
  registry.register(scrollUpAction);
  registry.register(scrollToPercentAction);
  registry.register(scrollToTopAction);
  registry.register(scrollToBottomAction);
  registry.register(scrollToTextAction);
  registry.register(openTabAction);
  registry.register(switchTabAction);
  registry.register(closeTabAction);
  registry.register(waitAction);
  registry.register(cacheContentAction);

  // Advanced actions (from BrowserOS patterns)
  registry.register(handleDialogAction);
  registry.register(uploadFileAction);
  registry.register(evaluateScriptAction);

  // Extraction actions (from BrowserOS patterns)
  registry.register(extractMarkdownAction);
  registry.register(exportPdfAction);
  registry.register(domSearchAction);
  registry.register(waitForConditionAction);
  registry.register(getConsoleErrorsAction);
  registry.register(getAccessibilityTreeAction);

  // Captcha
  registry.register(detectCaptchaAction);
  registry.register(screenshotCaptchaAction);

  // Human-in-the-loop
  if (humanInput) {
    registry.register(createAskHumanAction(humanInput));
  }

  return registry;
}
