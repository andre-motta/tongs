import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { app, BrowserWindow, protocol, session } from "electron";
import { AssetCatalog } from "./assets.js";
import { DesktopIpcController } from "./ipc.js";
import { parseLaunchArguments } from "./launch.js";
import { APP_DOCUMENT, isAllowedAppUrl } from "./security.js";
import { recoverRendererSession } from "./renderer_lifecycle.js";
import { SidecarTransport } from "./sidecar.js";

protocol.registerSchemesAsPrivileged([
  {
    scheme: "tongs",
    privileges: {
      standard: true,
      secure: true,
      supportFetchAPI: false,
      bypassCSP: false,
      allowServiceWorkers: false,
      codeCache: true,
    },
  },
]);
const forbidden = [
  "disable-gpu",
  "disable-gpu-sandbox",
  "no-sandbox",
  "disable-setuid-sandbox",
];
if (forbidden.some((name) => app.commandLine.hasSwitch(name))) {
  throw new Error("Unsafe Electron process switches are forbidden");
}
if (
  process.platform === "linux" &&
  process.env.WAYLAND_DISPLAY &&
  !usesX11(process.argv)
) {
  throw new Error(
    "Fedora KDE Wayland sessions must launch the desktop through XWayland",
  );
}

const launch = parseLaunchArguments(process.argv);
const smokePath = argumentValue(process.argv, "--tongs-smoke-report");
const desktopRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../..",
);
let window: BrowserWindow | null = null;
let transport: SidecarTransport | null = null;
let controller: DesktopIpcController | null = null;
let assetCatalog: AssetCatalog | null = null;
let rendererRestart: Promise<void> | null = null;
const childProcessFailures: object[] = [];

async function run(): Promise<void> {
  const desktopSession = session.fromPartition("tongs-production", {
    cache: false,
  });
  desktopSession.setPermissionRequestHandler(
    (_contents, _permission, callback) => callback(false),
  );
  desktopSession.setPermissionCheckHandler(() => false);
  desktopSession.webRequest.onBeforeRequest((details, callback) =>
    callback({ cancel: !isAllowedAppUrl(details.url) }),
  );
  transport = new SidecarTransport(launch);
  await transport.start();
  const assets = new AssetCatalog(transport, path.join(desktopRoot, "shell"));
  assetCatalog = assets;
  await assets.refresh();
  desktopSession.protocol.handle("tongs", (request) =>
    assets.response(request.url),
  );
  window = new BrowserWindow({
    width: 1180,
    height: 780,
    show: false,
    backgroundColor: "#101418",
    icon: path.join(desktopRoot, "icon.png"),
    webPreferences: {
      session: desktopSession,
      preload: path.join(desktopRoot, "src/preload/index.cjs"),
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      webviewTag: false,
      devTools: false,
      spellcheck: false,
    },
  });
  controller = new DesktopIpcController(window, transport, assets);
  controller.register();
  window.setMenu(null);
  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  window.webContents.on("will-navigate", (event, url) => {
    event.preventDefault();
    if (url === APP_DOCUMENT) void restartRendererSession();
  });
  window.webContents.on("will-attach-webview", (event) =>
    event.preventDefault(),
  );
  window.webContents.on("render-process-gone", () => {
    void restartRendererSession();
  });
  app.on("child-process-gone", (_event, details) => {
    childProcessFailures.push({
      type: details.type,
      reason: details.reason,
      exitCode: details.exitCode,
      serviceName: details.serviceName,
    });
  });
  window.once("ready-to-show", () => window?.show());
  window.on("closed", () => {
    controller?.dispose();
    controller = null;
    window = null;
  });
  await window.loadURL(APP_DOCUMENT);
  if (smokePath) await captureSmokeReport(smokePath);
}

async function captureSmokeReport(outputPath: string): Promise<void> {
  if (!window || !transport) throw new Error("Desktop smoke started too early");
  const reload = await reloadRendererSession();
  const rendererCrashRecovery = await crashRendererSession();
  const rendererProbe = rendererCrashRecovery.finalRendererProbe;
  const gpu = await app.getGPUInfo("complete");
  const metrics = await Promise.all(
    app.getAppMetrics().map(async (item) => ({
      type: item.type,
      pid: item.pid,
      serviceName: item.serviceName,
      sandboxed: item.sandboxed,
      linuxSandbox: await linuxSandbox(item.pid),
    })),
  );
  const sidecarLinuxSandbox = transport.processId
    ? await linuxSandbox(transport.processId)
    : null;
  const screenshotPath = outputPath.endsWith(".json")
    ? `${outputPath.slice(0, -5)}.png`
    : `${outputPath}.png`;
  await writeFile(
    screenshotPath,
    (await window.webContents.capturePage()).toPNG(),
    { mode: 0o600 },
  );
  const report = {
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node,
    sidecarPid: transport.processId,
    sidecarLinuxSandbox,
    sessionGeneration: transport.sessionGeneration,
    reload,
    rendererCrashRecovery,
    rendererProbe,
    gpu,
    gpuFeatureStatus: app.getGPUFeatureStatus(),
    metrics,
    sidecarProcessHistory: transport.crashHistory,
    childProcessFailures,
    security: {
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      webviewTag: false,
      scheme: APP_DOCUMENT,
      xwayland: usesX11(process.argv),
    },
  };
  await writeFile(outputPath, `${JSON.stringify(report, null, 2)}\n`, {
    mode: 0o600,
  });
  setTimeout(() => app.quit(), 500);
}

async function crashRendererSession(): Promise<{
  initialSessionGeneration: number;
  finalSessionGeneration: number;
  finalRendererProbe: object;
}> {
  if (!window || !transport) throw new Error("Desktop crash recovery started too early");
  const owner = window;
  const initialSessionGeneration = transport.sessionGeneration;
  const completed = waitForNewRendererLoad(owner, initialSessionGeneration);
  owner.webContents.forcefullyCrashRenderer();
  await completed;
  return {
    initialSessionGeneration,
    finalSessionGeneration: transport.sessionGeneration,
    finalRendererProbe: await rendererProbe(),
  };
}

async function rendererProbe(): Promise<object> {
  if (!window) throw new Error("Desktop renderer is unavailable");
  return (await window.webContents.executeJavaScript(
    "Promise.race([new Promise((resolve) => { const poll = () => document.documentElement.dataset.tongsProbe ? resolve(JSON.parse(document.documentElement.dataset.tongsProbe)) : setTimeout(poll, 20); poll(); }), new Promise((_, reject) => setTimeout(() => reject(new Error('renderer probe timeout')), 5000))])",
    true,
  )) as object;
}

async function reloadRendererSession(): Promise<{
  initialSessionGeneration: number;
  finalSessionGeneration: number;
  initialRendererProbe: object;
  finalRendererProbe: object;
}> {
  if (!window || !transport) throw new Error("Desktop reload started too early");
  const owner = window;
  const initialSessionGeneration = transport.sessionGeneration;
  const initialRendererProbe = await rendererProbe();
  const completed = waitForNewRendererLoad(owner, initialSessionGeneration);
  void owner.webContents.executeJavaScript("location.reload()").catch(() => {
    // The expected document teardown can reject the initiating renderer promise.
  });
  await completed;
  return {
    initialSessionGeneration,
    finalSessionGeneration: transport.sessionGeneration,
    initialRendererProbe,
    finalRendererProbe: await rendererProbe(),
  };
}

function waitForNewRendererLoad(
  owner: BrowserWindow,
  initialSessionGeneration: number,
): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => {
      owner.webContents.off("did-finish-load", onLoad);
      reject(new Error("renderer reload timeout"));
    }, 10_000);
    const onLoad = (): void => {
      if (!transport || transport.sessionGeneration <= initialSessionGeneration) {
        return;
      }
      clearTimeout(timer);
      owner.webContents.off("did-finish-load", onLoad);
      resolve();
    };
    owner.webContents.on("did-finish-load", onLoad);
  });
}

async function restartRendererSession(): Promise<void> {
  if (rendererRestart) return rendererRestart;
  if (!window || !transport) throw new Error("Desktop reload started too early");
  const owner = window;
  const sidecar = transport;
  const assets = assetCatalog;
  if (!assets) throw new Error("Desktop assets are unavailable");
  rendererRestart = (async () => {
    await recoverRendererSession({
      resetBindings: () => controller?.reset(),
      restartSidecar: () => sidecar.restart(),
      refreshAssets: () => assets.refresh(),
      loadDocument: async () => {
        if (window === owner && !owner.isDestroyed()) await owner.loadURL(APP_DOCUMENT);
      },
      showFailure: async () => {
        if (window !== owner || owner.isDestroyed()) return;
        await owner.webContents.executeJavaScript(
          "document.querySelector('#status').textContent = 'The local Tongs service is unavailable.'",
        );
      },
    });
  })();
  try {
    await rendererRestart;
  } finally {
    rendererRestart = null;
  }
}

app
  .whenReady()
  .then(run)
  .catch((error: unknown) => {
    console.error(error instanceof Error ? error.message : "Desktop startup failed");
    app.exit(1);
  });
app.on("before-quit", () => {
  controller?.dispose();
});
app.on("window-all-closed", () => app.quit());
app.on("will-quit", (event) => {
  if (!transport) return;
  event.preventDefault();
  const owned = transport;
  transport = null;
  void owned.stop().finally(() => app.exit(0));
});

function usesX11(argv: readonly string[]): boolean {
  return argv.some(
    (item, index) =>
      item === "--ozone-platform=x11" ||
      (item === "--ozone-platform" && argv[index + 1] === "x11"),
  );
}

function argumentValue(argv: readonly string[], name: string): string | null {
  const joined = argv.find((item) => item.startsWith(`${name}=`));
  if (joined) return joined.slice(name.length + 1);
  const index = argv.indexOf(name);
  return index >= 0 ? (argv[index + 1] ?? null) : null;
}

async function linuxSandbox(pid: number): Promise<object | null> {
  if (process.platform !== "linux") return null;
  try {
    const value = await readFile(`/proc/${pid}/status`, "utf8");
    const field = (name: string): number | null => {
      const match = new RegExp(`^${name}:\\s+(\\d+)`, "m").exec(value);
      return match ? Number(match[1]) : null;
    };
    return {
      noNewPrivs: field("NoNewPrivs"),
      seccomp: field("Seccomp"),
      seccompFilters: field("Seccomp_filters"),
    };
  } catch {
    return null;
  }
}
