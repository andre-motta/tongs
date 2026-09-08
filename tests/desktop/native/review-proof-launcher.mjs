import { spawn } from "node:child_process";
import path from "node:path";

const sourceRoot = path.resolve(
  process.env.TONGS_REVIEW_PROOF_SOURCE ?? path.join(import.meta.dirname, "../../.."),
);
const evidenceRoot = path.resolve(
  process.env.TONGS_REVIEW_PROOF_EVIDENCE ??
    path.join(sourceRoot, ".evidence", "issue-46"),
);
const electron = path.join(sourceRoot, "desktop", "node_modules", ".bin", "electron");
const child = spawn(
  electron,
  [
    path.join(sourceRoot, "tests", "desktop", "native", "review-proof-app"),
    "--ozone-platform=x11",
    `--user-data-dir=${path.join(evidenceRoot, "electron-profile")}`,
  ],
  {
    cwd: sourceRoot,
    env: {
      ...process.env,
      TONGS_REVIEW_PROOF_SOURCE: sourceRoot,
      TONGS_REVIEW_PROOF_EVIDENCE: evidenceRoot,
    },
    stdio: ["ignore", "pipe", "pipe"],
  },
);

child.stdout.pipe(process.stdout);
child.stderr.pipe(process.stderr);
const timer = setTimeout(() => child.kill("SIGTERM"), 120_000);
const result = await new Promise((resolve, reject) => {
  child.once("error", reject);
  child.once("exit", (code, signal) => resolve({ code, signal }));
});
clearTimeout(timer);
if (result.code !== 0) {
  throw new Error(
    `Native review proof exited unsuccessfully: ${result.code}/${result.signal}`,
  );
}
