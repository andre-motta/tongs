import assert from "node:assert/strict";
import test from "node:test";
import { ServiceStatusPublisher } from "../../../desktop/dist/src/main/sidecar.js";
import { harness, transportFor } from "./fake-sidecar.mjs";

function record(transport) {
  const published = [];
  const publisher = new ServiceStatusPublisher(transport, (status) =>
    published.push(`${status.state}@${status.revision}`),
  );
  publisher.start();
  return { published, publisher };
}

test("transport reports connected only after a completed handshake", async () => {
  const fake = harness({ autoHandshake: false });
  const transport = transportFor(fake);
  const states = [];
  transport.on("status", (state) => states.push(state));
  assert.equal(transport.serviceState, "stopped");
  const started = transport.start();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(transport.serviceState, "stopped");
  fake.respond(fake.children[0]);
  await started;
  assert.equal(transport.serviceState, "connected");
  assert.deepEqual(states, ["connected"]);
  await transport.stop();
  assert.deepEqual(states, ["connected", "stopped"]);
});

test("an unexpected exit publishes stopped and a restart publishes connected", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  const { published, publisher } = record(transport);
  await transport.start();
  fake.children[0].finish(9, null);
  assert.equal(transport.serviceState, "stopped");
  await transport.restart();
  assert.equal(transport.serviceState, "connected");
  assert.deepEqual(published, ["connected@1", "stopped@2", "connected@3"]);
  assert.deepEqual(publisher.current, { state: "connected", revision: 3, notices: [] });
  publisher.dispose();
  await transport.stop();
  assert.deepEqual(published, ["connected@1", "stopped@2", "connected@3"]);
});

test("a failed connection publishes stopped once", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const { published } = record(transport);
  const pending = transport.requestRead("assets.list", {}).result;
  fake.children[0].stdout.write("not json\n");
  await assert.rejects(pending, { code: "invalid_frame" });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(published, ["stopped@1"]);
  await transport.stop();
  assert.deepEqual(published, ["stopped@1"]);
});

test("the publisher starts from the transport state without publishing it", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const { published, publisher } = record(transport);
  assert.deepEqual(published, []);
  assert.deepEqual(publisher.current, { state: "connected", revision: 0, notices: [] });
  await transport.stop();
  assert.deepEqual(published, ["stopped@1"]);
});

test("handshake recovery warnings reach the status as notices for that session", async () => {
  const warning = "Part of the review may already have posted. Review: github.com/acme/widgets #12.";
  const fake = harness({ handshakeExtras: { recovery_warnings: [warning] } });
  const transport = transportFor(fake);
  const { published, publisher } = record(transport);
  await transport.start();
  assert.deepEqual(published, ["connected@1"]);
  assert.deepEqual(publisher.current, { state: "connected", revision: 1, notices: [warning] });
  assert.deepEqual([...transport.serviceNotices], [warning]);
  await transport.stop();
  // A stopped service shows no startup warnings of the session that ended.
  assert.deepEqual(publisher.current, { state: "stopped", revision: 2, notices: [] });
  publisher.dispose();
});

test("a handshake without recovery warnings has no notices", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  assert.deepEqual([...transport.serviceNotices], []);
  await transport.stop();
});

for (const [label, value] of [
  ["not a list", "check the forge"],
  ["a non-string item", [7]],
  ["an empty item", [""]],
  ["an oversized item", ["x".repeat(1001)]],
  ["too many items", new Array(21).fill("warning")],
]) {
  test(`a handshake whose recovery warnings are ${label} is incompatible`, async () => {
    const fake = harness({ handshakeExtras: { recovery_warnings: value } });
    const transport = transportFor(fake);
    await assert.rejects(transport.start(), { code: "incompatible_handshake" });
    assert.equal(transport.serviceState, "stopped");
    assert.deepEqual([...transport.serviceNotices], []);
  });
}
