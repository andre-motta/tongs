import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { app, BrowserWindow, nativeTheme, protocol, session } from "electron";
import { AssetCatalog } from "../../../desktop/dist/src/main/assets.js";
import { DesktopIpcController } from "../../../desktop/dist/src/main/ipc.js";
import { APP_DOCUMENT, isAllowedAppUrl } from "../../../desktop/dist/src/main/security.js";
import { SidecarTransport } from "../../../desktop/dist/src/main/sidecar.js";

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

const sourceRoot = path.resolve(
  process.env.TONGS_CI_PROOF_SOURCE ?? path.join(import.meta.dirname, "../../.."),
);
const evidenceRoot = path.resolve(
  process.env.TONGS_CI_PROOF_EVIDENCE ??
    path.join(sourceRoot, ".evidence", "issue-47"),
);
const pythonExecutable = path.join(sourceRoot, ".venv", "bin", "python");
const fixtureSidecar = path.join(
  sourceRoot,
  "tests",
  "desktop",
  "native",
  "ci_fixture_sidecar.py",
);
const desktopRoot = path.join(sourceRoot, "desktop");
const coreVersion = execFileSync(
  pythonExecutable,
  ["-c", "from importlib.metadata import version; print(version('tongs'))"],
  { encoding: "utf8" },
).trim();
const sourceCommit = process.env.TONGS_CI_PROOF_COMMIT ?? null;

let controller = null;
let transport = null;
let window = null;

const mark = (stage) => process.stderr.write(`[native-ci-proof] ${stage}\n`);

async function runProof() {
try {
  mark("prepare evidence");
  await mkdir(evidenceRoot, { recursive: true, mode: 0o700 });
  mark("wait for Electron");
  await app.whenReady();
  mark("configure session");
  const desktopSession = session.fromPartition(`tongs-ci-proof-${process.pid}`, {
    cache: false,
  });
  desktopSession.setPermissionRequestHandler(
    (_contents, _permission, callback) => callback(false),
  );
  desktopSession.setPermissionCheckHandler(() => false);
  desktopSession.webRequest.onBeforeRequest((details, callback) =>
    callback({ cancel: !isAllowedAppUrl(details.url) }),
  );

  const fixtureSpawn = (executable, _arguments, options) => {
    const child = spawn(executable, ["-E", "-P", fixtureSidecar], options);
    child.stderr.on("data", (chunk) => process.stderr.write(chunk));
    return child;
  };
  transport = new SidecarTransport(
    {
      pythonExecutable,
      coreVersion,
      safeCwd: evidenceRoot,
    },
    1_000,
    10_000,
    5_000,
    fixtureSpawn,
  );
  mark("start sidecar");
  await transport.start();
  mark("load assets");
  const assets = new AssetCatalog(
    transport,
    path.join(desktopRoot, "dist", "shell"),
  );
  await assets.refresh();
  desktopSession.protocol.handle("tongs", (request) => assets.response(request.url));

  window = new BrowserWindow({
    width: 1180,
    height: 780,
    show: true,
    backgroundColor: "#101418",
    webPreferences: {
      session: desktopSession,
      preload: path.join(desktopRoot, "dist", "src", "preload", "index.cjs"),
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      webviewTag: false,
      devTools: false,
      spellcheck: false,
    },
  });
  window.setMenu(null);
  window.webContents.on("console-message", (details) => {
    process.stderr.write(
      `[native-ci-proof] renderer ${details.level}: ${details.message}\n`,
    );
  });
  window.webContents.on(
    "did-fail-load",
    (_event, code, description, url) =>
      process.stderr.write(
        `[native-ci-proof] load failed ${code} ${description} ${url}\n`,
      ),
  );
  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  window.webContents.on("will-navigate", (event) => event.preventDefault());
  window.webContents.on("will-attach-webview", (event) => event.preventDefault());
  controller = new DesktopIpcController(window, transport, assets);
  controller.register();
  mark("load application");
  await window.loadURL(APP_DOCUMENT);
  mark(
    await window.webContents.executeJavaScript(
      '`loaded ${location.href} body=${document.body?.innerHTML?.slice(0, 200)}`',
      true,
    ),
  );

  mark("exercise pipeline hierarchy");
  const initial = await evaluate(`
    const repository = await waitFor("repository", () =>
      [...document.querySelectorAll(".nav-item")].find((item) =>
        item.textContent.includes("proof/desktop-ci")));
    repository.click();
    const review = await waitFor("review", () =>
      document.querySelector('.review-card[data-review-number="47"]'));
    review.click();
    const pipelines = await waitFor("pipelines tab", () =>
      document.querySelector('[data-panel="pipelines"]'));
    pipelines.click();
    await waitFor("pipeline", () =>
      document.querySelector(".ci-pipeline"));
    await waitFor("job", () =>
      document.querySelector(".ci-job"));
    await waitFor("log", () =>
      document.querySelector(".ci-log-lines"));
    const input = document.querySelector(".ci-log-search");
    input.focus();
    const setter = Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype, "value").set;
    setter.call(input, "search-target");
    input.dispatchEvent(new Event("input", { bubbles: true }));
    await waitFor("log search", () =>
      document.querySelector(".ci-match-count")?.textContent.includes("1 of 1"));
    return snapshot();
  `);
  const initialScreenshot = await capture("01-pipeline-log-dark.png");

  mark("exercise retry job");
  const retryJob = await performAction("Retry job", "CI action accepted");
  const knownScreenshot = await capture("02-retry-job-known.png");
  await clickButton("Dismiss");

  mark("exercise cancel job unknown outcome");
  const cancelJob = await performAction("Cancel job", "Remote outcome unknown");
  const unknownScreenshot = await capture("03-cancel-job-unknown.png");
  await clickButton("Acknowledge after review");

  mark("exercise retry pipeline rejection");
  const retryPipeline = await performAction("Retry pipeline", "CI action rejected");
  const rejectionScreenshot = await capture("04-retry-pipeline-rejected.png");
  await clickButton("Dismiss");

  mark("exercise cancel pipeline timeout and receipt reconciliation");
  const cancelPipeline = await performAction(
    "Cancel pipeline",
    "CI action needs reconciliation",
  );
  const timeoutScreenshot = await capture("05-cancel-pipeline-timeout.png");
  await new Promise((resolve) => setTimeout(resolve, 1_000));
  await clickButton("Check retained receipt");
  await evaluate(`
    await waitFor("retained known receipt", () =>
      document.body.innerText.includes("CI action accepted"));
    return snapshot();
  `);
  const reconciledScreenshot = await capture("06-cancel-pipeline-reconciled.png");

  nativeTheme.themeSource = "light";
  window.setSize(900, 700);
  await waitForFrames(3);
  const lightNarrowScreenshot = await capture("07-pipeline-light-narrow.png");
  const finalUi = await evaluate("return snapshot();");
  const actions = (await readFile(
    path.join(evidenceRoot, "mock-forge-actions.jsonl"),
    "utf8",
  ))
    .trim()
    .split("\n")
    .filter(Boolean)
    .map((line) => JSON.parse(line));
  const gpu = await app.getGPUInfo("complete");
  const report = {
    provenance:
      "Actual production renderer, preload, IPC controller, SidecarTransport, DesktopSidecarServer, and CIMutationService with isolated mocked forge writes",
    sourceCommit,
    sourceRoot,
    fixtureSidecar,
    evidenceRoot,
    coreVersion,
    electron: process.versions.electron,
    chromium: process.versions.chrome,
    node: process.versions.node,
    display: {
      sessionType: process.env.XDG_SESSION_TYPE ?? null,
      waylandDisplay: process.env.WAYLAND_DISPLAY ?? null,
      display: process.env.DISPLAY ?? null,
      ozonePlatform: "x11",
    },
    security: {
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      webviewTag: false,
      scheme: APP_DOCUMENT,
    },
    sessionGeneration: transport.sessionGeneration,
    initial,
    demonstrations: {
      retryJob,
      cancelJob,
      retryPipeline,
      cancelPipeline,
    },
    finalUi,
    mockForgeActions: actions,
    screenshots: [
      initialScreenshot,
      knownScreenshot,
      unknownScreenshot,
      rejectionScreenshot,
      timeoutScreenshot,
      reconciledScreenshot,
      lightNarrowScreenshot,
    ],
    gpuFeatureStatus: app.getGPUFeatureStatus(),
    gpu,
    processMetrics: app.getAppMetrics().map((item) => ({
      type: item.type,
      pid: item.pid,
      serviceName: item.serviceName,
      sandboxed: item.sandboxed,
    })),
    sidecarProcessHistory: transport.crashHistory,
  };
  await writeFile(
    path.join(evidenceRoot, "native-ci-proof.json"),
    `${JSON.stringify(report, null, 2)}\n`,
    { mode: 0o600 },
  );
  mark("proof complete");
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
} catch (error) {
  process.stderr.write(`${error instanceof Error ? error.stack : String(error)}\n`);
  process.exitCode = 1;
} finally {
  controller?.dispose();
  if (window && !window.isDestroyed()) window.destroy();
  if (transport) await transport.stop().catch(() => undefined);
  app.quit();
}
}

void runProof();

async function evaluate(body) {
  return window.webContents.executeJavaScript(
    `(async () => {
      const waitFor = async (label, select, timeout = 10000) => {
        const started = Date.now();
        while (Date.now() - started < timeout) {
          const result = select();
          if (result) return result;
          await new Promise((resolve) => setTimeout(resolve, 25));
        }
        throw new Error("Native CI proof timeout: " + label + ": " + document.body.innerText.slice(0, 1200));
      };
      const snapshot = () => ({
        title: document.querySelector(".view-title")?.textContent ?? null,
        pipeline: document.querySelector(".ci-pipeline.ci-selected")?.textContent ?? null,
        job: document.querySelector(".ci-job.ci-selected")?.textContent ?? null,
        logRows: [...document.querySelectorAll(".ci-log-lines code")].map((item) => item.textContent),
        matchCount: document.querySelector(".ci-match-count")?.textContent ?? null,
        mutation: document.querySelector(".ci-mutation-result")?.textContent ?? null,
        processGlobal: typeof globalThis.process,
        requireGlobal: typeof globalThis.require,
      });
      ${body}
    })()`,
    true,
  );
}

async function performAction(label, expected) {
  return evaluate(`
    const action = await waitFor(${JSON.stringify(label)}, () =>
      [...document.querySelectorAll("button")].find((item) =>
        item.textContent.trim() === ${JSON.stringify(label)} && !item.disabled));
    action.click();
    const confirmation = await waitFor("confirmation", () =>
      document.querySelector(".ci-confirm"));
    confirmation.click();
    confirmation.click();
    await waitFor(${JSON.stringify(expected)}, () =>
      document.body.innerText.includes(${JSON.stringify(expected)}), 15000);
    return snapshot();
  `);
}

async function clickButton(label) {
  await evaluate(`
    const button = await waitFor(${JSON.stringify(label)}, () =>
      [...document.querySelectorAll("button")].find((item) =>
        item.textContent.trim() === ${JSON.stringify(label)} && !item.disabled));
    button.click();
    return true;
  `);
}

async function capture(name) {
  await waitForFrames(2);
  const bytes = (await window.webContents.capturePage()).toPNG();
  const output = path.join(evidenceRoot, name);
  await writeFile(output, bytes, { mode: 0o600 });
  return {
    path: output,
    sha256: createHash("sha256").update(bytes).digest("hex"),
    byteCount: bytes.length,
  };
}

async function waitForFrames(count) {
  await window.webContents.executeJavaScript(
    `new Promise((resolve) => {
      let remaining = ${count};
      const next = () => {
        remaining -= 1;
        if (remaining <= 0) resolve();
        else requestAnimationFrame(next);
      };
      requestAnimationFrame(next);
    })`,
    true,
  );
}
