import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
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

/**
 * The discard the desktop now offers from the drawer and from the composer
 * overflow, carried out by the actual installed sidecar rather than a bridge
 * double, and checked where it matters: the durable SQLite store the TUI
 * writes to as well. `review.discard_draft` is a local write, so the fixture
 * forge here is only what mints a review handle and a revision; the draft row
 * is created, counted, discarded, and counted again straight out of the store
 * file, and once more after the sidecar has exited so the deletion is proved
 * durable rather than merely in flight.
 */
test("actual installed sidecar discards a review draft out of the durable store", async () => {
  const isolated = await mkdtemp(path.join(os.tmpdir(), "tongs-discard-"));
  const database = path.join(isolated, "drafts.db");
  const storedDrafts = () =>
    Number(
      execFileSync(
        python,
        [
          "-E",
          "-P",
          "-c",
          "import sqlite3,sys;print(sqlite3.connect(sys.argv[1]).execute('select count(*) from drafts').fetchone()[0])",
          database,
        ],
        { encoding: "utf8" },
      ).trim(),
    );
  try {
    await withFixtureSidecar(isolated, async (transport) => {
      const listed = await transport.requestRead("reviews.list", {
        scope: "all_open",
      }).result;
      assert.equal(listed.items.length, 1);
      const review = listed.items[0].handle;

      const created = await transport.requestMutation("drafts.create", {
        review,
        revision: {
          head_sha: "a".repeat(40),
          base_sha: "b".repeat(40),
          start_sha: null,
        },
        content: {
          body: "Discard me",
          verdict: null,
          comments: [
            {
              id: "33333333-3333-4333-8333-333333333333",
              kind: "general",
              body: "and this pending comment with me",
            },
          ],
        },
      }).result;
      assert.equal(created.state, "editable");
      assert.equal(created.body, "Discard me");
      assert.equal(created.comments.length, 1);
      assert.equal(storedDrafts(), 1);

      // The exact call the drawer button and the composer overflow entry make,
      // and the exact call the TUI makes from Shift+D.
      const discarded = await transport.requestMutation("drafts.discard", {
        review,
        draft_id: created.id,
        expected_version: created.version,
      }).result;
      assert.equal(discarded.discarded.id, created.id);
      assert.equal(discarded.discarded.version, created.version);
      assert.equal(storedDrafts(), 0);

      const remaining = await transport.requestRead("drafts.list", { review })
        .result;
      assert.equal(remaining.drafts.length, 0);
      await assert.rejects(
        transport.requestRead("drafts.get", {
          review,
          draft_id: created.id,
        }).result,
      );
    });
    // The sidecar has exited. What the store holds now is what a later
    // session, desktop or TUI, would find.
    assert.equal(storedDrafts(), 0);
  } finally {
    await rm(isolated, { recursive: true, force: true });
  }
});

/**
 * The production transport, started against the fixture sidecar module rather
 * than the production one. Everything above the session, the protocol server
 * and the durable draft store included, is the shipped code; the fixture is
 * only what mints a review handle and a revision without a forge.
 */
async function withFixtureSidecar(isolated, body) {
  const original = {
    XDG_CONFIG_HOME: process.env.XDG_CONFIG_HOME,
    XDG_CACHE_HOME: process.env.XDG_CACHE_HOME,
    XDG_DATA_HOME: process.env.XDG_DATA_HOME,
    TONGS_REVIEW_PROOF_EVIDENCE: process.env.TONGS_REVIEW_PROOF_EVIDENCE,
  };
  process.env.XDG_CONFIG_HOME = path.join(isolated, "config");
  process.env.XDG_CACHE_HOME = path.join(isolated, "cache");
  process.env.XDG_DATA_HOME = path.join(isolated, "data");
  process.env.TONGS_REVIEW_PROOF_EVIDENCE = isolated;
  const fixture = path.join(
    checkout,
    "tests/desktop/native/review_fixture_sidecar.py",
  );
  const version = execFileSync(
    python,
    ["-c", "import importlib.metadata; print(importlib.metadata.version('tongs'))"],
    { encoding: "utf8" },
  ).trim();
  const transport = new SidecarTransport(
    { pythonExecutable: python, coreVersion: version, safeCwd: isolated },
    2_000,
    10_000,
    5_000,
    (executable, _arguments, options) =>
      spawn(executable, ["-E", "-P", fixture], options),
  );
  try {
    await transport.start();
    await body(transport);
  } finally {
    await transport.stop();
    for (const [key, value] of Object.entries(original)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
}
