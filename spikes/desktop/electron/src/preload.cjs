const { contextBridge, ipcRenderer } = require("electron");

const api = Object.freeze({
  invoke(method, params = {}) {
    return ipcRenderer.invoke("tongs:invoke", method, params);
  },
});

contextBridge.exposeInMainWorld("tongs", api);
globalThis.addEventListener("DOMContentLoaded", () => {
  globalThis.dispatchEvent(new Event("tongs-ready"));
});
