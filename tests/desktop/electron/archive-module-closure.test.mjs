import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  access,
  copyFile,
  mkdir,
  mkdtemp,
  readFile,
  rm,
  writeFile,
} from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const checkout = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../..",
);
const desktop = path.join(checkout, "desktop");
const packer = path.join(
  checkout,
  "packaging/desktop/archive/pack_asar.mjs",
);
const allowlistPath = path.join(
  checkout,
  "packaging/desktop/archive/app-asar-paths.txt",
);
const omittedBeforeIssue137 = new Set([
  "dist/src/main/utilities.js",
  "dist/src/shared/utilities.js",
]);
const expectedRuntimeModules = [
  "/dist/src/main/assets.js",
  "/dist/src/main/ci.js",
  "/dist/src/main/index.js",
  "/dist/src/main/ipc.js",
  "/dist/src/main/launch.js",
  "/dist/src/main/renderer_lifecycle.js",
  "/dist/src/main/review.js",
  "/dist/src/main/security.js",
  "/dist/src/main/sidecar.js",
  "/dist/src/main/utilities.js",
  "/dist/src/preload/index.cjs",
  "/dist/src/shared/bridge.js",
  "/dist/src/shared/ci.js",
  "/dist/src/shared/review.js",
  "/dist/src/shared/utilities.js",
];

async function allowlist() {
  return (await readFile(allowlistPath, "utf8"))
    .split("\n")
    .filter(Boolean);
}

async function stage(root, paths) {
  for (const relative of paths) {
    const destination = path.join(root, relative);
    await mkdir(path.dirname(destination), { recursive: true });
    if (relative === "package.json") {
      await writeFile(
        destination,
        '{"main":"dist/src/main/index.js","type":"module"}\n',
      );
    } else {
      await copyFile(path.join(desktop, relative), destination);
    }
  }
}

function pack(input, output) {
  return spawnSync(process.execPath, [packer, desktop, input, output], {
    encoding: "utf8",
  });
}

test("actual compiled desktop module closure is packaged", async (context) => {
  const root = await mkdtemp(path.join(os.tmpdir(), "tongs-asar-closure-"));
  context.after(() => rm(root, { recursive: true, force: true }));
  const input = path.join(root, "input");
  const output = path.join(root, "app.asar");
  await stage(input, await allowlist());

  const result = pack(input, output);

  assert.equal(result.status, 0, result.stderr);
  const entries = JSON.parse(result.stdout);
  assert.deepEqual(
    entries.filter((entry) => entry.endsWith(".js") || entry.endsWith(".cjs")),
    ["/dist/shell/app.js", ...expectedRuntimeModules],
  );
});

test("the prior ASAR inputs fail with both emitted modules named", async (context) => {
  const root = await mkdtemp(path.join(os.tmpdir(), "tongs-asar-prior-"));
  context.after(() => rm(root, { recursive: true, force: true }));
  const input = path.join(root, "input");
  const output = path.join(root, "app.asar");
  const prior = (await allowlist()).filter(
    (relative) => !omittedBeforeIssue137.has(relative),
  );
  await stage(input, prior);

  const result = pack(input, output);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /ASAR runtime module closure mismatch/);
  for (const omitted of omittedBeforeIssue137) {
    assert.ok(result.stderr.includes(omitted));
  }
  await assert.rejects(access(output));
});

test("a future emitted runtime module fails until explicitly packaged", async (context) => {
  const root = await mkdtemp(path.join(os.tmpdir(), "tongs-asar-future-"));
  context.after(() => rm(root, { recursive: true, force: true }));
  const emitted = path.join(desktop, "dist/src/main/future-runtime.js");
  await writeFile(emitted, "export {};\n", { flag: "wx" });
  context.after(() => rm(emitted, { force: true }));
  const input = path.join(root, "input");
  const output = path.join(root, "app.asar");
  await stage(input, await allowlist());

  const result = pack(input, output);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /ASAR runtime module closure mismatch/);
  assert.ok(result.stderr.includes("dist/src/main/future-runtime.js"));
  await assert.rejects(access(output));
});

test("compiler-only output cannot enter the ASAR runtime tree", async (context) => {
  const root = await mkdtemp(path.join(os.tmpdir(), "tongs-asar-excluded-"));
  context.after(() => rm(root, { recursive: true, force: true }));
  const input = path.join(root, "input");
  const output = path.join(root, "app.asar");
  await stage(input, await allowlist());
  const declaration = "dist/src/main/index.d.ts";
  await mkdir(path.dirname(path.join(input, declaration)), { recursive: true });
  await copyFile(path.join(desktop, declaration), path.join(input, declaration));

  const result = pack(input, output);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /unsupported file: dist\/src\/main\/index\.d\.ts/);
  await assert.rejects(access(output));
});
