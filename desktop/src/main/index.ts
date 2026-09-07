import path from "node:path";
import { readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { app, BrowserWindow, protocol, session } from "electron";
import { AssetCatalog } from "./assets.js";
import { DesktopIpcController } from "./ipc.js";
import { parseLaunchArguments } from "./launch.js";
import { APP_DOCUMENT, isAllowedAppUrl } from "./security.js";
import { SidecarTransport } from "./sidecar.js";

protocol.registerSchemesAsPrivileged([{ scheme: "tongs", privileges: { standard: true, secure: true, supportFetchAPI: false, bypassCSP: false, allowServiceWorkers: false, codeCache: true } }]);
const forbidden = ["disable-gpu", "disable-gpu-sandbox", "no-sandbox", "disable-setuid-sandbox"];
if (forbidden.some((name) => app.commandLine.hasSwitch(name))) throw new Error("Unsafe Electron process switches are forbidden");
if (process.platform === "linux" && process.env.WAYLAND_DISPLAY && !usesX11(process.argv)) throw new Error("Fedora KDE Wayland sessions must launch the desktop through XWayland");

const launch = parseLaunchArguments(process.argv);
const smokePath = argumentValue(process.argv, "--tongs-smoke-report");
const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
let window: BrowserWindow | null = null;
let transport: SidecarTransport | null = null;
let controller: DesktopIpcController | null = null;
const childProcessFailures: object[] = [];

async function run(): Promise<void> {
  const desktopSession = session.fromPartition("tongs-production", { cache: false });
  desktopSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  desktopSession.setPermissionCheckHandler(() => false);
  desktopSession.webRequest.onBeforeRequest((details, callback) => callback({ cancel: !isAllowedAppUrl(details.url) }));
  transport = new SidecarTransport(launch);
  await transport.start();
  const assets = new AssetCatalog(transport, path.join(desktopRoot, "shell"));
  await assets.refresh();
  desktopSession.protocol.handle("tongs", (request) => assets.response(request.url));
  window = new BrowserWindow({
    width: 1180, height: 780, show: false, backgroundColor: "#101418", icon: path.join(desktopRoot, "icon.png"),
    webPreferences: { session: desktopSession, preload: path.join(desktopRoot, "src/preload/index.cjs"), sandbox: true, contextIsolation: true, nodeIntegration: false, webviewTag: false, devTools: false, spellcheck: false },
  });
  controller = new DesktopIpcController(window, transport, assets); controller.register(); window.setMenu(null);
  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  window.webContents.on("will-navigate", (event, url) => { if (url !== APP_DOCUMENT) event.preventDefault(); });
  window.webContents.on("will-attach-webview", (event) => event.preventDefault());
  window.webContents.on("render-process-gone", () => { controller?.reset(); void transport?.restart(); });
  app.on("child-process-gone", (_event, details) => { childProcessFailures.push({ type: details.type, reason: details.reason, exitCode: details.exitCode, serviceName: details.serviceName }); });
  let firstLoadComplete = false; let restartingForReload = false;
  window.webContents.on("did-start-loading", () => {
    if (!firstLoadComplete || restartingForReload || !window || !transport) return;
    restartingForReload = true; window.webContents.stop(); controller?.reset();
    void transport.restart().then(() => window?.loadURL(APP_DOCUMENT)).finally(() => { restartingForReload = false; });
  });
  window.webContents.on("did-finish-load", () => { firstLoadComplete = true; });
  window.once("ready-to-show", () => window?.show());
  window.on("closed", () => { controller?.dispose(); controller = null; window = null; });
  await window.loadURL(APP_DOCUMENT);
  if (smokePath) {
    const rendererProbe = await window.webContents.executeJavaScript("Promise.race([new Promise((resolve) => { const poll = () => document.documentElement.dataset.tongsProbe ? resolve(JSON.parse(document.documentElement.dataset.tongsProbe)) : setTimeout(poll, 20); poll(); }), new Promise((_, reject) => setTimeout(() => reject(new Error('renderer probe timeout')), 5000))])", true) as object;
    const gpu = await app.getGPUInfo("complete");
    const metrics = await Promise.all(app.getAppMetrics().map(async (item) => ({ type: item.type, pid: item.pid, serviceName: item.serviceName, sandboxed: item.sandboxed, linuxSandbox: await linuxSandbox(item.pid) })));
    const sidecarLinuxSandbox = transport.processId ? await linuxSandbox(transport.processId) : null;
    const screenshotPath = smokePath.endsWith(".json") ? `${smokePath.slice(0, -5)}.png` : `${smokePath}.png`;
    await writeFile(screenshotPath, (await window.webContents.capturePage()).toPNG(), { mode: 0o600 });
    await writeFile(smokePath, `${JSON.stringify({ electron: process.versions.electron, chrome: process.versions.chrome, node: process.versions.node, sidecarPid: transport.processId, sidecarLinuxSandbox, sessionGeneration: transport.sessionGeneration, rendererProbe, gpu, gpuFeatureStatus: app.getGPUFeatureStatus(), metrics, childProcessFailures, security: { sandbox: true, contextIsolation: true, nodeIntegration: false, webviewTag: false, scheme: APP_DOCUMENT, xwayland: usesX11(process.argv) } }, null, 2)}\n`, { mode: 0o600 });
    setTimeout(() => app.quit(), 500);
  }
}

app.whenReady().then(run).catch((error: unknown) => { console.error(error instanceof Error ? error.message : "Desktop startup failed"); app.exit(1); });
app.on("before-quit", () => { controller?.dispose(); });
app.on("window-all-closed", () => app.quit());
app.on("will-quit", (event) => {
  if (!transport) return; event.preventDefault(); const owned = transport; transport = null;
  void owned.stop().finally(() => app.exit(0));
});

function usesX11(argv: readonly string[]): boolean { return argv.some((item, index) => item === "--ozone-platform=x11" || (item === "--ozone-platform" && argv[index + 1] === "x11")); }
function argumentValue(argv: readonly string[], name: string): string | null { const joined = argv.find((item) => item.startsWith(`${name}=`)); if (joined) return joined.slice(name.length + 1); const index = argv.indexOf(name); return index >= 0 ? argv[index + 1] ?? null : null; }
async function linuxSandbox(pid: number): Promise<object | null> {
  if (process.platform !== "linux") return null;
  try { const text = await readFile(`/proc/${pid}/status`, "utf8"); const field = (name: string): number | null => { const match = new RegExp(`^${name}:\\s+(\\d+)`, "m").exec(text); return match ? Number(match[1]) : null; }; return { noNewPrivs: field("NoNewPrivs"), seccomp: field("Seccomp"), seccompFilters: field("Seccomp_filters") }; }
  catch { return null; }
}
