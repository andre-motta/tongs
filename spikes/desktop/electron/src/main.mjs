import { app, BrowserWindow, ipcMain, session } from "electron";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { Sidecar } from "./sidecar.mjs";
import {
  CONTENT_SECURITY_POLICY,
  RPC_CHANNEL,
  isAllowedNavigation,
  validateInvocation,
  validateRenderer,
} from "./security.mjs";

const sourceDir = path.dirname(fileURLToPath(import.meta.url));
const appDir = path.dirname(sourceDir);

function argumentValue(name) {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : null;
}

function firstExisting(candidates) {
  const fs = process.getBuiltinModule("node:fs");
  return candidates.find((candidate) => fs.existsSync(candidate));
}

function resolveRuntimePaths() {
  const frontend =
    argumentValue("--frontend") ||
    process.env.TONGS_DESKTOP_FRONTEND ||
    firstExisting([
      path.join(appDir, "frontend"),
      path.resolve(appDir, "..", "frontend", "dist"),
    ]);
  const backend =
    process.env.TONGS_DESKTOP_BACKEND ||
    firstExisting([path.join(appDir, "python"), path.resolve(appDir, "..")]);
  if (!frontend) throw new Error("Shared frontend assets were not found");
  if (!backend) throw new Error("Python backend package was not found");
  return {
    frontend: path.resolve(frontend),
    backend: path.resolve(backend),
    python: process.env.TONGS_DESKTOP_PYTHON || "python3",
  };
}

function installSessionPolicy(assetOrigin) {
  const desktopSession = session.fromPartition("tongs-prototype");
  desktopSession.setPermissionCheckHandler(() => false);
  desktopSession.setPermissionRequestHandler((_webContents, _permission, callback) => {
    callback(false);
  });
  desktopSession.webRequest.onHeadersReceived((details, callback) => {
    let responseOrigin;
    try {
      responseOrigin = new URL(details.url).origin;
    } catch {
      callback({ cancel: true });
      return;
    }
    if (responseOrigin !== assetOrigin) {
      callback({ cancel: true });
      return;
    }
    const headers = details.responseHeaders ?? {};
    headers["Content-Security-Policy"] = [CONTENT_SECURITY_POLICY];
    headers["Cross-Origin-Opener-Policy"] = ["same-origin"];
    headers["X-Content-Type-Options"] = ["nosniff"];
    callback({ responseHeaders: headers });
  });
  return desktopSession;
}

async function collectSmokeEvidence(window, sidecar, startupMs) {
  const call = async (method, params) => {
    const started = performance.now();
    const result = await window.webContents.executeJavaScript(
      `window.tongs.invoke(${JSON.stringify(method)}, ${JSON.stringify(params ?? {})})`,
      true,
    );
    return { elapsed_ms: Math.round((performance.now() - started) * 10) / 10, result };
  };
  const health = await call("health");
  const reviews = await call("list_reviews");
  const large = await call("get_diff", { id: "large" });
  const plugins = await call("list_plugins");
  const readyPlugin = plugins.result.find((plugin) => plugin.status === "ready");
  let plugin = null;
  let help = null;
  if (readyPlugin?.modules?.[0]) {
    plugin = await call("plugin_invoke", {
      plugin: readyPlugin.id,
      method: "echo",
      params: { text: "Electron native smoke" },
    });
    help = await call("plugin_help", {
      plugin: readyPlugin.id,
      module: readyPlugin.modules[0].id,
    });
  }
  return {
    native_window: true,
    fixture: health.result.fixture === true,
    shell: "electron",
    platform: process.platform,
    arch: process.arch,
    session_type: process.env.XDG_SESSION_TYPE ?? null,
    desktop: process.env.XDG_CURRENT_DESKTOP ?? null,
    ozone_platform: app.commandLine.getSwitchValue("ozone-platform") || null,
    disable_gpu: app.commandLine.hasSwitch("disable-gpu"),
    electron: process.versions.electron,
    chromium: process.versions.chrome,
    embedded_node: process.versions.node,
    python_executable: sidecar.python,
    packaged: app.isPackaged,
    python_pid: sidecar.child?.pid ?? null,
    electron_pid: process.pid,
    startup_ms: startupMs,
    health,
    reviews: { elapsed_ms: reviews.elapsed_ms, count: reviews.result.length },
    large_diff: { elapsed_ms: large.elapsed_ms, lines: large.result.lines.length },
    plugins: { elapsed_ms: plugins.elapsed_ms, records: plugins.result },
    plugin_call: plugin,
    plugin_help: help
      ? { elapsed_ms: help.elapsed_ms, bundled_documentation: help.result.includes("bundled") }
      : null,
    web_preferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webviewTag: false,
    },
  };
}

let sidecar;
let window;
let quitting = false;

async function shutdown() {
  if (quitting) return;
  quitting = true;
  ipcMain.removeHandler(RPC_CHANNEL);
  await sidecar?.stop();
}

async function run() {
  const launchStarted = performance.now();
  const runtime = resolveRuntimePaths();
  sidecar = new Sidecar({
    python: runtime.python,
    frontendDir: runtime.frontend,
    backendDir: runtime.backend,
  });
  const assetUrl = await sidecar.start();
  const assetOrigin = new URL(assetUrl).origin;
  const desktopSession = installSessionPolicy(assetOrigin);
  const icon = path.join(appDir, "assets", "icon.png");

  window = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 960,
    minHeight: 600,
    show: false,
    backgroundColor: "#111827",
    icon,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
      webviewTag: false,
      preload: path.join(sourceDir, "preload.cjs"),
      session: desktopSession,
    },
  });

  ipcMain.handle(RPC_CHANNEL, async (event, method, params) => {
    validateRenderer(event, window.webContents, assetOrigin);
    const invocation = validateInvocation(method, params);
    return sidecar.request(invocation.method, invocation.params);
  });

  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  window.webContents.on("will-attach-webview", (event) => event.preventDefault());
  window.webContents.on("will-navigate", (event, target) => {
    if (!isAllowedNavigation(target, assetOrigin)) event.preventDefault();
  });
  window.webContents.on("render-process-gone", async (_event, details) => {
    process.stderr.write(`Renderer exited: ${details.reason}\n`);
    await shutdown();
    app.exit(1);
  });
  sidecar.once("crash", async () => {
    if (quitting) return;
    await shutdown();
    app.exit(1);
  });

  await window.loadURL(assetUrl);
  window.show();

  const smokeReport = argumentValue("--smoke-report");
  if (smokeReport) {
    await new Promise((resolve) => setTimeout(resolve, 750));
    const report = await collectSmokeEvidence(
      window,
      sidecar,
      Math.round((performance.now() - launchStarted) * 10) / 10,
    );
    const uiProbe = argumentValue("--ui-probe");
    if (uiProbe) {
      const probeSource = await readFile(path.resolve(uiProbe), "utf8");
      const probeStarted = performance.now();
      report.ui_probe = {
        elapsed_ms: null,
        result: await window.webContents.executeJavaScript(
          `Promise.race([Promise.resolve((${probeSource})), new Promise((_, reject) => setTimeout(() => reject(new Error("UI probe timed out")), 12000))])`,
          true,
        ),
      };
      report.ui_probe.elapsed_ms =
        Math.round((performance.now() - probeStarted) * 10) / 10;
    }
    const screenshot = argumentValue("--screenshot");
    if (screenshot) {
      await mkdir(path.dirname(path.resolve(screenshot)), { recursive: true });
      const image = await window.webContents.capturePage();
      await writeFile(path.resolve(screenshot), image.toPNG());
      report.screenshot = path.resolve(screenshot);
    }
    await mkdir(path.dirname(path.resolve(smokeReport)), { recursive: true });
    await writeFile(path.resolve(smokeReport), `${JSON.stringify(report, null, 2)}\n`);
    await shutdown();
    app.quit();
  }
}

app.on("window-all-closed", () => app.quit());
app.on("before-quit", (event) => {
  if (!quitting && sidecar?.child) {
    event.preventDefault();
    shutdown().finally(() => app.quit());
  }
});

app.setName("Tongs Desktop Prototype");
app.whenReady().then(run).catch(async (error) => {
  process.stderr.write(`Tongs Electron failed: ${error.message}\n`);
  await shutdown();
  app.exit(1);
});
