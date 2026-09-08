import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { readFile, rm } from "node:fs/promises";
import path from "node:path";

const sourceRoot = path.resolve(
  process.env.TONGS_UTILITY_PROOF_SOURCE ?? path.join(import.meta.dirname, "../../.."),
);
const evidenceRoot = path.resolve(
  process.env.TONGS_UTILITY_PROOF_EVIDENCE ??
    path.join(sourceRoot, ".evidence", "issue-100"),
);
const expectedCommit = process.env.TONGS_UTILITY_PROOF_COMMIT;
if (!expectedCommit)
  throw new Error("Native utility proof requires an exact source commit");
const reportPath = path.join(evidenceRoot, "native-utility-proof.json");
await rm(reportPath, { force: true });
const electron = path.join(sourceRoot, "desktop", "node_modules", ".bin", "electron");
const child = spawn(
  electron,
  [
    path.join(sourceRoot, "tests", "desktop", "native", "utility-proof-app"),
    "--ozone-platform=x11",
    `--user-data-dir=${path.join(evidenceRoot, "electron-profile")}`,
  ],
  {
    cwd: sourceRoot,
    env: {
      ...process.env,
      TONGS_UTILITY_PROOF_SOURCE: sourceRoot,
      TONGS_UTILITY_PROOF_EVIDENCE: evidenceRoot,
    },
    stdio: ["ignore", "pipe", "pipe"],
  },
);

child.stdout.pipe(process.stdout);
child.stderr.pipe(process.stderr);
const timer = setTimeout(() => child.kill("SIGTERM"), 60_000);
const result = await new Promise((resolve, reject) => {
  child.once("error", reject);
  child.once("exit", (code, signal) => resolve({ code, signal }));
});
clearTimeout(timer);
if (result.code !== 0) {
  throw new Error(
    `Native utility proof exited unsuccessfully: ${result.code}/${result.signal}`,
  );
}
let report;
try {
  report = JSON.parse(await readFile(reportPath, "utf8"));
} catch (error) {
  throw new Error("Native utility proof did not produce its authoritative report", {
    cause: error,
  });
}
assert.equal(report.sourceCommit, expectedCommit);
assert.equal(
  report.copiedUrl,
  "https://fixture.example/proof/desktop-ci/merge_requests/47",
);
assert.equal(report.security.sandbox, true);
assert.equal(report.security.contextIsolation, true);
assert.equal(report.security.nodeIntegration, false);
assert.equal(report.security.webviewTag, false);
assert.equal(report.review.processGlobal, "undefined");
assert.equal(report.review.requireGlobal, "undefined");
assert.equal(report.editorRecord.file_mode, 0o600);
assert.equal(
  report.editorRecord.content,
  "starting controlled build\n" +
    "\u001b[32mPASS\u001b[0m renderer bridge\n" +
    "literal <script>alert('inert')</script> remains text\n" +
    "search-target mutation receipt\n",
);
assert.match(
  report.editorRecord.export_name,
  /^tongs-slot-[1-8]-job-201-[0-9a-f]{32}\.log$/,
);
assert.deepEqual(report.exportFilesAfterEditorExit, []);
assert.ok(
  report.editorLedgerFiles.includes(".tongs-editor-ledger.sqlite3"),
);
assert.equal(typeof report.screenshot.sha256, "string");
assert.equal(report.screenshot.sha256.length, 64);
