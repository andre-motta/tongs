import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import test from "node:test";
import { Sidecar } from "../src/sidecar.mjs";

const desktopDir = path.resolve(import.meta.dirname, "..", "..");
const fixtureDir = path.resolve(import.meta.dirname, "..", "fixture");
const python = process.env.TONGS_DESKTOP_PYTHON || "python3";

test("real Python sidecar supports concurrent IDs and clean EOF shutdown", async () => {
  const sidecar = new Sidecar({
    python,
    frontendDir: fixtureDir,
    backendDir: desktopDir,
    requestTimeoutMs: 2_000,
  });
  const url = await sidecar.start();
  assert.match(url, /^http:\/\/127\.0\.0\.1:\d+\/$/);
  const results = await Promise.all(Array.from({ length: 20 }, () => sidecar.request("health")));
  assert.ok(results.every((result) => result.fixture && result.protocol === "prototype-1"));
  const child = sidecar.child;
  await sidecar.stop();
  assert.notEqual(child.exitCode, null);
  assert.equal(sidecar.pending.size, 0);
});

test("request timeout removes the pending request", async () => {
  const temp = await mkdtemp(path.join(os.tmpdir(), "tongs-electron-timeout-"));
  await writeFile(
    path.join(temp, "backend.py"),
    "import json, time\nprint(json.dumps({'event':'ready','url':'http://127.0.0.1:1234/'}), flush=True)\nfor line in __import__('sys').stdin: time.sleep(2)\n",
  );
  const sidecar = new Sidecar({
    python,
    frontendDir: fixtureDir,
    backendDir: temp,
    requestTimeoutMs: 30,
    shutdownTimeoutMs: 30,
  });
  await sidecar.start();
  await assert.rejects(sidecar.request("health"), /timed out/);
  assert.equal(sidecar.pending.size, 0);
  await sidecar.stop();
  await rm(temp, { recursive: true, force: true });
});

test("startup timeout terminates a sidecar that never becomes ready", async () => {
  const temp = await mkdtemp(path.join(os.tmpdir(), "tongs-electron-startup-"));
  await writeFile(path.join(temp, "backend.py"), "import time\ntime.sleep(2)\n");
  const sidecar = new Sidecar({
    python,
    frontendDir: fixtureDir,
    backendDir: temp,
    startupTimeoutMs: 30,
  });
  await assert.rejects(sidecar.start(), /did not become ready/);
  if (sidecar.child) {
    await new Promise((resolve) => sidecar.child.once("exit", resolve));
  }
  await rm(temp, { recursive: true, force: true });
});

test("sidecar crash rejects outstanding requests and reports the crash", async () => {
  const temp = await mkdtemp(path.join(os.tmpdir(), "tongs-electron-crash-"));
  await writeFile(
    path.join(temp, "backend.py"),
    "import json, sys\nprint(json.dumps({'event':'ready','url':'http://127.0.0.1:1234/'}), flush=True)\nfor line in sys.stdin: raise SystemExit(7)\n",
  );
  const sidecar = new Sidecar({
    python,
    frontendDir: fixtureDir,
    backendDir: temp,
    requestTimeoutMs: 2_000,
  });
  const crashed = new Promise((resolve) => sidecar.once("crash", resolve));
  await sidecar.start();
  await assert.rejects(sidecar.request("health"), /exited unexpectedly/);
  await crashed;
  assert.equal(sidecar.pending.size, 0);
  await rm(temp, { recursive: true, force: true });
});
