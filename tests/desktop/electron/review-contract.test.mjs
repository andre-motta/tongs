import assert from "node:assert/strict";
import test from "node:test";

import {
  REVIEW_OPERATIONS,
  assertReviewParams,
  assertReviewResult,
} from "../../../desktop/dist/src/main/review.js";

const revision = { head_sha: "head", base_sha: "base", start_sha: null };
const operation = "review:1";
const review = "opaque-review";
const draftId = "11111111-1111-4111-8111-111111111111";
const attemptId = "22222222-2222-4222-8222-222222222222";
const commentId = "33333333-3333-4333-8333-333333333333";
const mutationAnchor = {
  old_path: "src/old.ts",
  new_path: "src/new.ts",
  line: 4,
  side: "RIGHT",
};
const draftAnchor = {
  revision,
  old_path: "src/old.ts",
  new_path: "src/new.ts",
  old_line: 3,
  new_line: 4,
  side: "new",
  context_fingerprint: "a".repeat(64),
  start_line: null,
  start_side: null,
};
const content = {
  body: "Summary",
  verdict: "comment",
  comments: [{ id: commentId, kind: "inline", body: "Note", anchor: draftAnchor }],
};
const draft = {
  id: draftId,
  review,
  revision,
  version: 1,
  ...content,
  comments: [
    {
      ...content.comments[0],
      anchor: { ...draftAnchor, stale: false },
    },
  ],
  state: "editable",
  created_at: "2026-09-08T00:00:00+00:00",
  updated_at: "2026-09-08T00:00:00+00:00",
};
const mutation = {
  operation_id: operation,
  outcome: "known",
  receipt: { remote_id: "remote", comment_id: "comment", discussion_id: null },
  reason: null,
  resync_required: false,
};
const actionReceipt = {
  operation_id: operation,
  action: "close",
  review,
  revision,
  expected_state: "open",
  outcome: "known",
  remote_id: "remote",
  merge_sha: null,
  source_cleanup: "not_requested",
  error: null,
  resync_required: true,
};
const submission = {
  attempt_id: attemptId,
  draft_id: draftId,
  review,
  frozen_version: 1,
  state: "partial",
  outcome: "paused",
  steps: [
    { id: "comment:0", kind: "inline_comment", comment_ids: [commentId] },
    { id: "verdict", kind: "verdict", comment_ids: [] },
  ],
  receipts: [
    {
      step_id: "comment:0",
      remote_id: "remote-comment",
      recorded_at: "2026-09-08T00:00:01+00:00",
      resync_required: false,
    },
  ],
  completed_step_ids: ["comment:0"],
  unknown_step_ids: [],
  atomic: false,
  resync_required: true,
  failure: {
    code: "authorization",
    message: "Permission denied.",
    retryable: false,
    step_id: "verdict",
  },
  plan_available: true,
};

test("review operation inventory is fixed, unique, and classifies all 22 RPC methods", () => {
  assert.equal(REVIEW_OPERATIONS.length, 22);
  assert.equal(new Set(REVIEW_OPERATIONS.map((item) => item.channel)).size, 22);
  assert.deepEqual(
    REVIEW_OPERATIONS.map((item) => item.method).sort(),
    [
      "drafts.create", "drafts.discard", "drafts.get", "drafts.list", "drafts.save",
      "review_actions.capabilities", "review_actions.close", "review_actions.merge",
      "review_actions.receipt", "review_actions.reopen", "review_actions.unapprove",
      "review_mutations.capabilities", "review_mutations.comment",
      "review_mutations.inline_comment", "review_mutations.reply",
      "review_mutations.resolve", "review_mutations.verdict",
      "review_submissions.list", "review_submissions.reconcile",
      "review_submissions.resume", "review_submissions.start",
      "review_submissions.status",
    ],
  );
  assert.equal(REVIEW_OPERATIONS.filter((item) => item.mutation).length, 15);
});

test("review parameter validators accept each fixed method and exact wire shape", () => {
  const specimens = new Map([
    ["review_mutations.capabilities", { review }],
    ["review_mutations.comment", { operation_id: operation, review, body: "Note" }],
    ["review_mutations.inline_comment", { operation_id: operation, review, revision, anchor: mutationAnchor, body: "Note" }],
    ["review_mutations.reply", { operation_id: operation, review, revision, discussion_id: "thread", body: "Reply" }],
    ["review_mutations.resolve", { operation_id: operation, review, revision, discussion_id: "thread", resolved: true }],
    ["review_mutations.verdict", { operation_id: operation, review, revision, verdict: "comment", body: "Body", inline_comments: [{ anchor: mutationAnchor, body: "Note" }] }],
    ["review_actions.capabilities", { review }],
    ["review_actions.merge", { operation_id: operation, review, revision, squash: true, source_cleanup: { branch: "feature" } }],
    ["review_actions.close", { operation_id: operation, review, revision }],
    ["review_actions.reopen", { operation_id: operation, review, revision }],
    ["review_actions.unapprove", { operation_id: operation, review, revision }],
    ["review_actions.receipt", { operation_id: operation, review, revision, action: "close" }],
    ["drafts.create", { review, revision, content }],
    ["drafts.get", { review, draft_id: draftId }],
    ["drafts.list", { review, states: ["editable", "unknown"], cursor: 0, max_items: 100 }],
    ["drafts.save", { review, draft_id: draftId, expected_version: 1, content }],
    ["drafts.discard", { review, draft_id: draftId, expected_version: 1 }],
    ["review_submissions.start", { review, draft_id: draftId, expected_version: 1 }],
    ["review_submissions.status", { review, attempt_id: attemptId }],
    ["review_submissions.list", { review, cursor: 0, max_items: 100 }],
    ["review_submissions.resume", { review, attempt_id: attemptId }],
    ["review_submissions.reconcile", { review, attempt_id: attemptId, resolution: "retry_remaining" }],
  ]);
  for (const item of REVIEW_OPERATIONS)
    assert.doesNotThrow(() => assertReviewParams(item.method, specimens.get(item.method)));
});

test("review result validators accept typed capabilities, receipts, drafts, and progress", () => {
  const mutationCapabilities = {
    review,
    capabilities: {
      general_comment: true, inline_comment: true, multiline_comment: true,
      reply: true, resolve: true, approve: true, request_changes: true,
      comment_verdict: true, atomic_review_batch: false,
    },
  };
  const actionCapabilities = {
    review,
    capabilities: { merge: true, close: true, reopen: false, unapprove: true },
  };
  assert.doesNotThrow(() => assertReviewResult("review_mutations.capabilities", mutationCapabilities));
  assert.doesNotThrow(() => assertReviewResult("review_actions.capabilities", actionCapabilities));
  for (const method of [
    "review_mutations.comment", "review_mutations.inline_comment",
    "review_mutations.reply", "review_mutations.resolve", "review_mutations.verdict",
  ]) assert.doesNotThrow(() => assertReviewResult(method, mutation));
  assert.doesNotThrow(() => assertReviewResult("review_actions.close", actionReceipt));
  assert.doesNotThrow(() => assertReviewResult("review_actions.receipt", { receipt: actionReceipt }));
  for (const method of ["drafts.create", "drafts.get", "drafts.save"])
    assert.doesNotThrow(() => assertReviewResult(method, draft));
  assert.doesNotThrow(() => assertReviewResult("drafts.discard", { discarded: draft }));
  assert.doesNotThrow(() => assertReviewResult("drafts.list", { cursor: 0, next_cursor: null, drafts: [draft] }));
  for (const method of [
    "review_submissions.start", "review_submissions.status",
    "review_submissions.resume", "review_submissions.reconcile",
  ]) assert.doesNotThrow(() => assertReviewResult(method, submission));
  assert.doesNotThrow(() => assertReviewResult("review_submissions.list", { cursor: 0, next_cursor: null, attempts: [submission] }));
});

test("review validators reject extra fields, byte overflow, and contradictory outcomes", () => {
  assert.throws(
    () => assertReviewParams("review_mutations.comment", { operation_id: operation, review, body: "Note", raw_error: "secret" }),
    /fields/,
  );
  assert.throws(
    () => assertReviewParams("review_mutations.comment", { operation_id: operation, review, body: "😀".repeat(20_000) }),
    /byte limit/,
  );
  assert.throws(
    () => assertReviewParams("drafts.create", { review, revision, content: { ...content, comments: [{ ...content.comments[0], anchor: { ...draftAnchor, stale: "false" } }] } }),
    /boolean/,
  );
  assert.throws(
    () => assertReviewResult("review_mutations.comment", { ...mutation, outcome: "unknown" }),
    /outcome state/,
  );
  assert.throws(
    () => assertReviewResult("review_actions.merge", actionReceipt),
    /does not match/,
  );
  assert.throws(
    () => assertReviewResult("review_actions.merge", {
      ...actionReceipt,
      action: "merge",
      merge_sha: null,
    }),
    /requires a merge SHA/,
  );
  for (const change of [
    { remote_id: "claimed" },
    { merge_sha: "claimed" },
    { source_cleanup: "confirmed" },
    { resync_required: false },
    { error: null },
  ]) {
    assert.throws(
      () => assertReviewResult("review_actions.close", {
        ...actionReceipt,
        outcome: "unknown",
        remote_id: null,
        error: { code: "network", message: "Unknown.", retryable: true },
        resync_required: true,
        ...change,
      }),
      /Unknown review action receipt/,
    );
  }
  for (const impossibleAnchor of [
    { ...draftAnchor, side: "old", old_line: null, new_line: 4 },
    { ...draftAnchor, side: "new", old_line: 3, new_line: null },
  ]) {
    assert.throws(
      () => assertReviewParams("drafts.create", {
        review,
        revision,
        content: {
          ...content,
          comments: [
            { ...content.comments[0], anchor: impossibleAnchor },
          ],
        },
      }),
      /selected side has no line/,
    );
    assert.throws(
      () => assertReviewResult("drafts.get", {
        ...draft,
        comments: [
          {
            ...draft.comments[0],
            anchor: { ...impossibleAnchor, stale: false },
          },
        ],
      }),
      /selected side has no line/,
    );
  }
  assert.throws(
    () => assertReviewResult("review_submissions.status", { ...submission, unknown_step_ids: ["comment:0"] }),
    /confirmed and unknown/,
  );
  assert.throws(
    () => assertReviewResult("review_submissions.status", {
      ...submission,
      unknown_step_ids: ["verdict"],
      resync_required: false,
    }),
    /requires resynchronization/,
  );
});

test("request validation matches Python controls, bounds, canonical UUIDs, and sparse draft content", () => {
  assert.doesNotThrow(() => assertReviewParams("drafts.create", { review, revision, content: {} }));
  assert.doesNotThrow(() => assertReviewParams("drafts.save", {
    review,
    draft_id: draftId,
    expected_version: 1,
    content: { body: "only body" },
  }));
  assert.doesNotThrow(() => assertReviewParams("drafts.create", {
    review,
    revision,
    content: {
      comments: [{
        id: commentId,
        kind: "inline",
        body: "Note",
        anchor: {
          revision,
          old_path: "old.ts",
          new_path: "new.ts",
          old_line: null,
          new_line: 1,
          side: "new",
          context_fingerprint: "a".repeat(64),
        },
      }],
    },
  }));
  for (const invalid of [
    { method: "drafts.get", params: { review, draft_id: "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA" } },
    { method: "drafts.list", params: { review, cursor: 1_000_001 } },
    { method: "drafts.list", params: { review, states: ["editable", "editable"] } },
    { method: "review_mutations.reply", params: { operation_id: operation, review, revision, discussion_id: "bad\tthread", body: "ok" } },
    { method: "review_actions.merge", params: { operation_id: operation, review, revision, source_cleanup: { branch: "bad\rbranch" } } },
    { method: "review_mutations.comment", params: { operation_id: operation, review, body: "bad\rbody" } },
    { method: "review_mutations.comment", params: { operation_id: operation, review, body: "bad\ud800body" } },
  ]) {
    assert.throws(() => assertReviewParams(invalid.method, invalid.params));
  }
  assert.throws(
    () => assertReviewParams("review_actions.merge", {
      operation_id: operation,
      review,
      revision,
      source_cleanup: { branch: "x".repeat(501) },
    }),
    /text/,
  );
  assert.throws(
    () => assertReviewParams("drafts.create", {
      review,
      revision,
      content: {
        comments: [
          { id: commentId, kind: "general", body: "one" },
          { id: commentId, kind: "general", body: "two" },
        ],
      },
    }),
    /Duplicate review draft comment ID/,
  );
});

test("verdict and mutation outcomes enforce the Python service invariants", () => {
  assert.throws(
    () => assertReviewParams("review_mutations.verdict", {
      operation_id: operation, review, revision, verdict: "comment",
    }),
    /requires a body or inline comment/,
  );
  assert.throws(
    () => assertReviewParams("review_mutations.verdict", {
      operation_id: operation, review, revision, verdict: "request_changes", body: "",
    }),
    /requires a body/,
  );
  assert.doesNotThrow(() => assertReviewParams("review_mutations.verdict", {
    operation_id: operation,
    review,
    revision,
    verdict: "comment",
    inline_comments: [{ anchor: mutationAnchor, body: "inline" }],
  }));
  assert.throws(
    () => assertReviewResult("review_mutations.comment", { ...mutation, reason: "claimed" }),
    /known review mutation/,
  );
  for (const change of [
    { receipt: mutation.receipt },
    { reason: null },
    { resync_required: false },
  ]) {
    assert.throws(
      () => assertReviewResult("review_mutations.comment", {
        ...mutation,
        outcome: "unknown",
        receipt: null,
        reason: "timeout",
        resync_required: true,
        ...change,
      }),
      /unknown review mutation/,
    );
  }
  assert.throws(
    () => assertReviewResult("review_actions.reopen", {
      ...actionReceipt,
      action: "reopen",
      expected_state: "open",
    }),
    /invalid expected state/,
  );
});

test("submission evidence is exact and permits valid planless submitted recovery", () => {
  assert.throws(
    () => assertReviewResult("review_submissions.status", {
      ...submission,
      completed_step_ids: [],
    }),
    /must equal receipt steps/,
  );
  assert.throws(
    () => assertReviewResult("review_submissions.status", {
      ...submission,
      steps: [
        { id: "comment:0", kind: "inline_comment", comment_ids: [commentId, commentId] },
        submission.steps[1],
      ],
    }),
    /Duplicate review submission comment ID/,
  );
  const planless = {
    ...submission,
    state: "submitted",
    outcome: "submitted",
    steps: [],
    receipts: [{
      step_id: "github_review",
      remote_id: "remote-review",
      recorded_at: "2026-09-08T00:00:01+00:00",
      resync_required: false,
    }],
    completed_step_ids: ["github_review"],
    unknown_step_ids: [],
    atomic: false,
    resync_required: false,
    failure: null,
    plan_available: false,
  };
  assert.doesNotThrow(() => assertReviewResult("review_submissions.status", planless));
  assert.doesNotThrow(() => assertReviewResult("review_submissions.status", {
    ...planless,
    unknown_step_ids: ["github_review", "github_review"],
    resync_required: true,
  }));
  assert.throws(
    () => assertReviewResult("review_submissions.status", {
      ...planless,
      completed_step_ids: [],
    }),
    /must equal receipt steps/,
  );
});
