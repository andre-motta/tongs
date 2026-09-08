import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { app, BrowserWindow, clipboard, protocol, session } from "electron";
import { AssetCatalog } from "../../../desktop/dist/src/main/assets.js";
import { DesktopIpcController } from "../../../desktop/dist/src/main/ipc.js";
import { APP_DOCUMENT, isAllowedAppUrl } from "../../../desktop/dist/src/main/security.js";
import { SidecarTransport } from "../../../desktop/dist/src/main/sidecar.js";
import { WorkspaceUtilities } from "../../../desktop/dist/src/main/utilities.js";

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
  process.env.TONGS_UTILITY_PROOF_SOURCE ?? path.join(import.meta.dirname, "../../.."),
);
const evidenceRoot = path.resolve(
  process.env.TONGS_UTILITY_PROOF_EVIDENCE ??
    path.join(sourceRoot, ".evidence", "issue-100"),
);
const pythonExecutable = path.join(sourceRoot, ".venv", "bin", "python");
const fixtureSidecar = path.join(sourceRoot, "tests", "desktop", "native", "ci_fixture_sidecar.py");
const editorFixture = path.join(sourceRoot, "tests", "desktop", "native", "editor_fixture.py");
const editorEvidence = path.join(evidenceRoot, "editor-fixture.json");
const exportRoot = path.join(evidenceRoot, "editor-exports");
const desktopRoot = path.join(sourceRoot, "desktop");
const coreVersion = execFileSync(
  pythonExecutable,
  ["-c", "from importlib.metadata import version; print(version('tongs'))"],
  { encoding: "utf8" },
).trim();
const sourceCommit = process.env.TONGS_UTILITY_PROOF_COMMIT ?? null;

let controller = null;
let transport = null;
let window = null;

const mark = (stage) => process.stderr.write(`[native-utility-proof] ${stage}\n`);

async function runProof() {
  try {
    await mkdir(evidenceRoot, { recursive: true, mode: 0o700 });
    await rm(editorEvidence, { force: true });
    await rm(exportRoot, { recursive: true, force: true });
    process.env.TONGS_EDITOR_EVIDENCE_PATH = editorEvidence;
    await app.whenReady();
    const desktopSession = session.fromPartition(
      `tongs-utility-proof-${process.pid}`,
      { cache: false },
    );
    desktopSession.setPermissionRequestHandler(
      (_contents, _permission, callback) => callback(false),
    );
    desktopSession.setPermissionCheckHandler(() => false);
    desktopSession.webRequest.onBeforeRequest((details, callback) =>
      callback({ cancel: !isAllowedAppUrl(details.url) }),
    );

    const fixtureSpawn = (executable, _arguments, options) => {
      const child = spawn(executable, ["-E", "-P", fixtureSidecar], {
        ...options,
        env: {
          ...options.env,
          TONGS_CI_PROOF_EVIDENCE: evidenceRoot,
          TONGS_EDITOR_COMMAND: `${quote(pythonExecutable)} ${quote(editorFixture)} --controlled-editor`,
          TONGS_EDITOR_EVIDENCE_PATH: editorEvidence,
        },
      });
      child.stderr.on("data", (chunk) => process.stderr.write(chunk));
      return child;
    };
    transport = new SidecarTransport(
      { pythonExecutable, coreVersion, safeCwd: evidenceRoot },
      2_000,
      10_000,
      5_000,
      fixtureSpawn,
    );
    await transport.start();
    const assets = new AssetCatalog(transport, path.join(desktopRoot, "dist", "shell"));
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
    window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
    window.webContents.on("will-navigate", (event) => event.preventDefault());
    window.webContents.on("will-attach-webview", (event) => event.preventDefault());
    const utilities = new WorkspaceUtilities(transport, clipboard, exportRoot);
    controller = new DesktopIpcController(window, transport, assets, utilities);
    controller.register();
    mark("load application");
    await window.loadURL(APP_DOCUMENT);

    const review = await evaluate(`
      const repository = await waitFor("repository", () =>
        [...document.querySelectorAll(".nav-item")].find((item) =>
          item.textContent.includes("proof/desktop-ci")));
      repository.click();
      const review = await waitFor("review", () =>
        document.querySelector('.review-card[data-review-number="47"]'));
      review.click();
      await waitFor("copy", () => button("Copy URL"));
      button("Copy URL").click();
      await waitFor("copy result", () =>
        document.body.innerText.includes("Review URL copied to the clipboard."));
      return snapshot();
    `);
    const copiedUrl = clipboard.readText();

    const cache = await evaluate(`
      button("Clear Cache").click();
      await waitFor("clear confirmation", () =>
        document.querySelector(".utility-dialog"));
      button("Clear cache").click();
      await waitFor("clear result", () =>
        document.body.innerText.includes("Draft reviews were preserved."));
      return snapshot();
    `);

    const editor = await evaluate(`
      button("Dismiss").click();
      button("Pipelines").click();
      await waitFor("job log", () => document.querySelector(".ci-log-lines"));
      button("Open log in editor").click();
      await waitFor("editor result", () =>
        document.body.innerText.includes("Tongs cannot confirm"));
      return snapshot();
    `);
    await waitForFile(editorEvidence);
    const editorRecord = JSON.parse(await readFile(editorEvidence, "utf8"));
    await waitForEmpty(exportRoot);
    const screenshot = await capture("utility-workflows.png");
    const actions = (await readFile(path.join(evidenceRoot, "mock-forge-actions.jsonl"), "utf8"))
      .trim()
      .split("\n")
      .filter(Boolean)
      .map((line) => JSON.parse(line));
    const report = {
      provenance:
        "Production renderer, preload, IPC controller, SidecarTransport, DesktopSidecarServer, WorkspaceUtilityService, Electron clipboard, and controlled external editor fixture",
      sourceCommit,
      coreVersion,
      electron: process.versions.electron,
      chromium: process.versions.chrome,
      security: {
        sandbox: true,
        contextIsolation: true,
        nodeIntegration: false,
        webviewTag: false,
        scheme: APP_DOCUMENT,
      },
      review,
      copiedUrl,
      cache,
      editor,
      editorRecord,
      actions,
      exportFilesAfterEditorExit: await readdir(exportRoot),
      screenshot,
      processMetrics: app.getAppMetrics().map((item) => ({
        type: item.type,
        pid: item.pid,
        sandboxed: item.sandboxed,
      })),
    };
    await writeFile(
      path.join(evidenceRoot, "native-utility-proof.json"),
      `${JSON.stringify(report, null, 2)}\n`,
      { mode: 0o600 },
    );
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
        throw new Error("Native utility proof timeout: " + label + ": " + document.body.innerText.slice(0, 1200));
      };
      const button = (label) => [...document.querySelectorAll("button")].find(
        (item) => item.textContent.trim() === label && !item.disabled);
      const snapshot = () => ({
        title: document.querySelector(".view-title")?.textContent ?? null,
        notice: document.querySelector(".utility-notice")?.textContent ?? null,
        pipeline: document.querySelector(".ci-pipeline.ci-selected")?.textContent ?? null,
        job: document.querySelector(".ci-job.ci-selected")?.textContent ?? null,
        processGlobal: typeof globalThis.process,
        requireGlobal: typeof globalThis.require,
      });
      ${body}
    })()`,
    true,
  );
}

async function waitForFile(filePath) {
  const started = Date.now();
  while (Date.now() - started < 10_000) {
    try {
      await readFile(filePath);
      return;
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
  }
  throw new Error("Controlled editor evidence was not written");
}

async function waitForEmpty(directory) {
  const started = Date.now();
  while (Date.now() - started < 10_000) {
    if ((await readdir(directory)).length === 0) return;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  throw new Error("Private editor export was not cleaned after editor exit");
}

async function capture(name) {
  await window.webContents.executeJavaScript(
    "new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))",
  );
  const bytes = (await window.webContents.capturePage()).toPNG();
  const output = path.join(evidenceRoot, name);
  await writeFile(output, bytes, { mode: 0o600 });
  return {
    path: output,
    sha256: createHash("sha256").update(bytes).digest("hex"),
    byteCount: bytes.length,
  };
}

function quote(value) {
  return `'${value.replaceAll("'", `'"'"'`)}'`;
}
