/**
 * Anti-detection stealth scripts.
 *
 * Patterns from browserless (puppeteer-extra-plugin-stealth) and nanobrowser,
 * written from scratch. Injected via page.evaluateOnNewDocument() before any
 * page script runs.
 */

export interface StealthOptions {
  /** Per-session seed (0-2^31) so canvas/audio noise is consistent within a session but differs across sessions. */
  sessionSeed?: number;
  /** Brand/version for UA Client Hints — must match navigator.userAgent set in engine.ts. */
  chromeVersion?: string;
  /** Platform string reported via userAgentData.platform and Sec-CH-UA-Platform. */
  platform?: 'macOS' | 'Windows' | 'Linux';
}

export function getStealthScript(opts: StealthOptions = {}): string {
  const seed = opts.sessionSeed ?? Math.floor(Math.random() * 2147483647);
  const chromeVersion = opts.chromeVersion ?? '130.0.6723.91';
  const chromeMajor = chromeVersion.split('.')[0];
  const platform = opts.platform ?? 'macOS';
  return `
(function () {
  // Per-session PRNG — consistent within session, differs across sessions.
  // Mulberry32 algorithm: fast, good distribution, tiny state.
  var __seed = ${seed} >>> 0;
  function __rand() {
    __seed = (__seed + 0x6D2B79F5) >>> 0;
    var t = __seed;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  }

  // 1. Hide navigator.webdriver — use defineProperty with configurable:true so
  //    getOwnPropertyDescriptor(navigator,'webdriver') returns a descriptor
  //    indistinguishable from real browsers (which have no 'webdriver' property
  //    at all, so undefined getter is the right shape).
  try {
    Object.defineProperty(Navigator.prototype, 'webdriver', {
      configurable: true,
      enumerable: true,
      get: function () { return undefined; },
    });
  } catch (e) {}
  try {
    // Belt-and-suspenders: override on instance too (some detectors bypass prototype)
    Object.defineProperty(navigator, 'webdriver', {
      configurable: true,
      enumerable: true,
      get: function () { return undefined; },
    });
  } catch (e) {}

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

  // 9. Consistent user-agent data — derived from config so navigator.userAgent,
  //    userAgentData, and Accept-CH all agree. Drift is a common detection signal.
  var __chromeMajor = '${chromeMajor}';
  var __chromeFull = '${chromeVersion}';
  var __platform = '${platform}';
  var __platformVersion = __platform === 'macOS' ? '14.5.0'
                          : __platform === 'Windows' ? '10.0.0'
                          : '6.5.0';
  if (navigator.userAgentData) {
    Object.defineProperty(navigator, 'userAgentData', {
      configurable: true,
      get: () => ({
        brands: [
          { brand: 'Chromium', version: __chromeMajor },
          { brand: 'Google Chrome', version: __chromeMajor },
          { brand: 'Not_A Brand', version: '24' },
        ],
        mobile: false,
        platform: __platform,
        getHighEntropyValues: () =>
          Promise.resolve({
            architecture: 'x86',
            bitness: '64',
            model: '',
            platform: __platform,
            platformVersion: __platformVersion,
            uaFullVersion: __chromeFull,
            wow64: false,
            fullVersionList: [
              { brand: 'Chromium', version: __chromeFull },
              { brand: 'Google Chrome', version: __chromeFull },
              { brand: 'Not_A Brand', version: '24.0.0.0' },
            ],
          }),
        toJSON: function () {
          return {
            brands: this.brands,
            mobile: this.mobile,
            platform: this.platform,
          };
        },
      }),
    });
  }

  // 10. Screen dimensions consistency (headless often has wrong values)
  Object.defineProperty(screen, 'width', { get: () => 1920 });
  Object.defineProperty(screen, 'height', { get: () => 1080 });
  Object.defineProperty(screen, 'availWidth', { get: () => 1920 });
  Object.defineProperty(screen, 'availHeight', { get: () => 1040 });
  Object.defineProperty(screen, 'colorDepth', { get: () => 24 });
  Object.defineProperty(screen, 'pixelDepth', { get: () => 24 });

  // 11. Network connection info (headless detection vector)
  if (navigator.connection) {
    Object.defineProperty(navigator, 'connection', {
      get: () => ({
        effectiveType: '4g',
        rtt: 50,
        downlink: 10,
        saveData: false,
      }),
    });
  }

  // 12. Hardware fingerprinting (headless often reports 1-2 cores)
  Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
  Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });

  // 13. Battery API spoofing (headless detection vector)
  if (navigator.getBattery) {
    navigator.getBattery = () => Promise.resolve({
      charging: true,
      chargingTime: 0,
      dischargingTime: Infinity,
      level: 1,
      addEventListener: function() {},
      removeEventListener: function() {},
      dispatchEvent: function() { return true; },
      onchargingchange: null,
      onchargingtimechange: null,
      ondischargingtimechange: null,
      onlevelchange: null,
    });
  }

  // 14. Canvas fingerprint randomization — apply seeded noise over a band of
  //     pixels rather than a single pixel. Noise stays consistent within a
  //     session (so the site sees the same "device") but differs across
  //     sessions so the fingerprint can't be pinned globally.
  //     Also covers toBlob, getImageData, and measureText (commonly combined).
  function __applyCanvasNoise(ctx, w, h) {
    try {
      var bandH = Math.min(h, 3);
      var bandW = Math.min(w, 32);
      var img = ctx.getImageData(0, 0, bandW, bandH);
      var data = img.data;
      for (var i = 0; i < data.length; i += 4) {
        // Only perturb the low bit of R channel — imperceptible visually.
        data[i] = data[i] ^ ((__rand() * 2) | 0);
      }
      ctx.putImageData(img, 0, 0);
    } catch (e) {}
  }
  var __origToDataURL = HTMLCanvasElement.prototype.toDataURL;
  HTMLCanvasElement.prototype.toDataURL = function () {
    if (this.width > 16 && this.height > 16) {
      var ctx = this.getContext('2d');
      if (ctx) __applyCanvasNoise(ctx, this.width, this.height);
    }
    return __origToDataURL.apply(this, arguments);
  };
  var __origToBlob = HTMLCanvasElement.prototype.toBlob;
  if (__origToBlob) {
    HTMLCanvasElement.prototype.toBlob = function () {
      if (this.width > 16 && this.height > 16) {
        var ctx = this.getContext('2d');
        if (ctx) __applyCanvasNoise(ctx, this.width, this.height);
      }
      return __origToBlob.apply(this, arguments);
    };
  }
  var __origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
  CanvasRenderingContext2D.prototype.getImageData = function (x, y, w, h) {
    var img = __origGetImageData.call(this, x, y, w, h);
    // Randomize a few scattered pixels in the returned data — same-session stable.
    try {
      var data = img.data;
      var pick = Math.min(8, (data.length / 4) | 0);
      for (var k = 0; k < pick; k++) {
        var idx = ((__rand() * (data.length / 4)) | 0) * 4;
        data[idx] = data[idx] ^ 1;
      }
    } catch (e) {}
    return img;
  };

  // 15. AudioContext fingerprint noise — createDynamicsCompressor + oscillator
  //     output is hashed by sites. Perturb AudioBuffer samples on getChannelData.
  if (typeof AudioBuffer !== 'undefined') {
    var __origGetChannelData = AudioBuffer.prototype.getChannelData;
    AudioBuffer.prototype.getChannelData = function () {
      var data = __origGetChannelData.apply(this, arguments);
      try {
        // Apply a micro-perturbation proportional to the sample magnitude.
        for (var i = 0; i < data.length; i += 100) {
          data[i] = data[i] + (__rand() - 0.5) * 1e-7;
        }
      } catch (e) {}
      return data;
    };
  }
  if (typeof AnalyserNode !== 'undefined') {
    var __origGetFloatFreq = AnalyserNode.prototype.getFloatFrequencyData;
    AnalyserNode.prototype.getFloatFrequencyData = function (array) {
      __origGetFloatFreq.call(this, array);
      try {
        for (var i = 0; i < array.length; i += 50) {
          array[i] = array[i] + (__rand() - 0.5) * 0.1;
        }
      } catch (e) {}
    };
  }

  // 16. Consistent window.outerWidth/Height (headless mismatches inner/outer)
  Object.defineProperty(window, 'outerWidth', { get: () => window.innerWidth });
  Object.defineProperty(window, 'outerHeight', { get: () => window.innerHeight + 85 });

  // 17. WebRTC leak prevention — block RTCPeerConnection from revealing local IP.
  //     (Real browsers with VPNs/extensions exhibit the same behavior, so this
  //     is not itself a red flag.)
  if (typeof RTCPeerConnection !== 'undefined') {
    var __OrigRTC = RTCPeerConnection;
    function __PatchedRTC() {
      var pc = new __OrigRTC(arguments[0], arguments[1]);
      var origCreateOffer = pc.createOffer.bind(pc);
      pc.createOffer = function (opts) {
        opts = opts || {};
        opts.offerToReceiveAudio = false;
        opts.offerToReceiveVideo = false;
        return origCreateOffer(opts);
      };
      return pc;
    }
    __PatchedRTC.prototype = __OrigRTC.prototype;
    try { window.RTCPeerConnection = __PatchedRTC; } catch (e) {}
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
