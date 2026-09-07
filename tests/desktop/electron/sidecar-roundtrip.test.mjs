import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtemp } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { SidecarTransport } from "../../../desktop/dist/src/main/sidecar.js";
const python = path.resolve(process.env.TONGS_TEST_PYTHON ?? "../.venv/bin/python");
test("actual installed sidecar completes handshake and bounded asset read", async () => {
  const isolated = await mkdtemp(path.join(os.tmpdir(), "tongs-sidecar-")); process.env.XDG_CONFIG_HOME = path.join(isolated, "config"); process.env.XDG_CACHE_HOME = path.join(isolated, "cache"); process.env.XDG_DATA_HOME = path.join(isolated, "data");
  const version = execFileSync(python, ["-c", "import importlib.metadata; print(importlib.metadata.version('tongs'))"], { encoding: "utf8" }).trim();
  const transport = new SidecarTransport({ pythonExecutable: python, coreVersion: version, safeCwd: isolated }, 2_000, 5_000, 2_000); await transport.start();
  assert.equal(transport.sessionGeneration, 1); assert.ok(transport.processId); const result = await transport.requestRead("assets.list", {}).result; assert.ok(Array.isArray(result.assets));
  await transport.stop(); assert.equal(transport.crashHistory.at(-1)?.unexpected, false);
});
