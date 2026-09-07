import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { SidecarTransport } from "../../../desktop/dist/src/main/sidecar.js";

const checkout = path.resolve(import.meta.dirname, "../../..");
const python = process.env.TONGS_TEST_PYTHON ?? path.join(checkout, ".venv/bin/python");

test("actual installed sidecar returns bounded asset metadata", async () => {
  const isolated = await mkdtemp(path.join(os.tmpdir(), "tongs-sidecar-"));
  const original = {
    XDG_CONFIG_HOME: process.env.XDG_CONFIG_HOME,
    XDG_CACHE_HOME: process.env.XDG_CACHE_HOME,
    XDG_DATA_HOME: process.env.XDG_DATA_HOME,
  };
  process.env.XDG_CONFIG_HOME = path.join(isolated, "config");
  process.env.XDG_CACHE_HOME = path.join(isolated, "cache");
  process.env.XDG_DATA_HOME = path.join(isolated, "data");
  let transport;
  try {
    const version = execFileSync(
      python,
      ["-c", "import importlib.metadata; print(importlib.metadata.version('tongs'))"],
      { encoding: "utf8" },
    ).trim();
    transport = new SidecarTransport(
      { pythonExecutable: python, coreVersion: version, safeCwd: isolated },
      2_000,
      5_000,
      2_000,
    );
    await transport.start();
    assert.equal(transport.sessionGeneration, 1);
    assert.ok(transport.processId);
    const result = await transport.requestRead("assets.list", {}).result;
    assert.ok(Array.isArray(result.assets));
  } finally {
    await transport?.stop();
    for (const [key, value] of Object.entries(original)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    await rm(isolated, { recursive: true, force: true });
  }
});
