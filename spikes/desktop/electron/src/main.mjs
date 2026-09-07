import { app, BrowserWindow, ipcMain, session } from "electron";
import { once } from "node:events";
import { writeFileSync } from "node:fs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import {
  assertRequiredHardwareGpu,
  evaluateGpuEvidence,
  finalizeGpuEvidence,
} from "./gpu.mjs";
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
  if (index >= 0) return process.argv[index + 1];
  const inline = process.argv.find((argument) => argument.startsWith(`${name}=`));
  return inline ? inline.slice(name.length + 1) : null;
}

function hasArgument(name) {
  return process.argv.includes(name);
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

async function readLinuxSandboxStatus(pid) {
  if (process.platform !== "linux" || !pid) return null;
  const fields = new Set([
    "Name",
    "Pid",
    "PPid",
    "TracerPid",
    "NoNewPrivs",
    "Seccomp",
    "Seccomp_filters",
  ]);
  const [status, commandLine] = await Promise.all([
    readFile(`/proc/${pid}/status`, "utf8"),
    readFile(`/proc/${pid}/cmdline`, "utf8"),
  ]);
  const result = Object.fromEntries(
    status
      .split("\n")
      .map((line) => line.split(":", 2).map((value) => value.trim()))
      .filter(([name]) => fields.has(name)),
  );
  result.switches = commandLine
    .split("\0")
    .filter((value) =>
      [
        "--type=",
        "--ozone-platform=",
        "--no-sandbox",
        "--disable-gpu-sandbox",
        "--disable-seccomp-filter-sandbox",
      ].some((prefix) => value.startsWith(prefix)),
    );
  return result;
}

async function collectGraphicsEvidence(window) {
  const webgl = await window.webContents.executeJavaScript(
    `(() => {
      const canvas = document.createElement("canvas");
      const context = canvas.getContext("webgl2") || canvas.getContext("webgl");
      if (!context) return { available: false, renderer: null, vendor: null };
      const extension = context.getExtension("WEBGL_debug_renderer_info");
      const result = {
        available: true,
        renderer: extension
          ? context.getParameter(extension.UNMASKED_RENDERER_WEBGL)
          : context.getParameter(context.RENDERER),
        vendor: extension
          ? context.getParameter(extension.UNMASKED_VENDOR_WEBGL)
          : context.getParameter(context.VENDOR),
        version: context.getParameter(context.VERSION),
      };
      context.getExtension("WEBGL_lose_context")?.loseContext();
      return result;
    })()`,
    true,
  );
  const gpuInfo = await app.getGPUInfo("complete");
  const processMetrics = app.getAppMetrics().map(({ pid, type, serviceName, name }) => ({
    pid,
    type,
    service_name: serviceName ?? null,
    name: name ?? null,
  }));
  const gpuProcess = processMetrics.find(({ type }) => type === "GPU") ?? null;
  const rendererPid = window.webContents.getOSProcessId();
  const [mainSandbox, gpuSandbox, rendererSandbox] = await Promise.all([
    readLinuxSandboxStatus(process.pid),
    readLinuxSandboxStatus(gpuProcess?.pid),
    readLinuxSandboxStatus(rendererPid),
  ]);
  const [gpuParentSandbox, rendererParentSandbox] = await Promise.all([
    readLinuxSandboxStatus(Number(gpuSandbox?.PPid)),
    readLinuxSandboxStatus(Number(rendererSandbox?.PPid)),
  ]);
  const rendererSecurity = await window.webContents.executeJavaScript(
    `({
      process_global: typeof process,
      require_global: typeof require,
      tongs_bridge: typeof window.tongs?.invoke,
      cross_origin_isolated: window.crossOriginIsolated,
    })`,
    true,
  );
  return {
    gpu: {
      hardware_acceleration_enabled: app.isHardwareAccelerationEnabled(),
      feature_status: app.getGPUFeatureStatus(),
      info: gpuInfo,
      webgl,
      process: gpuProcess,
      process_sandbox: gpuSandbox,
      parent_sandbox: gpuParentSandbox,
    },
    renderer_process: {
      pid: rendererPid,
      sandbox: rendererSandbox,
      parent_sandbox: rendererParentSandbox,
      security: rendererSecurity,
    },
    main_process: { pid: process.pid, sandbox: mainSandbox },
    process_metrics: processMetrics,
  };
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
  const graphicsInitial = await collectGraphicsEvidence(window);
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
  const report = {
    native_window: true,
    fixture: health.result.fixture === true,
    shell: "electron",
    platform: process.platform,
    arch: process.arch,
    session_type: process.env.XDG_SESSION_TYPE ?? null,
    desktop: process.env.XDG_CURRENT_DESKTOP ?? null,
    ozone_platform: app.commandLine.getSwitchValue("ozone-platform") || null,
    disable_gpu: app.commandLine.hasSwitch("disable-gpu"),
    disable_vulkan: app.commandLine.hasSwitch("disable-vulkan"),
    no_sandbox: app.commandLine.hasSwitch("no-sandbox"),
    disable_gpu_sandbox: app.commandLine.hasSwitch("disable-gpu-sandbox"),
    disable_seccomp_filter_sandbox: app.commandLine.hasSwitch(
      "disable-seccomp-filter-sandbox",
    ),
    enable_gpu_sandbox: app.commandLine.hasSwitch("enable-gpu-sandbox"),
    gpu_sandbox_start_early: app.commandLine.hasSwitch("gpu-sandbox-start-early"),
    use_angle: app.commandLine.getSwitchValue("use-angle") || null,
    use_gl: app.commandLine.getSwitchValue("use-gl") || null,
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
    graphics_initial: {
      ...graphicsInitial,
      child_process_failures: childProcessFailures.map((failure) => ({ ...failure })),
    },
    web_preferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webviewTag: false,
    },
  };
  report.graphics_initial.gpu.acceptance = evaluateGpuEvidence({
    ...report,
    gpu: report.graphics_initial.gpu,
    renderer_process: report.graphics_initial.renderer_process,
    child_process_failures: report.graphics_initial.child_process_failures,
  });
  return report;
}

let sidecar;
let window;
let quitting = false;
const childProcessFailures = [];

app.on("child-process-gone", (_event, details) => {
  childProcessFailures.push({
    type: details.type,
    reason: details.reason,
    exit_code: details.exitCode,
    service_name: details.serviceName ?? null,
    name: details.name ?? null,
  });
});

async function injectGpuFailureForTest() {
  if (process.env.TONGS_DESKTOP_TEST_KILL_GPU_AFTER_PROBE !== "1") return;
  const gpuProcess = app.getAppMetrics().find(({ type }) => type === "GPU");
  if (!gpuProcess) throw new Error("GPU failure injection found no GPU process");
  const failureObserved = once(app, "child-process-gone");
  process.kill(gpuProcess.pid, "SIGKILL");
  await Promise.race([
    failureObserved,
    new Promise((_, reject) =>
      setTimeout(() => reject(new Error("GPU failure injection was not observed")), 5_000),
    ),
  ]);
}

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
    await injectGpuFailureForTest();
    await mkdir(path.dirname(path.resolve(smokeReport)), { recursive: true });
    const graphicsFinal = await collectGraphicsEvidence(window);
    finalizeGpuEvidence(report, graphicsFinal, childProcessFailures);
    writeFileSync(path.resolve(smokeReport), `${JSON.stringify(report, null, 2)}\n`);
    assertRequiredHardwareGpu(report, hasArgument("--require-hardware-gpu"));
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
