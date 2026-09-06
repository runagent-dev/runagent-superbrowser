/**
 * Machine-profile presets — marker-wrapped verbatim copy of
 * nanobot/superbrowser_config/presets.json. Edit BOTH files together;
 * scripts/check_config_parity.sh fails CI when they drift.
 */

export const PRESETS: Record<string, Record<string, unknown>> =
  // PRESETS_JSON_START
  {
    "local": {
      "engine": {
        "headless": true,
        "chromePath": "auto",
        "concurrency": { "maxConcurrent": 3, "maxQueued": 5, "maxSessions": 5 }
      },
      "vision": { "provider": "gemini", "model": "gemini-2.0-flash-exp", "somOverlay": true, "cacheTtlSec": 30 },
      "antibot": { "cookieJar": true, "learningReads": false },
      "t3": { "chromePath": "auto", "persistProfile": true, "headless": false, "autoXvfb": false, "viewerPort": 3101 }
    },
    "vm": {
      "engine": {
        "headless": true,
        "chromePath": "/usr/bin/google-chrome-stable",
        "concurrency": {
          "maxConcurrent": 10,
          "maxQueued": 10,
          "maxSessions": 20,
          "rateLimitPerMin": 200,
          "taskTimeoutMs": 300000
        }
      },
      "vision": { "provider": "gemini", "model": "gemini-2.0-flash-exp", "somOverlay": true, "cacheTtlSec": 30 },
      "antibot": { "cookieJar": true, "learningReads": false },
      "t3": {
        "chromePath": "/usr/bin/google-chrome-stable",
        "persistProfile": true,
        "headless": false,
        "autoXvfb": true,
        "xvfbDisplay": ":99",
        "viewerPort": 3101
      }
    },
    "docker": {}
  }
  // PRESETS_JSON_END
  ;
