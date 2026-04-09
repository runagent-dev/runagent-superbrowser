/**
 * Anti-detection stealth scripts.
 *
 * Patterns from browserless (puppeteer-extra-plugin-stealth) and nanobrowser,
 * written from scratch. Injected via page.evaluateOnNewDocument() before any
 * page script runs.
 */

export function getStealthScript(): string {
  return `
(function () {
  // 1. Hide navigator.webdriver
  Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined,
  });

  // 2. Spoof window.chrome.runtime
  if (!window.chrome) {
    Object.defineProperty(window, 'chrome', {
      value: {},
      writable: true,
    });
  }
  if (!window.chrome.runtime) {
    window.chrome.runtime = {
      connect: function () {},
      sendMessage: function () {},
    };
  }

  // 3. Populate navigator.plugins (non-empty PluginArray)
  Object.defineProperty(navigator, 'plugins', {
    get: () => {
      const fakePlugins = [
        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
        { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
      ];
      const arr = Object.create(PluginArray.prototype);
      fakePlugins.forEach((p, i) => {
        const plugin = Object.create(Plugin.prototype);
        Object.defineProperties(plugin, {
          name: { value: p.name },
          filename: { value: p.filename },
          description: { value: p.description },
          length: { value: 0 },
        });
        arr[i] = plugin;
      });
      Object.defineProperty(arr, 'length', { value: fakePlugins.length });
      return arr;
    },
  });

  // 4. Set navigator.languages
  Object.defineProperty(navigator, 'languages', {
    get: () => ['en-US', 'en'],
  });

  // 5. Override Notification.permission
  if (typeof Notification !== 'undefined') {
    Object.defineProperty(Notification, 'permission', {
      get: () => 'default',
    });
  }

  // 6. Spoof WebGL renderer/vendor info
  const getParameterOriginal = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function (param) {
    const UNMASKED_VENDOR = 0x9245;
    const UNMASKED_RENDERER = 0x9246;
    if (param === UNMASKED_VENDOR) return 'Intel Inc.';
    if (param === UNMASKED_RENDERER) return 'Intel Iris OpenGL Engine';
    return getParameterOriginal.call(this, param);
  };
  if (typeof WebGL2RenderingContext !== 'undefined') {
    const getParameter2Original = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function (param) {
      const UNMASKED_VENDOR = 0x9245;
      const UNMASKED_RENDERER = 0x9246;
      if (param === UNMASKED_VENDOR) return 'Intel Inc.';
      if (param === UNMASKED_RENDERER) return 'Intel Iris OpenGL Engine';
      return getParameter2Original.call(this, param);
    };
  }

  // 7. Patch navigator.permissions.query for consistent responses
  const originalQuery = navigator.permissions.query.bind(navigator.permissions);
  navigator.permissions.query = function (params) {
    if (params.name === 'notifications') {
      return Promise.resolve({ state: Notification.permission, onchange: null });
    }
    return originalQuery(params);
  };

  // 8. Fix iframe contentWindow access detection
  const originalAttachShadow = Element.prototype.attachShadow;
  Element.prototype.attachShadow = function () {
    return originalAttachShadow.call(this, ...arguments);
  };

  // 9. Consistent user-agent data
  if (navigator.userAgentData) {
    Object.defineProperty(navigator, 'userAgentData', {
      get: () => ({
        brands: [
          { brand: 'Chromium', version: '120' },
          { brand: 'Google Chrome', version: '120' },
          { brand: 'Not_A Brand', version: '8' },
        ],
        mobile: false,
        platform: 'macOS',
        getHighEntropyValues: () =>
          Promise.resolve({
            architecture: 'x86',
            model: '',
            platform: 'macOS',
            platformVersion: '14.0.0',
            uaFullVersion: '120.0.6099.109',
          }),
      }),
    });
  }
})();
`;
}

/**
 * Additional stealth: spoof the navigator.platform to match user-agent.
 */
export function getPlatformOverrideScript(platform: string = 'MacIntel'): string {
  return `
Object.defineProperty(navigator, 'platform', {
  get: () => '${platform}',
});
`;
}
