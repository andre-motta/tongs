import assert from "node:assert/strict";
import test from "node:test";
import { QueryCoordinator } from "../../../desktop/dist/src/renderer/core/query.js";
import { loadAllPages } from "../../../desktop/dist/src/renderer/features/diff/index.js";

const revision = { head_sha: "head", base_sha: "base", start_sha: null };

test("diff reconstruction follows next_cursor across page boundaries", async () => {
  const calls = [];
  const bridge = {
    cancelRead: async () => true,
    openDiff: (params) => {
      calls.push(["open", params]);
      return read("open", {
        snapshot_id: "s1",
        resource: "review",
        revision,
        cursor: 0,
        next_cursor: 9,
        entries: [
          {
            kind: "file",
            file_index: 0,
            old_path: "a.py",
            new_path: "a.py",
            status: "modified",
            additions: 1,
            deletions: 1,
            is_binary: false,
            language: "python",
            is_truncated: false,
            is_empty: false,
            is_mode_only: false,
            is_unavailable: false,
          },
        ],
      });
    },
    pageDiff: (params) => {
      calls.push(["page", params]);
      return read("page", {
        snapshot_id: "s1",
        resource: "review",
        revision,
        cursor: 9,
        next_cursor: null,
        entries: [
          {
            kind: "hunk",
            file_index: 0,
            hunk_index: 0,
            header: "@@ -1 +1 @@",
            old_start: 1,
            old_count: 1,
            new_start: 1,
            new_count: 1,
            context_text: "",
          },
        ],
      });
    },
  };
  const result = await loadAllPages(
    "review",
    "split",
    bridge,
    new QueryCoordinator(bridge),
  );
  assert.equal(result.layout, "split");
  assert.equal(result.rows.length, 2);
  assert.deepEqual(calls, [
    ["open", { review: "review", layout: "split", max_items: 1000 }],
    [
      "page",
      { snapshot: "s1", resource: "review", cursor: 9, max_items: 1000 },
    ],
  ]);
});

test("diff reconstruction rejects a revision change between pages", async () => {
  const bridge = {
    cancelRead: async () => true,
    openDiff: () =>
      read("open", {
        snapshot_id: "s1",
        resource: "review",
        revision,
        cursor: 0,
        next_cursor: 1,
        entries: [],
      }),
    pageDiff: () =>
      read("page", {
        snapshot_id: "s1",
        resource: "review",
        revision: { ...revision, head_sha: "changed" },
        cursor: 1,
        next_cursor: null,
        entries: [],
      }),
  };
  await assert.rejects(
    loadAllPages("review", "unified", bridge, new QueryCoordinator(bridge)),
    /revision_changed/,
  );
});

function read(requestToken, value) {
  return { requestToken, result: Promise.resolve(value) };
}
