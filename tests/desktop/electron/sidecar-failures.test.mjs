import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";
import {
  MAX_JSON_DEPTH,
  MAX_JSON_ITEMS,
  MAX_RESPONSE_BYTES,
  MIN_JSON_ITEMS,
  SidecarTransport,
} from "../../../desktop/dist/src/main/sidecar.js";
import { harness, launch, transportFor } from "./fake-sidecar.mjs";

test("trusted launch binds the sidecar to the exact editor export root", async () => {
  const fake = harness();
  const variable = "TONGS_DESKTOP_EDITOR_EXPORT_ROOT";
  const original = process.env[variable];
  process.env[variable] = "/tmp/renderer-selected-root";
  const trustedRoot = path.join(process.cwd(), ".trusted-editor-exports");
  const transport = new SidecarTransport(
    { ...launch, utilityExportRoot: trustedRoot },
    1_000,
    1_000,
    1_000,
    fake.spawn,
  );
  try {
    await transport.start();
    assert.equal(fake.children[0].spawnOptions.env[variable], trustedRoot);
  } finally {
    await transport.stop();
    if (original === undefined) delete process.env[variable];
    else process.env[variable] = original;
  }
});

test("launch without utility authority strips an inherited export root", async () => {
  const fake = harness();
  const variable = "TONGS_DESKTOP_EDITOR_EXPORT_ROOT";
  const original = process.env[variable];
  process.env[variable] = "/tmp/untrusted-editor-exports";
  const transport = transportFor(fake);
  try {
    await transport.start();
    assert.equal(fake.children[0].spawnOptions.env[variable], undefined);
  } finally {
    await transport.stop();
    if (original === undefined) delete process.env[variable];
    else process.env[variable] = original;
  }
});

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

test("mutation timeout is uncertain and never sends cancel or replays a late result", async () => {
  const fake = harness();
  const transport = new SidecarTransport(launch, 20, 1_000, 1_000, fake.spawn);
  await transport.start();
  const request = transport.requestMutation("pipelines.retry", {
    operation_id: "timeout-operation",
    pipeline: "pipeline-handle",
  });

  await assert.rejects(request.result, { code: "mutation_timeout" });
  assert.equal(
    fake.children[0].frames.filter((frame) => frame.type === "cancel").length,
    0,
  );
  assert.equal(
    fake.children[0].frames.filter((frame) => frame.method === "pipelines.retry").length,
    1,
  );

  fake.children[0].stdout.write(
    `${JSON.stringify({
      v: 1,
      type: "response",
      id: request.requestId,
      result: {
        operation_id: "timeout-operation",
        action: "retry_pipeline",
        outcome: "known",
        error: null,
        resync_required: false,
      },
    })}\n`,
  );
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(transport.crashHistory.length, 0);
  assert.equal(
    fake.children[0].frames.filter((frame) => frame.method === "pipelines.retry").length,
    1,
  );
  await transport.stop();
});

test("mutation cannot be cancelled through the read cancellation channel", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const request = transport.requestMutation("jobs.cancel", {
    operation_id: "cancel-channel-operation",
    pipeline: "pipeline-handle",
    job: "job-handle",
  });

  assert.equal(transport.cancelRead(request.requestId), false);
  assert.equal(
    fake.children[0].frames.filter((frame) => frame.type === "cancel").length,
    0,
  );
  fake.children[0].finish(8, null);
  await assert.rejects(request.result, { code: "unexpected_eof" });
});

test("mutation responses preserve validated service error classifications", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const mutation = transport.requestMutation("pipelines.retry", {
    operation_id: "known-rejection-operation",
    pipeline: "pipeline-handle",
  });
  fake.children[0].stdout.write(
    `${JSON.stringify({
      v: 1,
      type: "response",
      id: mutation.requestId,
      error: {
        code: "service_error",
        message: "safe server message",
        retryable: false,
        details: { service_code: "not_found" },
      },
    })}\n`,
  );
  await assert.rejects(mutation.result, { code: "not_found" });

  const read = transport.requestRead("pipelines.list", {});
  fake.children[0].stdout.write(
    `${JSON.stringify({
      v: 1,
      type: "response",
      id: read.requestId,
      error: {
        code: "service_error",
        message: "safe server message",
        retryable: false,
        details: { service_code: "not_found" },
      },
    })}\n`,
  );
  await assert.rejects(read.result, { code: "service_error" });
  await transport.stop();
});

test("sidecar disappearance leaves a dispatched mutation unknown", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const result = transport.requestMutation("jobs.retry", {
    operation_id: "disappearance-operation",
    pipeline: "pipeline-handle",
    job: "job-handle",
  }).result;

  fake.children[0].finish(9, null);
  await assert.rejects(result, { code: "unexpected_eof" });
  assert.equal(
    fake.children[0].frames.filter((frame) => frame.method === "jobs.retry").length,
    1,
  );
});

test("restart rejects an in-flight mutation and never replays it in the new session", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const result = transport.requestMutation("pipelines.cancel", {
    operation_id: "restart-operation",
    pipeline: "pipeline-handle",
  }).result;

  await transport.restart();
  await assert.rejects(result, { code: "shutting_down" });
  assert.equal(
    fake.children[0].frames.filter((frame) => frame.method === "pipelines.cancel").length,
    1,
  );
  assert.equal(
    fake.children[1].frames.filter((frame) => frame.method === "pipelines.cancel").length,
    0,
  );
  assert.equal(transport.sessionGeneration, 2);
  await transport.stop();
});

function respondTo(child, id, body) {
  child.stdout.write(`${JSON.stringify({ v: 1, type: "response", id, ...body })}\n`);
}

function nested(depth) {
  let value = "leaf";
  for (let level = 0; level < depth; level += 1) value = [value];
  return value;
}

function watchCrashes(transport) {
  const crashes = [];
  transport.on("crash", (error) => crashes.push(error.code));
  return crashes;
}

test("handshake declares the JSON value and depth limits", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  assert.deepEqual(fake.children[0].handshake.params.limits, {
    json_values: MAX_JSON_ITEMS,
    json_depth: MAX_JSON_DEPTH,
  });
  assert.deepEqual(transport.negotiatedJsonLimits, {
    values: MAX_JSON_ITEMS,
    depth: MAX_JSON_DEPTH,
  });
  await transport.stop();
});

test("over-budget response fails only its request and other reads survive", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const crashes = watchCrashes(transport);
  const threads = transport.requestRead("discussions.list", { review: "review-handle" });
  const commits = transport.requestRead("commits.list", { review: "review-handle" });
  const deep = transport.requestRead("jobs.list", { pipeline: "pipeline-handle" });
  const child = fake.children[0];

  respondTo(child, threads.requestId, {
    result: { discussions: new Array(MAX_JSON_ITEMS).fill(0) },
  });
  await assert.rejects(threads.result, {
    code: "response_too_large",
    message: /too large to show/,
    retryable: false,
  });
  respondTo(child, deep.requestId, { result: nested(MAX_JSON_DEPTH) });
  await assert.rejects(deep.result, { code: "response_too_large" });
  respondTo(child, commits.requestId, { result: { commits: [] } });
  assert.deepEqual(await commits.result, { commits: [] });

  const later = transport.requestRead("reviews.list", {});
  respondTo(child, later.requestId, { result: { items: [], failures: [] } });
  assert.deepEqual(await later.result, { items: [], failures: [] });
  assert.equal(crashes.length, 0);
  assert.equal(transport.crashHistory.length, 0);
  assert.equal(fake.children.length, 1);
  assert.equal(child.killed, false);
  await transport.stop();
});

test("response exactly at the value budget is delivered", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const read = transport.requestRead("commits.list", { review: "review-handle" });
  // The envelope holds the object, v, type, id, and the result list itself.
  const items = new Array(MAX_JSON_ITEMS - 5).fill(0);
  respondTo(fake.children[0], read.requestId, { result: items });
  assert.equal((await read.result).length, items.length);
  await transport.stop();
});

test("over-budget response for an unknown request is dropped without a crash", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const crashes = watchCrashes(transport);
  respondTo(fake.children[0], "electron-1-999", {
    result: new Array(MAX_JSON_ITEMS).fill(0),
  });
  const read = transport.requestRead("reviews.list", {});
  respondTo(fake.children[0], read.requestId, { result: { items: [] } });
  assert.deepEqual(await read.result, { items: [] });
  assert.equal(crashes.length, 0);
  await transport.stop();
});

test("over-budget frame without a request id still fails the connection", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const crashes = watchCrashes(transport);
  const pending = transport.requestRead("reviews.list", {});
  fake.children[0].stdout.write(
    `${JSON.stringify({ v: 1, type: "event", sequence: 1, event: "x", data: nested(MAX_JSON_DEPTH) })}\n`,
  );
  await assert.rejects(pending.result, { code: "invalid_frame" });
  assert.deepEqual(crashes, ["invalid_frame"]);
});

test("unparseable frame still fails every pending request", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const crashes = watchCrashes(transport);
  const first = transport.requestRead("reviews.list", {});
  const second = transport.requestRead("commits.list", { review: "review-handle" });
  fake.children[0].stdout.write(`{"v":1,"type":"response","id":"${first.requestId}"\n`);
  await assert.rejects(first.result, { code: "invalid_frame" });
  await assert.rejects(second.result, { code: "invalid_frame" });
  assert.deepEqual(crashes, ["invalid_frame"]);
});

test("negotiated tighter limits apply to responses and requests", async () => {
  const fake = harness({ limitOverrides: { json_values: MIN_JSON_ITEMS, json_depth: 12 } });
  const transport = transportFor(fake);
  await transport.start();
  assert.deepEqual(transport.negotiatedJsonLimits, { values: MIN_JSON_ITEMS, depth: 12 });
  const crashes = watchCrashes(transport);
  const read = transport.requestRead("commits.list", { review: "review-handle" });
  respondTo(fake.children[0], read.requestId, { result: new Array(MIN_JSON_ITEMS).fill(0) });
  await assert.rejects(read.result, { code: "response_too_large" });
  assert.throws(() => transport.requestRead("reviews.list", { deep: nested(12) }));
  assert.equal(crashes.length, 0);
  await transport.stop();
});

for (const [name, overrides] of [
  ["missing value limit", { json_values: undefined }],
  ["missing depth limit", { json_depth: undefined }],
  ["looser value limit", { json_values: MAX_JSON_ITEMS + 1 }],
  ["looser depth limit", { json_depth: MAX_JSON_DEPTH + 1 }],
  ["value limit below the floor", { json_values: MIN_JSON_ITEMS - 1 }],
  ["fractional depth limit", { json_depth: 12.5 }],
  ["textual value limit", { json_values: "20000" }],
]) {
  test(`handshake with a ${name} fails closed`, async () => {
    const fake = harness({ limitOverrides: overrides });
    const transport = transportFor(fake);
    await assert.rejects(transport.start(), {
      code: "incompatible_handshake",
      message: /JSON limits are incompatible/,
    });
  });
}
