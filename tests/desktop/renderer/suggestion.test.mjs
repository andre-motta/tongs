import assert from "node:assert/strict";
import test from "node:test";

import {
  computeSuggestionFence,
  formatSuggestionBody,
  prepareSuggestionTarget,
  suggestionDisabledReason,
} from "../../../desktop/dist/src/renderer/features/review/suggestion.js";

test("suggestion blocks match GitHub and GitLab syntax with collision-safe fences", () => {
  assert.equal(computeSuggestionFence("const fence = ```;"), "````");
  assert.equal(
    formatSuggestionBody("  keep_indent()  ", 1, "github", "Use this"),
    "Use this\n\n```suggestion\n  keep_indent()  \n```",
  );
  assert.equal(
    formatSuggestionBody("first\n```inside\nlast", 3, "gitlab"),
    "````suggestion:-0+2\nfirst\n```inside\nlast\n````",
  );
});

test("single and multiline targets preserve original source and forge anchors", () => {
  const single = selection([source(null, 7, "  final_line()", "addition")]);
  assert.equal(suggestionDisabledReason(single, "github"), null);
  assert.deepEqual(prepareSuggestionTarget(single, "github"), {
    originalCode: "  final_line()",
    originalLineCount: 1,
    mutationAnchor: {
      old_path: "old.py",
      new_path: "new.py",
      line: 7,
      side: "RIGHT",
    },
    draftSelection: {
      review: "review",
      revision,
      oldPath: "old.py",
      newPath: "new.py",
      side: "new",
      oldLine: null,
      newLine: 7,
      startLine: null,
      startSide: null,
      contextLines: ["before", "  final_line()"],
      contextComplete: true,
    },
  });

  const lines = [
    source(10, 10, "first", "context"),
    source(null, 11, "second", "addition"),
    source(11, 12, "third", "context"),
  ];
  const range = selection(lines);
  assert.deepEqual(prepareSuggestionTarget(range, "github").mutationAnchor, {
    old_path: "old.py",
    new_path: "new.py",
    line: 12,
    side: "RIGHT",
    start_line: 10,
    start_side: "RIGHT",
  });
  const gitlab = prepareSuggestionTarget(range, "gitlab");
  assert.deepEqual(gitlab.mutationAnchor, {
    old_path: "old.py",
    new_path: "new.py",
    line: 10,
    side: "RIGHT",
  });
  assert.equal(gitlab.draftSelection.newLine, 10);
  assert.equal(gitlab.draftSelection.startLine, null);
  assert.equal(gitlab.originalCode, "first\nsecond\nthird");
});

test("suggestions reject stale sides, gaps, deletion rows, and partial source", () => {
  assert.match(
    suggestionDisabledReason({ ...selection([source(1, 1, "old")]), side: "old" }, "github"),
    /new side only/,
  );
  assert.match(
    suggestionDisabledReason(
      selection([source(1, 1, "one"), source(3, 3, "three")]),
      "github",
    ),
    /contiguous/,
  );
  assert.match(
    suggestionDisabledReason(selection([source(2, null, "gone", "deletion")]), "gitlab"),
    /new-side source lines/,
  );
  assert.match(
    suggestionDisabledReason(
      { ...selection([source(1, 1, "partial")]), contextComplete: false },
      "github",
    ),
    /partial/,
  );
});

const revision = { head_sha: "head", base_sha: "base", start_sha: null };

function source(oldLine, newLine, content, lineType = "context") {
  return { oldLine, newLine, content, lineType };
}

function selection(selectedLines) {
  const last = selectedLines.at(-1);
  return {
    review: "review",
    snapshotId: "snapshot",
    resource: "review",
    revision,
    fileIndex: 0,
    hunkIndex: 0,
    rowIndex: null,
    oldPath: "old.py",
    newPath: "new.py",
    side: "new",
    oldLine: last.oldLine,
    newLine: last.newLine,
    lineType: last.lineType,
    contextLines: ["before", ...selectedLines.map((line) => line.content)],
    contextComplete: true,
    rangeOriginOldLine: selectedLines[0].oldLine,
    rangeOriginNewLine: selectedLines[0].newLine,
    selectedLines,
  };
}
