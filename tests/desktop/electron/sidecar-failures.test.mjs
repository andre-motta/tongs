import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import test from "node:test";
import {
  MAX_RESPONSE_BYTES,
  SidecarTransport,
} from "../../../desktop/dist/src/main/sidecar.js";

const caps = [
  "assets",
  "cancellation",
  "events",
  "opaque_handles",
  "paged_diffs",
  "paged_logs",
  "plugins",
];
const methods = [
  "assets.list",
  "assets.read",
  "commits.list",
  "diff.open",
  "diff.page",
  "discussions.list",
  "host.set_location",
  "jobs.list",
  "logs.open",
  "logs.page",
  "pipelines.list",
  "plugins.invoke",
  "plugins.list",
  "repositories.discover",
  "repositories.open",
  "review_pipelines.list",
  "reviews.get",
  "reviews.list",
  "shutdown",
];

function harness({
  autoHandshake = true,
  delayedKill = false,
  delayedShutdown = false,
  extraCapabilities = [],
} = {}) {
  const children = [];
  const respond = (child, frame) => {
    const result = {
      protocol_major: 1,
      core_version: "1.0",
      session_id: "1234567890abcdef",
      capabilities: [...caps, ...extraCapabilities],
      accepted_capabilities: caps,
      methods,
      limits: {
        request_frame_bytes: 262144,
        response_frame_bytes: 8388608,
        event_frame_bytes: 65536,
        pending_requests: 64,
        queued_events: 256,
        asset_chunk_bytes: 524288,
      },
    };
    child.stdout.write(
      `${JSON.stringify({ v: 1, type: "response", id: frame.id, result })}\n`,
    );
  };
  const spawn = () => {
    const child = new EventEmitter();
    children.push(child);
    child.stdin = new PassThrough();
    child.stdout = new PassThrough();
    child.stderr = new PassThrough();
    child.pid = 4000 + children.length;
    child.killed = false;
    child.exitCode = null;
    child.signalCode = null;
    child.frames = [];
    child.finish = (code = 0, signal = null) => {
      child.exitCode = code;
      child.signalCode = signal;
      child.emit("exit", code, signal);
    };
    child.kill = (signal) => {
      child.killed = true;
      if (!delayedKill) queueMicrotask(() => child.finish(null, signal));
      return true;
    };
    let input = "";
    child.stdin.on("data", (chunk) => {
      input += chunk;
      while (input.includes("\n")) {
        const newline = input.indexOf("\n");
        const frame = JSON.parse(input.slice(0, newline));
        input = input.slice(newline + 1);
        child.frames.push(frame);
        if (frame.method === "handshake") {
          child.handshake = frame;
          if (autoHandshake) respond(child, frame);
        }
        if (frame.method === "shutdown" && !delayedShutdown) {
          child.stdout.write(
            `${JSON.stringify({ v: 1, type: "response", id: frame.id, result: {} })}\n`,
          );
          queueMicrotask(() => child.finish(0, null));
        }
      }
    });
    return child;
  };
  return {
    spawn,
    children,
    respond: (child) => respond(child, child.handshake),
  };
}

const launch = {
  pythonExecutable: process.execPath,
  coreVersion: "1.0",
  safeCwd: process.cwd(),
};

function transportFor(fake, shutdownTimeout = 1_000) {
  return new SidecarTransport(
    launch,
    1_000,
    1_000,
    shutdownTimeout,
    fake.spawn,
  );
}

test("unexpected EOF fails every pending read and records one crash", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const pending = transport.requestRead("assets.list", {}).result;
  fake.children[0].finish(7, null);
  await assert.rejects(pending, { code: "unexpected_eof" });
  assert.equal(transport.crashHistory.length, 1);
});

test("oversized unterminated output terminates the connection", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const pending = transport.requestRead("assets.list", {}).result;
  fake.children[0].stdout.write(Buffer.alloc(MAX_RESPONSE_BYTES + 1, 0x20));
  await assert.rejects(pending, { code: "frame_too_large" });
});

test("bounded shutdown rejects pending reads and exits cleanly", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const pending = transport.requestRead("assets.list", {}).result;
  const stopped = transport.stop();
  await assert.rejects(pending, { code: "shutting_down" });
  await stopped;
  assert.equal(transport.crashHistory.at(-1).unexpected, false);
});

test("concurrent starts share the incomplete handshake", async () => {
  const fake = harness({ autoHandshake: false });
  const transport = transportFor(fake);
  let secondReady = false;
  const first = transport.start();
  const second = transport.start().then(() => {
    secondReady = true;
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(fake.children.length, 1);
  assert.equal(secondReady, false);
  fake.respond(fake.children[0]);
  await Promise.all([first, second]);
  await transport.stop();
});

test("stop during startup settles before any replacement", async () => {
  const fake = harness({ autoHandshake: false });
  const transport = transportFor(fake);
  const starting = transport.start();
  await new Promise((resolve) => setImmediate(resolve));
  const stopping = transport.stop();
  await assert.rejects(starting);
  await stopping;
  assert.equal(fake.children.length, 1);
  assert.equal(transport.processId, undefined);
});

test("stale child events cannot corrupt a replacement generation", async () => {
  const fake = harness({ delayedKill: true });
  const transport = transportFor(fake, 20);
  await transport.start();
  const old = fake.children[0];
  const pending = transport.requestRead("assets.list", {}).result;
  old.stdout.write("not-json\n");
  await assert.rejects(pending, { code: "invalid_frame" });
  const replacement = transport.start();
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal(fake.children.length, 1);
  old.finish(9, "SIGTERM");
  await replacement;
  const current = fake.children[1];
  assert.equal(transport.processId, current.pid);
  old.stdout.write("garbage\n");
  old.emit("exit", 9, "SIGTERM");
  assert.equal(transport.processId, current.pid);
  assert.equal(transport.sessionGeneration, 2);
  current.finish(0, null);
});

test("restart stops the prior session before a new handshake", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  await transport.restart();
  assert.equal(fake.children.length, 2);
  assert.equal(transport.sessionGeneration, 2);
  await transport.stop();
});

test("terminal stop rejects a concurrent start without replacement", async () => {
  const fake = harness({ delayedKill: true, delayedShutdown: true });
  const transport = transportFor(fake, 20);
  await transport.start();
  const old = fake.children[0];
  const stopping = transport.stop();
  const starting = assert.rejects(transport.start(), { code: "shutting_down" });
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal(fake.children.length, 1);
  old.finish(0, null);
  await stopping;
  await starting;
  assert.equal(fake.children.length, 1);
  assert.equal(transport.processId, undefined);
});

test("terminal stop prevents an in-progress restart from resurrecting", async () => {
  const fake = harness({ delayedKill: true, delayedShutdown: true });
  const transport = transportFor(fake, 20);
  await transport.start();
  const old = fake.children[0];
  const restarting = transport.restart();
  const stopping = transport.stop();
  old.finish(0, null);
  await stopping;
  await assert.rejects(restarting, { code: "shutting_down" });
  assert.equal(fake.children.length, 1);
  assert.equal(transport.processId, undefined);
});

test("handshake accepts server capability supersets", async () => {
  const fake = harness({ extraCapabilities: ["split_diffs"] });
  const transport = transportFor(fake);
  await transport.start();
  assert.equal(transport.sessionGeneration, 1);
  await transport.stop();
});
