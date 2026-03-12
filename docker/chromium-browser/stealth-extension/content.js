// === Stealth patches for Cloudflare anti-bot bypass ===

// Override navigator.webdriver
Object.defineProperty(navigator, 'webdriver', {
  get: () => false,
  configurable: true,
});

// Ensure navigator.languages is realistic
Object.defineProperty(navigator, 'languages', {
  get: () => ['en-US', 'en'],
  configurable: true,
});

Object.defineProperty(navigator, 'language', {
  get: () => 'en-US',
  configurable: true,
});

// Inject realistic navigator.plugins
Object.defineProperty(navigator, 'plugins', {
  get: () => {
    const pluginData = [
      { name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
      { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: '' },
      { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: '' },
      { name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer', description: '' },
      { name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer', description: '' },
    ];
    const plugins = Object.create(PluginArray.prototype);
    let i = 0;
    for (const p of pluginData) {
      const plugin = Object.create(Plugin.prototype, {
        name: { value: p.name, enumerable: true },
        filename: { value: p.filename, enumerable: true },
        description: { value: p.description, enumerable: true },
        length: { value: 1, enumerable: true },
      });
      Object.defineProperty(plugins, i, { value: plugin, enumerable: true });
      Object.defineProperty(plugins, p.name, { value: plugin, enumerable: false });
      i++;
    }
    Object.defineProperty(plugins, 'length', { value: pluginData.length, enumerable: true });
    plugins.refresh = () => {};
    plugins.item = (idx) => plugins[idx] || null;
    plugins.namedItem = (name) => plugins[name] || null;
    return plugins;
  },
  configurable: true,
});

// Ensure window.chrome exists with realistic runtime object
if (!window.chrome) {
  window.chrome = {};
}
if (!window.chrome.runtime) {
  window.chrome.runtime = {
    OnInstalledReason: {
      CHROME_UPDATE: 'chrome_update',
      INSTALL: 'install',
      SHARED_MODULE_UPDATE: 'shared_module_update',
      UPDATE: 'update',
    },
    OnRestartRequiredReason: {
      APP_UPDATE: 'app_update',
      OS_UPDATE: 'os_update',
      PERIODIC: 'periodic',
    },
    PlatformArch: {
      ARM: 'arm',
      ARM64: 'arm64',
      MIPS: 'mips',
      MIPS64: 'mips64',
      X86_32: 'x86-32',
      X86_64: 'x86-64',
    },
    PlatformNaclArch: {
      ARM: 'arm',
      MIPS: 'mips',
      MIPS64: 'mips64',
      X86_32: 'x86-32',
      X86_64: 'x86-64',
    },
    PlatformOs: {
      ANDROID: 'android',
      CROS: 'cros',
      LINUX: 'linux',
      MAC: 'mac',
      OPENBSD: 'openbsd',
      WIN: 'win',
    },
    RequestUpdateCheckStatus: {
      NO_UPDATE: 'no_update',
      THROTTLED: 'throttled',
      UPDATE_AVAILABLE: 'update_available',
    },
    connect: function () {},
    sendMessage: function () {},
  };
}

// Spoof WebGL vendor and renderer
const getParameterProxyHandler = {
  apply: function (target, thisArg, args) {
    const param = args[0];
    const gl = thisArg;
    // UNMASKED_VENDOR_WEBGL
    if (param === 0x9245) return 'Google Inc. (Intel)';
    // UNMASKED_RENDERER_WEBGL
    if (param === 0x9246) return 'ANGLE (Intel, Mesa Intel(R) UHD Graphics 630, OpenGL 4.6)';
    return Reflect.apply(target, thisArg, args);
  },
};

const origGetParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = new Proxy(origGetParameter, getParameterProxyHandler);

if (typeof WebGL2RenderingContext !== 'undefined') {
  const origGetParameter2 = WebGL2RenderingContext.prototype.getParameter;
  WebGL2RenderingContext.prototype.getParameter = new Proxy(origGetParameter2, getParameterProxyHandler);
}

// Clean up chromedriver artifacts (cdc_ properties)
for (const prop in window) {
  if (/^cdc_/.test(prop)) {
    delete window[prop];
  }
}

// Patch Permissions API: make 'notifications' return 'prompt' instead of 'denied'
if (typeof Permissions !== 'undefined' && Permissions.prototype.query) {
  const origQuery = Permissions.prototype.query;
  Permissions.prototype.query = function (parameters) {
    return parameters && parameters.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission === 'denied' ? 'prompt' : Notification.permission, onchange: null })
      : origQuery.call(this, parameters);
  };
}
