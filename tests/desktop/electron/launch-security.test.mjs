import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { parseLaunchArguments, SIDECAR_ARGUMENTS } from "../../../desktop/dist/src/main/launch.js";
import { assertAuthorizedSender, assertHttpsExternalUrl, assertParams, assertResult, isAllowedAppUrl } from "../../../desktop/dist/src/main/security.js";

test("launcher accepts only exact absolute interpreter and safe cwd", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "tongs-launch-"));
  const executable = path.join(root, "python"); await writeFile(executable, "#!/bin/sh\n", { mode: 0o700 });
  const value = parseLaunchArguments(["--tongs-python-executable", executable, "--tongs-core-version=1.2.3", "--tongs-safe-cwd", root]);
  assert.equal(value.pythonExecutable, executable); assert.equal(value.safeCwd, root);
  assert.deepEqual([...SIDECAR_ARGUMENTS], ["-E", "-P", "-m", "tongs.desktop.sidecar"]);
  assert.throws(() => parseLaunchArguments(["--tongs-python-executable=python", "--tongs-core-version=1", `--tongs-safe-cwd=${root}`]));
  assert.throws(() => parseLaunchArguments([`--tongs-python-executable=${executable}`, "--tongs-core-version=1", `--tongs-safe-cwd=${root}`, "--tongs-core-version=2"]));
  assert.doesNotThrow(() => parseLaunchArguments([`--tongs-python-executable=${executable}`, "--tongs-core-version=1", `--tongs-safe-cwd=${root}`, "--tongs-smoke-report=/tmp/report.json", "--tongs-smoke-review-number=69"]));
});

test("runtime parameter boundary rejects unknown, malformed, and oversized values", () => {
  assertParams("reviews.list", { scope: "all_open", per_page: 100 });
  assertParams("diff.open", { review: "review", layout: "split", max_items: 1000 });
  assert.throws(() => assertParams("diff.open", { review: "review", layout: "sideways" }));
  assert.throws(() => assertParams("reviews.list", { scope: "all_open", surprise: true }));
  assert.throws(() => assertParams("diff.page", { snapshot: "s", resource: "r", cursor: -1 }));
  assert.throws(() => assertParams("reviews.list", { scope: "all_open", per_page: 101 }));
  assert.throws(() => assertParams("reviews.list", { scope: "everything" }));
  assert.throws(() => assertParams("plugins.invoke", { plugin: "p", method: "m", params: { body: "x".repeat(300_000) } }));
});

test("runtime result boundary validates operation-specific DTOs", () => {
  assertResult("repositories.discover", { repositories: [{ handle: "r", display_name: "repo", forge_type: "github" }] });
  assert.throws(() => assertResult("repositories.discover", { repositories: [{ handle: "r", display_name: "repo", forge_type: "github", filesystem_path: "/secret" }] }));
  assert.throws(() => assertResult("jobs.list", { jobs: [{ handle: "j", value: { id: 1, name: "test" } }] }));
  assert.throws(() => assertResult("host.set_location", { accepted: false }));
  assertResult("diff.open", {
    snapshot_id: "snapshot", resource: "review", cursor: 0, next_cursor: null,
    revision: { head_sha: "head", base_sha: "base", start_sha: null },
    entries: [{
      kind: "split", file_index: 0, hunk_index: 0, row_index: 0,
      old: { old_line: 1, new_line: null, content: "old", line_type: "deletion", anchor_side: "old" },
      new: { old_line: null, new_line: 1, content: "new", line_type: "addition", anchor_side: "new" },
    }],
  });
  assert.throws(() => assertResult("plugins.list", {
    plugins: [{ plugin_id: "sample", state: "started", has_terminal_entry_point: false, has_desktop_entry_point: true, error: null, manifest: {} }],
  }));
  assertResult("plugins.list", {
    plugins: [{
      plugin_id: "sample", state: "started", has_terminal_entry_point: false,
      has_desktop_entry_point: true, error: null,
      manifest: {
        title: "Sample", version: "1.0", api_major: 1,
        modules: [{ id: "main", title: "Main", entry_asset: "asset", stylesheets: ["style"] }],
        navigation: [{ id: "home", title: "Home", module_id: "main" }],
        commands: [{ id: "open", title: "Open", navigation_id: "home", help_text: "" }],
        methods: ["refresh"], events: ["changed"],
        focus_targets: [{ id: "item", title: "Item", module_id: "main" }],
        help_asset: null, reads: ["reviews"], assets_available: true,
      },
    }],
  });
});

test("navigation and external URL policy is scheme exact", () => {
  assert.equal(isAllowedAppUrl("tongs://app/index.html"), true); assert.equal(isAllowedAppUrl("https://app/index.html"), false);
  assert.equal(isAllowedAppUrl("tongs://evil/index.html"), false); assert.equal(assertHttpsExternalUrl("https://example.com/review"), "https://example.com/review");
  assert.throws(() => assertHttpsExternalUrl("http://example.com")); assert.throws(() => assertHttpsExternalUrl("https://token@example.com"));
});

test("IPC sender must be the exact top-level authorized document", () => {
  const mainFrame = { url: "tongs://app/index.html" }; const owner = { mainFrame };
  assert.doesNotThrow(() => assertAuthorizedSender({ sender: owner, senderFrame: mainFrame }, owner));
  assert.throws(() => assertAuthorizedSender({ sender: owner, senderFrame: { url: mainFrame.url } }, owner));
  assert.throws(() => assertAuthorizedSender({ sender: owner, senderFrame: { url: "tongs://app/other" } }, owner));
});
