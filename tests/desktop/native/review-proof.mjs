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

/**
 * Repointed for the #181 review-surface redesign (rerun prep for #55). The old
 * surfaces this script drove no longer exist: the Discussions panel "Quick
 * comment" field and its composer, "Post quick suggestion" / "Add suggestion
 * to draft", the separate "Replacement code" and "Optional explanation"
 * fields, "Start review" / "Resume review", and the "Review body" / "Verdict"
 * single-select drawer fields are all gone. Every label and aria-label below
 * was read directly from the renderer source at
 * head `ecb7f851aec9362aa71ed178bcd8a55250e5d32f` (`origin/feat/desktop-app`,
 * the head with every #181 card integrated):
 *
 * - Tabs (`desktop/src/renderer/features/review-detail/index.tsx`,
 *   `features/diff/index.tsx`, `features/review/index.tsx`): "Overview",
 *   "Files changed", "Discussions", "Commits".
 * - General comment, Overview only (`review-detail/index.tsx`, mounted inside
 *   `.panel.general-composer-panel`; rendered by
 *   `features/review/composer.tsx` `InlineComposer` with `anchor: null`):
 *   section `aria-label="General comment composer"`, textarea
 *   `aria-label="General review comment"`.
 * - In-diff comment, Files changed only (`features/diff/index.tsx`, same
 *   `InlineComposer` with a line/range anchor): section
 *   `aria-label="Inline comment composer"`, textarea
 *   `aria-label="Inline review comment"`, button "Insert suggestion"
 *   (disabled until mutation capabilities load and a valid new-side
 *   selection exists; refuses a body that already carries a suggestion
 *   block), button "Preview".
 * - Composer write actions, both composers, same implementation
 *   (`composer.tsx` `InlineComposer`): button "Add comment now" (immediate
 *   write), primary button "Start a review" (no pending review yet) or
 *   "Add to review" (one already open) or "Save changes" (editing a pending
 *   entry); overflow button `aria-label="More review actions"` text "More",
 *   then "Discard review" -> "Confirm discard of {subject}".
 * - Gutter affordance (`features/diff/index.tsx` `GutterComment`): button
 *   `aria-label="Comment on {old|new} line {N}"`, text "+". Opens the in-diff
 *   composer on the current line/range selection.
 * - Line selection (`features/diff/index.tsx` `LineNumberAnchor`): button
 *   `aria-label="Select {old|new} line {N}"` -- unchanged by #181, kept as
 *   evidence it was checked, not assumed.
 * - Pending entries (`features/review/pending-card.tsx` `PendingCard`,
 *   `features/review/drawer.tsx` `DrawerEntryRow`): button
 *   `aria-label="Edit pending comment on {label}"` text "Edit", button
 *   `aria-label="Delete pending comment on {label}"` text "Delete".
 * - Published threads (`features/review/thread.tsx`): row button
 *   `aria-label="Reply to {reference}"` text "Reply" opens the reply
 *   composer; inside it, textarea `aria-label="Reply body for {reference}"`,
 *   checkbox label "Resolve thread" / "Reopen thread", button "Reply now".
 * - Discussions panel (`features/review/index.tsx` `ThreadJumpList`): a pure
 *   jump list now, no composer, no reply or resolve control, and no way to
 *   start a review. Button "Show in diff" per row is unchanged.
 * - "Your review" drawer (`features/review/drawer.tsx`): toggle button
 *   `aria-label="Your review, {N} pending"` text "Your review"; drawer
 *   `aria-label="Your review"`; its own close button
 *   `aria-label="Close your review"` text "Close" (same text as the MR-level
 *   "Close" action below -- the drawer must be closed via its aria-label
 *   before that action is pressed). Inside: label "Summary" wrapping a
 *   textarea; `role="radiogroup" aria-label="Verdict"` with tile `<label>`s
 *   "Comment" / "Approve" / "Request changes", each wrapping a radio input;
 *   button "Save summary and verdict" ("Saving…" while in flight); "Discard
 *   review" -> "Confirm discard of {subject}"; "Submit review" -> "Confirm
 *   submit review"; each pending entry's body renders in
 *   `.review-drawer-entry-body`, general/reply entries grouped under
 *   "Review-level".
 * - MR-level actions (`features/review/index.tsx` `ActionButtons`), untouched
 *   by #181: "Merge" / "Close" / "Reopen" / "Remove approval" ->
 *   "Confirm {label}"; a rejected action reports through
 *   `role="alert"` inside `.review-workflow-shell` with the message from
 *   `desktop/src/shared/review.ts` `REVIEW_MUTATION_MESSAGES.conflict`,
 *   unchanged: "The review changed remotely. Refresh it before choosing
 *   another action."
 * - Uncertainty acknowledgment (`review-detail/index.tsx` and
 *   `features/review/index.tsx`, unchanged): button "I inspected the forge;
 *   acknowledge uncertainty".
 */

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
const discoveryControl = path.join(evidenceRoot, "discovery-state.txt");

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
      "discovery-state.txt",
      "mock-forge-actions.jsonl",
      "native-review-proof.json",
    ]) await rm(path.join(evidenceRoot, name), { force: true });
    await writeFile(discoveryControl, "present\n", { mode: 0o600 });
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

    mark("exercise overview general composer and navigation buffer");
    await navigateToReview();
    const initial = await evaluate(`
      button("Overview").click();
      await waitFor("overview general composer", () =>
        document.querySelector('section[aria-label="General comment composer"]'));
      const composer = labelled("General review comment");
      setValue(composer, "kept across panel navigation");
      button("Files changed").click();
      await waitFor("source diff", () =>
        document.querySelector('button[aria-label="Select new line 3"]'));
      button("Overview").click();
      await waitFor("overview general composer restored", () =>
        document.querySelector('section[aria-label="General comment composer"]'));
      const restored = labelled("General review comment");
      if (restored.value !== "kept across panel navigation")
        throw new Error("ordinary panel navigation lost the composer buffer");
      return snapshot();
    `);
    const navigationScreenshot = await capture("00-unsent-buffer-after-navigation.png");

    mark("exercise controlled discovery removal and restoration");
    await evaluate(`
      setValue(labelled("General review comment"), "kept across discovery removal");
      return true;
    `);
    await writeFile(discoveryControl, "removed\n", { mode: 0o600 });
    const discoveryRemoval = await evaluate(`
      button("Refresh local repositories").click();
      await waitFor("removed repository fallback", () =>
        document.querySelector(".view-title")?.textContent === "All reviews");
      const repositoryVisible = [...document.querySelectorAll(".nav-item")].some(
        (item) => item.textContent.includes("proof/desktop-review"),
      );
      if (repositoryVisible)
        throw new Error("removed repository remains in native navigation");
      const notice = [...document.querySelectorAll("main > .notice")].find((item) =>
        item.textContent.includes("no longer in the local workspace"),
      );
      if (!notice)
        throw new Error("native route fallback did not explain repository removal");
      return { ...snapshot(), notice: notice.textContent, repositoryVisible };
    `);
    const discoveryRemovalScreenshot = await capture(
      "01-controlled-discovery-removal.png",
    );
    await writeFile(discoveryControl, "present\n", { mode: 0o600 });
    await evaluate(`
      const refresh = await waitFor("repository refresh", () => {
        const candidate = button("Refresh local repositories");
        return candidate && !candidate.disabled ? candidate : null;
      });
      refresh.click();
      await waitFor("restored repository", () =>
        [...document.querySelectorAll(".nav-item")].find((item) =>
          item.textContent.includes("proof/desktop-review")));
      return true;
    `);
    await navigateToReview();
    const discoveryRestoration = await evaluate(`
      button("Overview").click();
      const composer = await waitFor("restored discovery buffer", () =>
        labelled("General review comment"));
      if (composer.value !== "kept across discovery removal")
        throw new Error("discovery reconciliation lost the unsent review buffer");
      if ([...document.querySelectorAll("main > .notice")].some((item) =>
        item.textContent.includes("no longer in the local workspace")))
        throw new Error("repository removal notice remained after restoration");
      return snapshot();
    `);
    const discoveryRestorationScreenshot = await capture(
      "02-controlled-discovery-restoration.png",
    );
    await evaluate(`setValue(labelled("General review comment"), ""); return true;`);

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
      document.querySelector('button[aria-label="Comment on new line 5"]').click();
      const body = await waitFor("in-diff composer", () =>
        labelled("Inline review comment"));
      const insert = await waitFor("enabled Insert suggestion action", () => {
        const action = button("Insert suggestion");
        return action && !action.disabled ? action : null;
      });
      insert.click();
      if (body.value !== "\u0060\u0060\u0060suggestion\\nbefore\\nnew value\\nafter\\n\u0060\u0060\u0060")
        throw new Error("suggestion was not seeded from exact selected source");
      // The reader's note is typed, then Insert suggestion combines it with a
      // freshly recomputed block; the block content is then edited in place,
      // which is what the single shared textarea replaces the old separate
      // "Replacement code" and "Optional explanation" fields with.
      setValue(body, "");
      setValue(body, "Use the guarded replacement");
      const insertAgain = await waitFor("enabled Insert suggestion action, second press", () => {
        const action = button("Insert suggestion");
        return action && !action.disabled ? action : null;
      });
      insertAgain.click();
      setValue(
        body,
        "Use the guarded replacement\\n\\n\u0060\u0060\u0060suggestion\\n  replacement(\u0060value\u0060)  \\n\u0060\u0060\u0060",
      );
      button("Add comment now").click();
      return snapshot();
    `);
    await waitForAction("create_inline_comment", 1);
    const suggestionScreenshot = await capture("03-suggestion-posted.png");

    mark("resolve the discussion jump against the current loaded diff");
    const discussionJump = await evaluate(`
      button("Discussions").click();
      await waitFor("review workflow", () => document.querySelector(".review-workflow-shell"));
      const show = await waitFor("show discussion in diff", () => button("Show in diff"));
      show.click();
      await waitFor("discussion diff target", () =>
        document.querySelector('button[aria-label="Select new line 4"][aria-pressed="true"]'));
      return snapshot();
    `);
    const discussionJumpScreenshot = await capture("04-discussion-jump.png");
    await evaluate(`
      button("Discussions").click();
      await waitFor("review workflow", () => document.querySelector(".review-workflow-shell"));
      return true;
    `);

    mark("exercise known and unknown quick comments without replay");
    await evaluate(`
      button("Overview").click();
      await waitFor("overview general composer", () =>
        document.querySelector('section[aria-label="General comment composer"]'));
      return true;
    `);
    await sendComposer("General review comment", "Add comment now", "controlled known quick comment");
    await evaluate(`
      await waitFor("known quick completion", () => labelled("General review comment")?.value === "");
      return true;
    `);
    await sendComposer("General review comment", "Add comment now", "controlled unknown remote result");
    const unknown = await evaluate(`
      await waitFor("unknown result", () =>
        document.body.innerText.includes("remote result is unknown"));
      return snapshot();
    `);
    const unknownScreenshot = await capture("05-quick-unknown-dark.png");
    await clickButton("I inspected the forge; acknowledge uncertainty");
    await evaluate(`
      await waitFor("uncertainty acknowledgment", () =>
        document.body.innerText.includes("acknowledged without replay"));
      return true;
    `);

    mark("create, inspect, and save a durable draft");
    await evaluate(`
      // The redesign has no standalone "Start review": the same primary write
      // button reads "Start a review" until a pending review exists, then
      // "Add to review" on every composer after. This first durable write is
      // what starts it.
      button("Files changed").click();
      const line = await waitFor("durable suggestion source", () =>
        document.querySelector('button[aria-label="Select new line 4"]'));
      line.click();
      document.querySelector('button[aria-label="Comment on new line 4"]').click();
      const body = await waitFor("in-diff composer", () => labelled("Inline review comment"));
      const insert = await waitFor("enabled Insert suggestion action", () => {
        const action = button("Insert suggestion");
        return action && !action.disabled ? action : null;
      });
      insert.click();
      if (body.value !== "\u0060\u0060\u0060suggestion\\nnew value\\n\u0060\u0060\u0060")
        throw new Error("durable suggestion did not retain exact source text");
      setValue(body, "\u0060\u0060\u0060suggestion\\ndurable_value\\n\u0060\u0060\u0060");
      const start = await waitFor("enabled primary composer action", () => {
        const action = [...document.querySelectorAll(".inline-composer-writes button.button:not(.button-secondary):not(.button-danger)")][0];
        return action && !action.disabled ? action : null;
      });
      if (start.textContent.trim() !== "Start a review")
        throw new Error("first durable write did not read Start a review");
      start.click();
      await waitFor("durable suggestion pending card", () => document.querySelector(".pending-card"));
      return true;
    `);
    await evaluate(`
      button("Overview").click();
      await waitFor("overview general composer", () =>
        document.querySelector('section[aria-label="General comment composer"]'));
      setValue(labelled("General review comment"), "Durable general draft comment");
      const add = await waitFor("enabled Add to review action", () => {
        const action = [...document.querySelectorAll(".inline-composer-writes button.button:not(.button-secondary):not(.button-danger)")][0];
        return action && !action.disabled ? action : null;
      });
      if (add.textContent.trim() !== "Add to review")
        throw new Error("second durable write did not read Add to review");
      add.click();
      await waitFor("general draft entry saved", () => labelled("General review comment")?.value === "");
      return true;
    `);
    await evaluate(`
      button("Discussions").click();
      await waitFor("review workflow", () => document.querySelector(".review-workflow-shell"));
      buttonAria("Your review, ").click();
      await waitFor("your review drawer", () => document.querySelector('[aria-label="Your review"]'));
      return true;
    `);
    await evaluate(`
      const entryText = () =>
        [...document.querySelectorAll(".review-drawer-entry-body")].map((item) => item.textContent);
      await waitFor("durable general entry", () =>
        entryText().some((text) => text.includes("Durable general draft comment")));
      if (!entryText().some((text) => text.includes("durable_value")))
        throw new Error("durable suggestion entry missing from Your review drawer");
      setValue(labelled("Summary"), "Durable review body after renderer and sidecar restart");
      // S54 names the Comment verdict specifically; this is the scenario's
      // automated counterpart, so the tile choice has to match it.
      labelled("Comment").click();
      return snapshot();
    `);
    await clickButton("Save summary and verdict");
    await evaluate(`
      await waitFor("saved draft", () => button("Save summary and verdict")?.disabled === true);
      return true;
    `);
    const draftScreenshot = await capture("06-draft-review-dark.png");

    mark("restart sidecar and renderer, then recover persisted draft");
    controller.reset();
    await transport.restart();
    await assets.refresh();
    await window.loadURL(APP_DOCUMENT);
    await navigateToReview();
    await evaluate(`
      // Recovery is automatic in the redesign: the shared workflow state adopts
      // the one active durable draft on mount, with no "Resume review" press.
      button("Discussions").click();
      await waitFor("review workflow", () => document.querySelector(".review-workflow-shell"));
      buttonAria("Your review, ").click();
      await waitFor("your review drawer", () => document.querySelector('[aria-label="Your review"]'));
      return true;
    `);
    const recoveredDraft = await evaluate(`
      await waitFor("recovered draft body", () =>
        labelled("Summary")?.value ===
          "Durable review body after renderer and sidecar restart");
      const entryText = () =>
        [...document.querySelectorAll(".review-drawer-entry-body")].map((item) => item.textContent);
      if (!entryText().some((text) => text.includes("Durable general draft comment")))
        throw new Error("durable general draft comment was not recovered");
      // .review-drawer-entry-body renders through SafeMarkdown, so a fenced
      // suggestion block never reaches the DOM as literal backtick text; the
      // fence becomes a rendered code block and textContent carries only the
      // code, the same as the entryText() check just above.
      if (!entryText().some((text) => text.includes("durable_value")))
        throw new Error("durable suggestion was not recovered");
      return snapshot();
    `);
    const recoveredScreenshot = await capture("07-draft-recovered-after-restart.png");

    mark("submit persisted draft through production submission service");
    await clickButton("Submit review");
    await clickButton("Confirm submit review");
    const submitted = await evaluate(`
      await waitFor("submitted review", () =>
        document.body.innerText.includes("Review submitted."), 15000);
      return snapshot();
    `);
    const submittedScreenshot = await capture("08-submitted-review.png");

    mark("exercise known conflict and merge action");
    // "Close" is ambiguous while the drawer is open: its own close button
    // reads "Close" too. Dismiss it by aria-label before the MR-level action.
    await evaluate(`
      const closeDrawer = document.querySelector('button[aria-label="Close your review"]');
      if (closeDrawer) closeDrawer.click();
      await waitFor("drawer closed", () => document.querySelector('[aria-label="Your review"]') === null);
      return true;
    `);
    await clickButton("Close");
    await clickButton("Confirm Close");
    const conflict = await evaluate(`
      await waitFor("known conflict", () =>
        document.body.innerText.includes("review changed remotely"));
      return snapshot();
    `);
    const conflictScreenshot = await capture("09-known-conflict.png");

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
    const finalScreenshot = await capture("10-review-light-narrow.png");
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
        discoveryRemoval,
        discoveryRestoration,
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
        discoveryRemovalScreenshot,
        discoveryRestorationScreenshot,
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
    return true;
  `);
}

/**
 * Sets `body` in the field labelled `fieldLabel` and presses the button
 * labelled `buttonLabel`. The redesign separates the two: a composer's
 * textarea and its write actions no longer share one label the way the old
 * "Quick comment" field and its own same-named button did.
 */
async function sendComposer(fieldLabel, buttonLabel, body) {
  await evaluate(`
    const composer = await waitFor(${JSON.stringify(fieldLabel)}, () => labelled(${JSON.stringify(fieldLabel)}));
    setValue(composer, ${JSON.stringify(body)});
    button(${JSON.stringify(buttonLabel)}).click();
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
  let text;
  try {
    text = await readFile(path.join(evidenceRoot, "mock-forge-actions.jsonl"), "utf8");
  } catch (error) {
    if (error?.code === "ENOENT") return [];
    throw error;
  }
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
      // For a button whose text is not exact-matchable because it carries a
      // dynamic suffix, such as "Your review" and its live pending count in
      // aria-label="Your review, {N} pending".
      const buttonAria = (prefix) => [...document.querySelectorAll("button")].find(
        (item) => (item.getAttribute("aria-label") || "").startsWith(prefix),
      );
      // Most composer and pending-card fields carry their accessible name as
      // an aria-label directly on the control (composer.tsx's "General
      // review comment" / "Inline review comment" / "Pending review
      // comment" textareas have no wrapping <label> at all). The drawer's
      // "Summary" field and its "Comment" / "Approve" / "Request changes"
      // verdict tiles are the opposite: a <label> wraps the control and
      // carries the text, with no aria-label on the control itself. Try the
      // aria-label form first, then fall back to the <label>-wrapping form,
      // so one helper covers both shapes correctly instead of assuming
      // either one everywhere.
      const labelled = (label) => {
        const byAria = document.querySelector(
          'textarea[aria-label="' + label + '"], input[aria-label="' + label
            + '"], select[aria-label="' + label + '"]',
        );
        if (byAria) return byAria;
        return [...document.querySelectorAll("label")].find(
          (item) => item.textContent.includes(label),
        )?.querySelector("textarea,select,input") ?? null;
      };
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
        draftBody: labelled("Summary")?.value ?? null,
        quickBody: labelled("General review comment")?.value ?? null,
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
