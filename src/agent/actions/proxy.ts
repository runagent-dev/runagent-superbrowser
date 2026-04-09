/**
 * Proxy and geo-block handling actions for the agent.
 */

import { z } from 'zod';
import { Action } from './registry.js';

const GEO_BLOCK_PATTERNS = [
  /access.*(limited|restricted|denied|blocked)/i,
  /not available in your (region|country|area|location)/i,
  /geo.?restrict/i,
  /this (service|content|website|site) is (not|only) available/i,
  /country.*(not|un).*(support|available)/i,
  /your (ip|location|region) (is|has been) (blocked|restricted)/i,
  /unavailable in your region/i,
  /access from your country/i,
];

/** Detect if the current page is geo-blocked. */
export const detectGeoBlockAction = new Action({
  name: 'detect_geo_block',
  description: 'Check if the current page is geo-restricted (showing "access limited", "not available in your region", etc.). Returns whether the page is blocked and suggests a region/proxy to use.',
  schema: z.object({}),
  handler: async (_input, page) => {
    const rawPage = page.getRawPage();
    const bodyText = await rawPage.evaluate(() => document.body?.innerText || '');
    const url = rawPage.url();

    const isBlocked = GEO_BLOCK_PATTERNS.some((p) => p.test(bodyText));

    if (!isBlocked) {
      return {
        success: true,
        extractedContent: 'Page is NOT geo-blocked. Access is normal.',
      };
    }

    // Try to identify the country from page content
    const lower = bodyText.toLowerCase();
    const countryHints: Record<string, string[]> = {
      'bd (Bangladesh)': ['bangladesh', 'bangla', 'dhaka'],
      'in (India)': ['india', 'indian', 'bharat'],
      'gb (UK)': ['united kingdom', 'british'],
      'us (USA)': ['united states', 'american'],
      'jp (Japan)': ['japan', 'japanese'],
    };

    let suggestedRegion = '';
    for (const [region, keywords] of Object.entries(countryHints)) {
      if (keywords.some((kw) => lower.includes(kw))) {
        suggestedRegion = region;
        break;
      }
    }

    return {
      success: true,
      extractedContent: `PAGE IS GEO-BLOCKED. The site restricts access by location.${suggestedRegion ? ` Suggested region: ${suggestedRegion}.` : ''} To bypass: close this session and create a new one with the region parameter (e.g., browser_open with region="${suggestedRegion.split(' ')[0] || 'xx'}"). This requires a proxy to be configured for that region.`,
      includeInMemory: true,
    };
  },
});
