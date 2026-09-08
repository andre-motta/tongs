import assert from "node:assert/strict";
import { spawn, execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdir, readFile, realpath, rm, writeFile } from "node:fs/promises";
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
  process.env.TONGS_REVIEW_PROOF_SOURCE ?? path.join(import.meta.dirname, "../../.."),
);
const evidenceRoot = path.resolve(
  process.env.TONGS_REVIEW_PROOF_EVIDENCE ??
    path.join(sourceRoot, ".evidence", "issue-46"),
);
const pythonExecutable = path.resolve(
  process.env.TONGS_REVIEW_PROOF_PYTHON ?? path.join(sourceRoot, ".venv", "bin", "python"),
);
const installMode = process.env.TONGS_REVIEW_PROOF_INSTALL_MODE ?? "editable";
const fixtureSidecar = path.join(
  sourceRoot,
  "tests",
  "desktop",
  "native",
  "review_fixture_sidecar.py",
);
const desktopRoot = path.join(sourceRoot, "desktop");
const coreVersion = execFileSync(
  pythonExecutable,
  ["-E", "-P", "-c", "from importlib.metadata import version; print(version('tongs'))"],
  { encoding: "utf8" },
).trim();
const sourceCommit = process.env.TONGS_REVIEW_PROOF_COMMIT ?? null;
const wheelPath = process.env.TONGS_REVIEW_PROOF_WHEEL ?? null;

let controller = null;
let transport = null;
let window = null;
const childLaunches = [];

const mark = (stage) => process.stderr.write(`[native-review-proof] ${stage}\n`);

async function runProof() {
  try {
    mark("prepare durable evidence");
    await mkdir(evidenceRoot, { recursive: true, mode: 0o700 });
    for (const name of [
      "drafts.db",
      "drafts.db-shm",
      "drafts.db-wal",
      "mock-forge-actions.jsonl",
      "native-review-proof.json",
    ]) await rm(path.join(evidenceRoot, name), { force: true });
    const sourceBinding = await verifySourceBinding();

    mark("wait for Electron");
    await app.whenReady();
    const desktopSession = session.fromPartition(`tongs-review-proof-${process.pid}`, {
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
      childLaunches.push({ executable, arguments: ["-E", "-P", fixtureSidecar] });
      const child = spawn(executable, ["-E", "-P", fixtureSidecar], options);
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
    mark("start production sidecar transport");
    await transport.start();
    const assets = new AssetCatalog(transport, path.join(desktopRoot, "dist", "shell"));
    await assets.refresh();
    desktopSession.protocol.handle("tongs", (request) => assets.response(request.url));

    window = new BrowserWindow({
      width: 1180,
      height: 820,
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
        `[native-review-proof] renderer ${details.level}: ${details.message}\n`,
      );
    });
    window.webContents.on("did-fail-load", (_event, code, description, url) => {
      process.stderr.write(
        `[native-review-proof] load failed ${code} ${description} ${url}\n`,
      );
    });
    window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
    window.webContents.on("will-navigate", (event) => event.preventDefault());
    window.webContents.on("will-attach-webview", (event) => event.preventDefault());
    controller = new DesktopIpcController(window, transport, assets);
    controller.register();
    await window.loadURL(APP_DOCUMENT);

    mark("exercise discussions and navigation buffer");
    await navigateToReview();
    const initial = await evaluate(`
      const composer = labelled("Quick comment");
      setValue(composer, "kept across panel navigation");
      button("Overview").click();
      await waitFor("overview", () => button("Discussions"));
      button("Discussions").click();
      await waitFor("workflow", () => document.querySelector(".review-workflow-shell"));
      const restored = labelled("Quick comment");
      if (restored.value !== "kept across panel navigation")
        throw new Error("ordinary panel navigation lost the composer buffer");
      return snapshot();
    `);
    const navigationScreenshot = await capture("00-unsent-buffer-after-navigation.png");
    await evaluate(`setValue(labelled("Quick comment"), ""); return true;`);

    mark("author and post a controlled multiline GitHub suggestion");
    const suggestion = await evaluate(`
      button("Files changed").click();
      await waitFor("source diff", () =>
        document.querySelector('button[aria-label="Select new line 3"]'));
      document.querySelector('button[aria-label="Select new line 3"]').click();
      await waitFor("range origin selection", () =>
        document.querySelector(
          'button[aria-label="Select new line 3"][aria-pressed="true"]',
        ));
      document.querySelector('button[aria-label="Select new line 5"]').dispatchEvent(
        new MouseEvent("click", { bubbles: true, shiftKey: true }),
      );
      await waitFor("committed multiline selection", () =>
        [3, 4, 5].every((number) => document.querySelector(
          'button[aria-label="Select new line ' + number + '"][aria-pressed="true"]',
        )));
      const suggest = await waitFor("enabled suggestion action", () => {
        const action = button("Suggest replacement");
        return action && !action.disabled ? action : null;
      });
      suggest.click();
      const replacement = await waitFor("suggestion replacement", () =>
        labelled("Replacement code"));
      if (replacement.value !== "before\\nnew value\\nafter")
        throw new Error("suggestion was not seeded from exact selected source");
      setValue(labelled("Optional explanation"), "Use the guarded replacement");
      setValue(replacement, "  replacement(\u0060value\u0060)  ");
      button("Post quick suggestion").click();
      return snapshot();
    `);
    await waitForAction("create_inline_comment", 1);
    const suggestionScreenshot = await capture("01-suggestion-posted.png");

    mark("resolve the discussion jump against the current loaded diff");
    const discussionJump = await evaluate(`
      const show = await waitFor("show discussion in diff", () => button("Show in diff"));
      show.click();
      await waitFor("discussion diff target", () =>
        document.querySelector('button[aria-label="Select new line 4"][aria-pressed="true"]'));
      return snapshot();
    `);
    const discussionJumpScreenshot = await capture("02-discussion-jump.png");
    await evaluate(`
      button("Discussions").click();
      await waitFor("review workflow", () => document.querySelector(".review-workflow-shell"));
      return true;
    `);

    mark("exercise known and unknown quick comments without replay");
    await sendComposer("Quick comment", "controlled known quick comment");
    await evaluate(`
      await waitFor("known quick completion", () => labelled("Quick comment")?.value === "");
      return true;
    `);
    await sendComposer("Quick comment", "controlled unknown remote result");
    const unknown = await evaluate(`
      await waitFor("unknown result", () =>
        document.body.innerText.includes("remote result is unknown"));
      return snapshot();
    `);
    const unknownScreenshot = await capture("03-quick-unknown-dark.png");
    await clickButton("I inspected the forge; acknowledge uncertainty");
    await evaluate(`
      await waitFor("uncertainty acknowledgment", () =>
        document.body.innerText.includes("acknowledged without replay"));
      return true;
    `);

    mark("create, inspect, and save a durable draft");
    await clickButton("Start review");
    await evaluate(`
      await waitFor("draft", () => document.body.innerText.includes("Draft review active"));
      button("Files changed").click();
      const line = await waitFor("durable suggestion source", () =>
        document.querySelector('button[aria-label="Select new line 4"]'));
      line.click();
      const suggest = await waitFor("enabled durable suggestion action", () => {
        const action = button("Suggest replacement");
        return action && !action.disabled ? action : null;
      });
      suggest.click();
      const replacement = await waitFor("durable suggestion replacement", () =>
        labelled("Replacement code"));
      if (replacement.value !== "new value")
        throw new Error("durable suggestion did not retain exact source text");
      setValue(replacement, "durable_value");
      button("Add suggestion to draft").click();
      button("Discussions").click();
      await waitFor("draft workflow", () => document.querySelector(".review-workflow-shell"));
      setValue(labelled("Add general draft comment"), "Durable general draft comment");
      button("Add general draft comment").click();
      await waitFor("draft comment", () =>
        [...document.querySelectorAll(".review-workflow-draft-comment textarea")]
          .some((item) => item.value === "Durable general draft comment"));
      setValue(labelled("Review body"), "Durable review body after renderer and sidecar restart");
      setValue(labelled("Verdict"), "approve");
      return snapshot();
    `);
    await clickButton("Save draft");
    await evaluate(`
      await waitFor("saved draft", () => button("Save draft")?.disabled === true);
      return true;
    `);
    const draftScreenshot = await capture("04-draft-review-dark.png");

    mark("restart sidecar and renderer, then recover persisted draft");
    controller.reset();
    await transport.restart();
    await assets.refresh();
    await window.loadURL(APP_DOCUMENT);
    await navigateToReview();
    await clickButton("Resume review");
    const recoveredDraft = await evaluate(`
      await waitFor("recovered draft body", () =>
        labelled("Review body")?.value ===
          "Durable review body after renderer and sidecar restart");
      const recoveredComments = [
        ...document.querySelectorAll(".review-workflow-draft-comment textarea"),
      ].map((item) => item.value);
      if (!recoveredComments.includes("Durable general draft comment"))
        throw new Error("durable general draft comment was not recovered");
      if (!recoveredComments.includes(
          "\u0060\u0060\u0060suggestion\\ndurable_value\\n\u0060\u0060\u0060"))
        throw new Error("durable suggestion was not recovered");
      return snapshot();
    `);
    const recoveredScreenshot = await capture("05-draft-recovered-after-restart.png");

    mark("submit persisted draft through production submission service");
    await clickButton("Submit review");
    await clickButton("Confirm submit review");
    const submitted = await evaluate(`
      await waitFor("submitted review", () =>
        document.body.innerText.includes("Review submitted."), 15000);
      return snapshot();
    `);
    const submittedScreenshot = await capture("06-submitted-review.png");

    mark("exercise known conflict and merge action");
    await clickButton("Close");
    await clickButton("Confirm Close");
    const conflict = await evaluate(`
      await waitFor("known conflict", () =>
        document.body.innerText.includes("review changed remotely"));
      return snapshot();
    `);
    const conflictScreenshot = await capture("07-known-conflict.png");

    nativeTheme.themeSource = "light";
    window.setSize(900, 720);
    await waitForFrames(3);
    const lightConflictNotice = await evaluate(`
      const alerts = [
        ...document.querySelectorAll('.review-workflow-shell [role="alert"]'),
      ];
      if (alerts.length !== 1)
        throw new Error(
          "known conflict must render one alert; found " +
            alerts.length +
            ": " +
            alerts.map((item) => item.textContent).join(" | "),
        );
      const alert = alerts[0];
      if (!alert.textContent.includes("The review changed remotely."))
        throw new Error("known conflict alert is not actionable");
      const style = getComputedStyle(alert);
      const channels = (value) => value.match(/[\\d.]+/g).slice(0, 3).map(Number);
      const luminance = (value) => {
        const linear = channels(value).map((channel) => {
          const normalized = channel / 255;
          return normalized <= 0.04045
            ? normalized / 12.92
            : ((normalized + 0.055) / 1.055) ** 2.4;
        });
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
      };
      const foreground = luminance(style.color);
      const background = luminance(style.backgroundColor);
      const contrastRatio =
        (Math.max(foreground, background) + 0.05) /
        (Math.min(foreground, background) + 0.05);
      if (contrastRatio < 4.5)
        throw new Error("light conflict notice contrast is below 4.5:1");
      return {
        alertCount: alerts.length,
        text: alert.textContent,
        color: style.color,
        backgroundColor: style.backgroundColor,
        contrastRatio,
      };
    `);
    const finalScreenshot = await capture("08-review-light-narrow.png");
    await clickButton("Merge");
    await clickButton("Confirm Merge");
    await waitForAction("merge", 1);
    const finalUi = await evaluate("return snapshot();");
    const actions = await readActions();
    assert.equal(
      actions.filter((item) => item.action === "add_comment" && item.body === "controlled unknown remote result").length,
      1,
      "unknown quick comment must not replay across restart",
    );
    assert.equal(actions.filter((item) => item.action === "close").length, 1);
    assert.equal(actions.filter((item) => item.action === "merge").length, 1);
    assert.ok(actions.some((item) => item.action === "submit_review"));
    const suggestionActions = actions.filter(
      (item) => item.action === "create_inline_comment",
    );
    assert.equal(suggestionActions.length, 2, "each suggestion must write exactly once");
    const quickSuggestion = suggestionActions.find((item) =>
      item.body.startsWith("Use the guarded replacement"),
    );
    const durableSuggestion = suggestionActions.find(
      (item) => item.body === "```suggestion\ndurable_value\n```",
    );
    assert.ok(quickSuggestion, "quick suggestion action missing");
    assert.ok(durableSuggestion, "durable suggestion action missing after restart");
    assert.deepEqual(
      {
        filePath: quickSuggestion.file_path,
        line: quickSuggestion.line,
        side: quickSuggestion.side,
        startLine: quickSuggestion.start_line,
        startSide: quickSuggestion.start_side,
        body: quickSuggestion.body,
      },
      {
        filePath: "src/example.py",
        line: 5,
        side: "RIGHT",
        startLine: 3,
        startSide: "RIGHT",
        body:
          "Use the guarded replacement\n\n```suggestion\n  replacement(`value`)  \n```",
      },
    );
    assert.deepEqual(
      {
        filePath: durableSuggestion.file_path,
        line: durableSuggestion.line,
        side: durableSuggestion.side,
        startLine: durableSuggestion.start_line,
        startSide: durableSuggestion.start_side,
      },
      {
        filePath: "src/example.py",
        line: 4,
        side: "RIGHT",
        startLine: null,
        startSide: null,
      },
    );
    assert.equal(childLaunches.length, 2, "proof must include one sidecar restart");
    for (const launch of childLaunches) {
      assert.equal(path.resolve(launch.executable), pythonExecutable);
      assert.deepEqual(launch.arguments.slice(0, 2), ["-E", "-P"]);
    }

    const wheel = wheelPath
      ? {
          path: path.resolve(wheelPath),
          sha256: digest(await readFile(path.resolve(wheelPath))),
        }
      : null;
    const gpu = await app.getGPUInfo("complete");
    const report = {
      provenance:
        "Production renderer, preload, typed review IPC, SidecarTransport, DesktopSidecarServer, DraftStore, ReviewMutationService, MRActionService, and ReviewSubmissionService with isolated mocked forge writes",
      sourceCommit,
      sourceRoot,
      fixtureSidecar,
      evidenceRoot,
      installMode,
      coreVersion,
      wheel,
      sourceBinding,
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
      childLaunches,
      sessionGeneration: transport.sessionGeneration,
      initial,
      demonstrations: {
        suggestion,
        discussionJump,
        unknown,
        recoveredDraft,
        submitted,
        conflict,
      },
      lightConflictNotice,
      finalUi,
      mockForgeActions: actions,
      screenshots: [
        navigationScreenshot,
        suggestionScreenshot,
        discussionJumpScreenshot,
        unknownScreenshot,
        draftScreenshot,
        recoveredScreenshot,
        submittedScreenshot,
        conflictScreenshot,
        finalScreenshot,
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
      path.join(evidenceRoot, "native-review-proof.json"),
      `${JSON.stringify(report, null, 2)}\n`,
      { mode: 0o600 },
    );
    mark("proof complete");
    process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
  } catch (error) {
    process.stderr.write(`${error instanceof Error ? error.stack : String(error)}\n`);
    process.exitCode = 1;
  } finally {
    const exitCode = process.exitCode ?? 0;
    controller?.dispose();
    if (window && !window.isDestroyed()) window.destroy();
    if (transport) await transport.stop().catch(() => undefined);
    app.exit(exitCode);
  }
}

void runProof();

async function verifySourceBinding() {
  const moduleNames = [
    "tongs.desktop.protocol.review_operations",
    "tongs.desktop.protocol.server",
    "tongs.services.session",
    "tongs.services.review_mutations",
    "tongs.services.review_submission",
    "tongs.state.drafts.store",
  ];
  const script = [
    "import hashlib, importlib, json, pathlib, sys",
    `names = ${JSON.stringify(moduleNames)}`,
    "items = []",
    "for name in names:",
    "    module = importlib.import_module(name)",
    "    location = pathlib.Path(module.__file__).resolve()",
    "    items.append({'module': name, 'path': str(location), 'sha256': hashlib.sha256(location.read_bytes()).hexdigest()})",
    "print(json.dumps({'executable': str(pathlib.Path(sys.executable).resolve()), 'prefix': str(pathlib.Path(sys.prefix).resolve()), 'modules': items}, sort_keys=True))",
  ].join("\n");
  const installed = JSON.parse(execFileSync(
    pythonExecutable,
    ["-E", "-P", "-c", script],
    { encoding: "utf8" },
  ));
  assert.equal(path.resolve(installed.executable), await realpath(pythonExecutable));
  const manifest = [];
  for (const item of installed.modules) {
    const relative = `${item.module.replaceAll(".", "/")}.py`;
    const source = path.join(sourceRoot, "src", relative);
    const sourceBytes = await readFile(source);
    const sourceHash = digest(sourceBytes);
    assert.equal(item.sha256, sourceHash, `${item.module} differs from candidate source`);
    if (installMode === "wheel") {
      assert.equal(isWithin(installed.prefix, item.path), true, `${item.module} is outside sys.prefix`);
    } else {
      assert.equal(path.resolve(item.path), path.resolve(source), `${item.module} is outside candidate source`);
    }
    manifest.push({ ...item, source, source_sha256: sourceHash });
  }
  const altered = Buffer.concat([
    await readFile(manifest[0].source),
    Buffer.from("\n# altered negative control\n"),
  ]);
  const negativeControl = {
    module: manifest[0].module,
    altered_sha256: digest(altered),
    rejected: digest(altered) !== manifest[0].sha256,
  };
  assert.equal(negativeControl.rejected, true, "altered relevant module must fail binding");
  return { interpreter: installed.executable, prefix: installed.prefix, modules: manifest, negativeControl };
}

function isWithin(parent, candidate) {
  const relative = path.relative(path.resolve(parent), path.resolve(candidate));
  return relative !== "" && !relative.startsWith("..") && !path.isAbsolute(relative);
}

async function navigateToReview() {
  await evaluate(`
    const repository = await waitFor("repository", () =>
      [...document.querySelectorAll(".nav-item")].find((item) =>
        item.textContent.includes("proof/desktop-review")));
    repository.click();
    await waitFor("review", () =>
      document.querySelector('.review-card[data-review-number="46"]'));
    await new Promise((resolve) => setTimeout(resolve, 100));
    document.querySelector('.review-card[data-review-number="46"]').click();
    await waitFor("review overview", () => document.querySelector(".review-header"));
    const discussions = await waitFor("discussions tab", () => button("Discussions"));
    discussions.click();
    await waitFor("review workflow", () => document.querySelector(".review-workflow-shell"));
    return true;
  `);
}

async function sendComposer(label, body) {
  await evaluate(`
    const composer = await waitFor(${JSON.stringify(label)}, () => labelled(${JSON.stringify(label)}));
    setValue(composer, ${JSON.stringify(body)});
    button(${JSON.stringify(label)}).click();
    return true;
  `);
}

async function clickButton(label) {
  await evaluate(`
    const target = await waitFor(${JSON.stringify(label)}, () => {
      const candidate = button(${JSON.stringify(label)});
      return candidate && !candidate.disabled ? candidate : null;
    });
    target.click();
    return true;
  `);
}

async function waitForAction(action, count) {
  const started = Date.now();
  while (Date.now() - started < 10_000) {
    const actions = await readActions();
    if (actions.filter((item) => item.action === action).length >= count) return;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  throw new Error(`Native review proof timeout: ${action} action`);
}

async function readActions() {
  const text = await readFile(path.join(evidenceRoot, "mock-forge-actions.jsonl"), "utf8");
  return text.trim().split("\n").filter(Boolean).map((line) => JSON.parse(line));
}

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
        throw new Error("Native review proof timeout: " + label + ": " + document.body.innerText.slice(0, 1600));
      };
      const button = (label) => [...document.querySelectorAll("button")].find(
        (item) => item.textContent.trim() === label,
      );
      const labelled = (label) => [...document.querySelectorAll("label")].find(
        (item) => item.textContent.includes(label),
      )?.querySelector("textarea,select,input") ?? null;
      const setValue = (element, value) => {
        const prototype = element instanceof HTMLTextAreaElement
          ? HTMLTextAreaElement.prototype
          : element instanceof HTMLSelectElement
            ? HTMLSelectElement.prototype
            : HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(prototype, "value").set.call(element, value);
        element.dispatchEvent(new Event("input", { bubbles: true }));
        element.dispatchEvent(new Event("change", { bubbles: true }));
      };
      const snapshot = () => ({
        title: document.querySelector(".view-title")?.textContent ?? null,
        workflow: document.querySelector(".review-workflow-shell")?.innerText ?? null,
        draftBody: labelled("Review body")?.value ?? null,
        quickBody: labelled("Quick comment")?.value ?? null,
        processGlobal: typeof globalThis.process,
        requireGlobal: typeof globalThis.require,
      });
      ${body}
    })()`,
    true,
  );
}

async function capture(name) {
  await waitForFrames(2);
  const bytes = (await window.webContents.capturePage()).toPNG();
  const output = path.join(evidenceRoot, name);
  await writeFile(output, bytes, { mode: 0o600 });
  return { path: output, sha256: digest(bytes), byteCount: bytes.length };
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

function digest(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}
