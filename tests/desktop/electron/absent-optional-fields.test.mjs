import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import {
  decodeReadFailure,
  encodeReadFailure,
} from "../../../desktop/dist/src/shared/bridge.js";
import { assertResult } from "../../../desktop/dist/src/main/security.js";

const fixture = JSON.parse(
  readFileSync(
    path.join(import.meta.dirname, "../fixtures/diff-shapes.json"),
    "utf8",
  ),
);

function comment(overrides) {
  return {
    id: "note-1",
    author: { username: "reviewer", display_name: "" },
    body: "looks good",
    created_at: "2026-09-08T12:00:00+00:00",
    file_path: null,
    old_line: null,
    new_line: null,
    is_resolved: false,
    replies: [],
    ...overrides,
  };
}

function discussions(root) {
  return {
    discussions: [
      {
        id: "53f505e47aca",
        is_inline: false,
        root_comment: comment(root),
        is_resolved: false,
        resolvable: true,
      },
    ],
  };
}

test("every sandbox diff shape passes the read boundary on both forges", () => {
  for (const key of ["github_unified_page", "gitlab_unified_page"]) {
    const page = fixture[key];
    const files = page.entries.filter((entry) => entry.kind === "file");
    assert.equal(files.length, 8, `${key} must carry all eight shapes`);
    assert.equal(
      files.filter((file) => file.language === null).length,
      1,
      `${key} must carry the file with no detected language`,
    );
    assertResult("diff.open", page);
    assertResult("diff.page", page);
  }
});

test("a file row naming an empty language is rejected with its cause", () => {
  const page = structuredClone(fixture.github_unified_page);
  page.entries[0].language = "";

  assert.throws(
    () => assertResult("diff.open", page),
    (error) => {
      assert.match(error.message, /Invalid diff\.open result/);
      assert.match(error.message, /"language" must be null or non-empty text/);
      return true;
    },
  );
});

test("a review-level note reads with no inline position", () => {
  assertResult("discussions.list", discussions({}));
  assertResult("discussions.list", discussions({ body: "" }));
  assertResult(
    "discussions.list",
    discussions({
      file_path: "src/calc.py",
      new_line: 26,
      replies: [comment({ id: "note-2", file_path: "src/calc.py", new_line: 26 })],
    }),
  );

  assert.throws(
    () => assertResult("discussions.list", discussions({ file_path: "" })),
    (error) => {
      assert.match(error.message, /Invalid discussions\.list result/);
      assert.match(error.message, /"file_path" must be null or non-empty text/);
      return true;
    },
  );
});

test("an empty job log reads as one empty row", () => {
  assertResult("logs.open", {
    snapshot_id: "snapshot",
    resource: "job",
    revision: {
      sha256:
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      byte_count: 0,
    },
    cursor: 0,
    next_cursor: null,
    entries: [{ text: "" }],
  });
});

test("a read failure keeps its code, retryability and cause across the boundary", () => {
  const encoded = encodeReadFailure({
    code: "snapshot_expired",
    message: "The resource snapshot is invalid or expired; refetch it.",
    retryable: true,
  });

  assert.deepEqual(decodeReadFailure(encoded), {
    code: "snapshot_expired",
    message: "The resource snapshot is invalid or expired; refetch it.",
    retryable: true,
  });
  // Electron rebuilds the rejection as a fresh Error around the message text.
  assert.deepEqual(
    decodeReadFailure(
      new Error(
        `Error invoking remote method 'tongs:diff.open': Error: ${encoded.message}`,
      ),
    ),
    {
      code: "snapshot_expired",
      message: "The resource snapshot is invalid or expired; refetch it.",
      retryable: true,
    },
  );
});

test("an untyped failure becomes a non-retryable invalid response", () => {
  const failure = decodeReadFailure(
    encodeReadFailure(new Error('Invalid diff.open result: field "language" must be null or non-empty text')),
  );

  assert.deepEqual(failure, {
    code: "invalid_response",
    message:
      'Invalid diff.open result: field "language" must be null or non-empty text',
    retryable: false,
  });
  assert.equal(decodeReadFailure(new Error("plain failure")), null);
  assert.equal(decodeReadFailure(null), null);
  assert.equal(
    decodeReadFailure(new Error("tongs-read-failure:%7Bnot-json")),
    null,
  );
});
