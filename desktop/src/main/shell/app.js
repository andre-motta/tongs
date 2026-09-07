(async () => {
  const status = document.querySelector("#status");
  try {
    const read = window.tongs.listAssets(); const assets = await read.result;
    const canvas = document.createElement("canvas"); const gl = canvas.getContext("webgl");
    const debug = gl?.getExtension("WEBGL_debug_renderer_info");
    const probe = { assets: assets.length, processGlobal: typeof globalThis.process, requireGlobal: typeof globalThis.require,
      webglVendor: gl && debug ? gl.getParameter(debug.UNMASKED_VENDOR_WEBGL) : null,
      webglRenderer: gl && debug ? gl.getParameter(debug.UNMASKED_RENDERER_WEBGL) : null };
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    document.documentElement.dataset.tongsProbe = JSON.stringify(probe);
    status.textContent = `Local service ready. ${assets.length} validated extension asset(s).`;
  } catch { status.textContent = "The local Tongs service is unavailable."; }
})();
