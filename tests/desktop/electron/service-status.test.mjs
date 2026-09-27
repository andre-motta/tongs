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
  // The warnings belong to the app run, so a stopped service keeps them.
  assert.deepEqual(publisher.current, { state: "stopped", revision: 2, notices: [warning] });
  publisher.dispose();
});

test("notices accumulate across sessions of one app run without repeats", async () => {
  const first = "Check github.com/acme/widgets #12.";
  const second = "Check gitlab.example.com/group/app #3.";
  const extras = { recovery_warnings: [first] };
  const fake = harness({ handshakeExtras: extras });
  const transport = transportFor(fake);
  const { publisher } = record(transport);
  await transport.start();
  delete extras.recovery_warnings;
  await transport.restart();
  assert.deepEqual([...transport.serviceNotices], [first]);
  extras.recovery_warnings = [second, first];
  await transport.restart();
  assert.deepEqual([...transport.serviceNotices], [first, second]);
  assert.deepEqual(publisher.current.notices, [first, second]);
  extras.recovery_warnings = Array.from({ length: 30 }, (_, index) => `Check #${index}.`);
  await transport.restart();
  assert.equal(transport.serviceNotices.length, 20);
  assert.deepEqual(transport.serviceNotices.slice(0, 2), [first, second]);
  publisher.dispose();
  await transport.stop();
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
  ["a null item", ["warning", null]],
]) {
  test(`a handshake whose recovery warnings are ${label} is incompatible`, async () => {
    const fake = harness({ handshakeExtras: { recovery_warnings: value } });
    const transport = transportFor(fake);
    await assert.rejects(transport.start(), { code: "incompatible_handshake" });
    assert.equal(transport.serviceState, "stopped");
    assert.deepEqual([...transport.serviceNotices], []);
  });
}

async function noticesFor(warnings) {
  const fake = harness({ handshakeExtras: { recovery_warnings: warnings } });
  const transport = transportFor(fake);
  await transport.start();
  const notices = [...transport.serviceNotices];
  assert.equal(transport.serviceState, "connected");
  await transport.stop();
  return notices;
}

test("empty recovery warnings are dropped and too many are cut to the bound", async () => {
  assert.deepEqual(await noticesFor(["", "warning"]), ["warning"]);
  const many = Array.from({ length: 21 }, (_, index) => `warning ${index}`);
  assert.deepEqual(await noticesFor(many), many.slice(0, 20));
});

test("an over-long ASCII recovery warning is cut, not rejected", async () => {
  const [notice] = await noticesFor(["x".repeat(1001)]);
  assert.equal(notice.length, 1000);
  assert.equal(notice, `${"x".repeat(999)}…`);
});

// U+1F600 is one code point but two UTF-16 units, matching Python's bound.
const ASTRAL = "\u{1F600}";

test("an astral recovery warning at 1000 UTF-16 units is kept whole", async () => {
  const exact = ASTRAL.repeat(500);
  assert.equal(exact.length, 1000);
  assert.deepEqual(await noticesFor([exact]), [exact]);
  const odd = `a${ASTRAL.repeat(499)}b`;
  assert.deepEqual(await noticesFor([odd]), [odd]);
});

for (const prefix of ["", "a", "ab"]) {
  test(`an astral recovery warning over the bound is cut on a code point (prefix ${prefix.length})`, async () => {
    const [notice] = await noticesFor([`${prefix}${ASTRAL.repeat(501)}`]);
    assert.ok(notice.length <= 1000, `length ${notice.length}`);
    assert.ok(notice.length >= 999, `length ${notice.length}`);
    assert.ok(notice.endsWith("…"));
    assert.ok(notice.isWellFormed(), "no lone surrogate");
    assert.ok(notice.startsWith(prefix));
  });
}
