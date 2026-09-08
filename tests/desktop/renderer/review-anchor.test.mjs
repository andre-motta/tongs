import assert from "node:assert/strict";
import test from "node:test";

import {
  splitSourceContext,
  unifiedSourceContext,
} from "../../../desktop/dist/src/renderer/features/diff/index.js";

const revision = { head_sha: "head", base_sha: "base", start_sha: null };
const file = {
  kind: "file",
  file_index: 0,
  old_path: "old.py",
  new_path: "new.py",
  status: "modified",
  additions: 1,
  deletions: 1,
  is_binary: false,
  language: "python",
  is_truncated: false,
  is_empty: false,
  is_mode_only: false,
  is_unavailable: false,
};

test("unified and split source context match by selected source side", () => {
  const unifiedRows = [
    line(0, 1, 1, "α", "context"),
    line(1, 2, 2, "before", "context"),
    line(2, 3, null, "deleted", "deletion"),
    line(3, null, 3, "新 line", "addition"),
    line(4, 4, 4, "after", "context"),
    line(5, 5, 5, "tail", "context"),
  ];
  const splitRows = [
    split(0, cell(1, 1, "α", "context", "old"), cell(1, 1, "α", "context", "new")),
    split(1, cell(2, 2, "before", "context", "old"), cell(2, 2, "before", "context", "new")),
    split(2, cell(3, null, "deleted", "deletion", "old"), cell(null, 3, "新 line", "addition", "new")),
    split(3, cell(4, 4, "after", "context", "old"), cell(4, 4, "after", "context", "new")),
    split(4, cell(5, 5, "tail", "context", "old"), cell(5, 5, "tail", "context", "new")),
  ];
  const unified = loaded("unified", [file, ...unifiedRows]);
  const splitLoaded = loaded("split", [file, ...splitRows]);

  assert.deepEqual(
    unifiedSourceContext(unified, file, unifiedRows[2], "old"),
    splitSourceContext(splitLoaded, file, splitRows[2], "old"),
  );
  assert.deepEqual(
    unifiedSourceContext(unified, file, unifiedRows[3], "new"),
    splitSourceContext(splitLoaded, file, splitRows[2], "new"),
  );
  assert.deepEqual(
    unifiedSourceContext(unified, file, unifiedRows[2], "old").lines,
    ["α", "before", "deleted", "after", "tail"],
  );
  assert.deepEqual(
    unifiedSourceContext(unified, file, unifiedRows[3], "new").lines,
    ["α", "before", "新 line", "after", "tail"],
  );
});

test("context stays within the hunk, respects boundaries, and flags partial snapshots", () => {
  const rows = [
    line(0, 1, 1, "first", "context"),
    line(1, 2, 2, "second", "context"),
    line(2, 3, 3, "third", "context"),
    { ...line(3, 4, 4, "other hunk", "context"), hunk_index: 1 },
  ];
  const complete = loaded("unified", [file, ...rows]);
  assert.deepEqual(
    unifiedSourceContext(complete, file, rows[0], "new"),
    { lines: ["first", "second", "third"], complete: true },
  );
  const partialFile = { ...file, is_truncated: true };
  const partial = loaded("unified", [partialFile, ...rows]);
  assert.deepEqual(
    unifiedSourceContext(partial, partialFile, rows[1], "new"),
    { lines: ["first", "second", "third"], complete: false },
  );
  const unavailable = loaded("unified", [file, ...rows], { code: "pagination_limit" });
  assert.equal(unifiedSourceContext(unavailable, file, rows[1], "old").complete, false);
});

test("opposite-only and empty split cells never enter selected-side context", () => {
  const rows = [
    split(0, cell(1, null, "old only", "deletion", "old"), null),
    split(1, null, cell(null, 1, "new only", "addition", "new")),
    split(2, cell(2, 2, "shared", "context", "old"), cell(2, 2, "shared", "context", "new")),
  ];
  const value = loaded("split", [file, ...rows]);
  assert.deepEqual(splitSourceContext(value, file, rows[0], "old").lines, ["old only", "shared"]);
  assert.deepEqual(splitSourceContext(value, file, rows[1], "new").lines, ["new only", "shared"]);
});

function loaded(layout, rows, partialError = null) {
  return {
    layout,
    snapshotId: "snapshot",
    resource: "review",
    revision,
    rows,
    partialError,
  };
}

function line(index, oldLine, newLine, content, lineType) {
  return {
    kind: "line",
    file_index: 0,
    hunk_index: 0,
    old_line: oldLine,
    new_line: newLine,
    content,
    line_type: lineType,
    _index: index,
  };
}

function cell(oldLine, newLine, content, lineType, anchorSide) {
  return {
    old_line: oldLine,
    new_line: newLine,
    content,
    line_type: lineType,
    anchor_side: anchorSide,
  };
}

function split(rowIndex, old, newer) {
  return {
    kind: "split",
    file_index: 0,
    hunk_index: 0,
    row_index: rowIndex,
    old,
    new: newer,
  };
}

