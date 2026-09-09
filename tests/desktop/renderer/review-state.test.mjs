import assert from "node:assert/strict";
import test from "node:test";

import {
  acknowledgeQuickUncertainty,
  adoptDisplayedRevision,
  adoptDraft,
  beginDraftSave,
  beginQuickIntent,
  beginSubmission,
  canStartSubmission,
  canCaptureDraftInline,
  canSaveDraft,
  captureDraftAnchor,
  chooseRemoteDraft,
  conflictDraftSave,
  contextFingerprint,
  createReviewWorkflowState,
  discardConfirmationPrompt,
  discardSubject,
  dismissSupersededDraft,
  draftNeedsRevisionRecovery,
  editDraft,
  keepLocalDraftRefusal,
  finishDraftSave,
  failDraftSave,
  finishSubmission,
  forkDraftToCurrentRevision,
  keepLocalDraft,
  markQuickIntentUncertain,
  observeReviewRevision,
  portableDraftContent,
  recoverSubmission,
  recoverQuickIntent,
  settleQuickIntent,
} from "../../../desktop/dist/src/renderer/features/review/state.js";

const review = "opaque-review";
const revision = { head_sha: "head", base_sha: "base", start_sha: null };
const draftId = "11111111-1111-4111-8111-111111111111";
const attemptId = "22222222-2222-4222-8222-222222222222";

test("display refresh preserves the captured revision until explicit adoption", () => {
  let state = createReviewWorkflowState(review, revision);
  state = observeReviewRevision(state, { ...revision, head_sha: "new-head" });
  assert.equal(state.displayed.revision.head_sha, "head");
  assert.equal(state.displayed.latestObservedRevision.head_sha, "new-head");
  state = editDraft(state, { body: "local", verdict: null, comments: [] });
  assert.throws(() => adoptDisplayedRevision(state), /local draft edits/);
});

test("quick intents retain one immutable operation and block after uncertainty", () => {
  const command = { body: "first" };
  let state = beginQuickIntent(
    createReviewWorkflowState(review, revision),
    "quick:1",
    command,
  );
  command.body = "changed outside";
  assert.equal(state.quick.command.body, "first");
  assert.throws(
    () => beginQuickIntent(state, "quick:2", { body: "second" }),
    /previous uncertain action/,
  );
  state = markQuickIntentUncertain(state, "quick:1");
  assert.equal(state.quick.status, "unknown");
  assert.match(state.quick.message, /may have completed remotely/);
  assert.throws(
    () => beginQuickIntent(state, "quick:2", { body: "second" }),
    /previous uncertain action/,
  );
});

test("known quick outcome is bound to its original operation ID", () => {
  let state = beginQuickIntent(
    createReviewWorkflowState(review, revision),
    "quick:1",
    { body: "first" },
  );
  assert.throws(
    () =>
      settleQuickIntent(state, "quick:1", {
        operation_id: "quick:other",
        outcome: "known",
        receipt: { remote_id: "r", comment_id: "c", discussion_id: null },
        reason: null,
        resync_required: false,
      }),
    /different operation ID/,
  );
  state = settleQuickIntent(state, "quick:1", {
    operation_id: "quick:1",
    outcome: "known",
    receipt: { remote_id: "r", comment_id: "c", discussion_id: null },
    reason: null,
    resync_required: false,
  });
  assert.equal(state.quick.status, "known");
  assert.doesNotThrow(() => beginQuickIntent(state, "quick:2", { body: "second" }));
});

test("unknown quick intent survives refresh until explicit same-operation resolution", () => {
  let state = adoptDraft(createReviewWorkflowState(review, revision), draft(1, "server"));
  state = recoverSubmission(state, progress("unknown", ["verdict"], ["comment:0"]));
  state = beginQuickIntent(state, "close:1", {
    action: "close",
    operation_id: "close:1",
    review,
    revision,
  });
  state = markQuickIntentUncertain(state, "close:1");
  const refreshed = observeReviewRevision(state, revision);
  assert.equal(refreshed.quick.status, "unknown");
  assert.equal(refreshed.draft.remote.id, draftId);
  assert.equal(refreshed.submission.progress.attempt_id, attemptId);
  assert.throws(
    () => recoverQuickIntent(refreshed, "other:1", actionOutcome("other:1", "known")),
    /matching uncertain/,
  );
  assert.throws(
    () => recoverQuickIntent(refreshed, "close:1", {
      ...actionOutcome("close:1", "known"),
      review: "another-review",
    }),
    /frozen target/,
  );
  assert.throws(
    () => recoverQuickIntent(refreshed, "close:1", {
      ...actionOutcome("close:1", "known"),
      revision: { ...revision, head_sha: "other" },
    }),
    /frozen target/,
  );
  const recovered = recoverQuickIntent(
    refreshed,
    "close:1",
    actionOutcome("close:1", "known"),
  );
  assert.equal(recovered.quick.status, "known");
  assert.equal(recovered.quick.operationId, "close:1");
  assert.equal(recovered.quick.command.operation_id, "close:1");
  assert.equal(recovered.draft.remote.id, draftId);
  assert.equal(recovered.submission.progress.attempt_id, attemptId);
});

test("explicit uncertainty acknowledgment retains frozen intent without generating a write", () => {
  const command = {
    operation_id: "comment:1",
    review,
    body: "possibly delivered",
  };
  let state = beginQuickIntent(createReviewWorkflowState(review, revision), "comment:1", command);
  state = markQuickIntentUncertain(state, "comment:1");
  const originalCommand = state.quick.command;
  const acknowledged = acknowledgeQuickUncertainty(state, "comment:1");
  assert.equal(acknowledged.quick.status, "acknowledged_unknown");
  assert.equal(acknowledged.quick.operationId, "comment:1");
  assert.equal(acknowledged.quick.command, originalCommand);
  assert.equal(acknowledged.quick.outcome, null);
  assert.match(acknowledged.quick.message, /without replay/);
  const next = beginQuickIntent(acknowledged, "comment:2", { body: "deliberate new intent" });
  assert.equal(next.quick.operationId, "comment:2");
});

test("draft conflict and edit-during-save preserve unsaved local text", () => {
  let state = adoptDraft(createReviewWorkflowState(review, revision), draft(1, "server"));
  state = editDraft(state, { body: "local", verdict: "comment", comments: [] });
  state = beginDraftSave(state);
  assert.deepEqual(
    {
      review: state.draft.pendingSave.review,
      draftId: state.draft.pendingSave.draftId,
      revision: state.draft.pendingSave.revision,
      expectedVersion: state.draft.pendingSave.expectedVersion,
    },
    { review, draftId, revision, expectedVersion: 1 },
  );
  assert.throws(() => beginDraftSave(state), /already pending/);
  state = editDraft(state, { body: "newer local", verdict: "comment", comments: [] });
  state = finishDraftSave(state, draft(2, "local"));
  assert.equal(state.draft.remote.version, 2);
  assert.equal(state.draft.local.body, "newer local");
  assert.equal(state.draft.dirty, true);

  state = beginDraftSave(state);
  state = conflictDraftSave(state, draft(3, "TUI edit"));
  assert.equal(state.draft.local.body, "newer local");
  assert.equal(state.draft.conflict.body, "TUI edit");
  state = keepLocalDraft(state);
  assert.equal(state.draft.remote.version, 3);
  assert.equal(state.draft.local.body, "newer local");
  assert.equal(state.draft.dirty, true);
});

test("either conflict choice clears the gate and keeps the losing text reachable", () => {
  const remoteComment = {
    id: "33333333-3333-4333-8333-333333333333",
    kind: "general",
    body: "remote comment",
  };
  const stored = { ...draft(4, "stored body"), comments: [remoteComment] };
  let base = adoptDraft(createReviewWorkflowState(review, revision), draft(3, "server"));
  base = beginDraftSave(
    editDraft(base, { body: "my text", verdict: "approve", comments: [] }),
  );
  const conflicted = conflictDraftSave(base, stored);
  assert.equal(canSaveDraft(conflicted), false);
  assert.equal(conflicted.draft.local.body, "my text");
  assert.equal(conflicted.draft.conflict.body, "stored body");
  assert.equal(conflicted.draft.conflict.comments[0].body, "remote comment");
  assert.deepEqual(conflicted.draft.supersededLocalDrafts, []);
  assert.equal(conflicted.draft.conflictHeldVersion, 3);
  assert.equal(keepLocalDraftRefusal(conflicted), null);

  const mine = keepLocalDraft(conflicted);
  assert.equal(mine.draft.conflict, null);
  assert.equal(mine.draft.conflictHeldVersion, null);
  assert.equal(mine.draft.remote.version, 4);
  assert.equal(mine.draft.local.body, "my text");
  assert.equal(canSaveDraft(mine), true);
  const resaved = finishDraftSave(beginDraftSave(mine), {
    ...draft(5, "my text"),
    verdict: "approve",
  });
  assert.equal(resaved.draft.remote.version, 5);
  assert.equal(resaved.draft.local.body, "my text");
  assert.equal(resaved.draft.dirty, false);

  const theirs = chooseRemoteDraft(conflicted);
  assert.equal(theirs.draft.conflict, null);
  assert.equal(theirs.draft.remote.version, 4);
  assert.equal(theirs.draft.local.body, "stored body");
  assert.equal(theirs.draft.local.comments[0].body, "remote comment");
  assert.equal(theirs.draft.dirty, false);
  assert.deepEqual(
    theirs.draft.supersededLocalDrafts.map((item) => [
      item.displacedByVersion,
      item.content.body,
      item.content.verdict,
    ]),
    [[4, "my text", "approve"]],
  );
  assert.equal(canSaveDraft(theirs), false);
  assert.equal(theirs.draft.pendingSave, null);
  assert.equal(theirs.draft.conflictHeldVersion, null);
  assert.equal(draftNeedsRevisionRecovery(theirs), false);
  const kept = editDraft(theirs, { body: "next", verdict: null, comments: [] });
  assert.equal(kept.draft.supersededLocalDrafts[0].content.body, "my text");
  const dismissed = dismissSupersededDraft(theirs, 4);
  assert.deepEqual(dismissed.draft.supersededLocalDrafts, []);
  assert.throws(() => dismissSupersededDraft(dismissed, 4), /retained for that version/);
  assert.throws(() => chooseRemoteDraft(dismissed), /No conflicting draft/);
  assert.throws(() => keepLocalDraft(dismissed), /No conflicting draft/);
});

test("a second take-theirs adds a retained copy instead of discarding the first", () => {
  let state = adoptDraft(createReviewWorkflowState(review, revision), draft(3, "server"));
  state = beginDraftSave(
    editDraft(state, { body: "first local", verdict: null, comments: [] }),
  );
  state = chooseRemoteDraft(conflictDraftSave(state, draft(4, "stored four")));
  assert.equal(state.draft.local.body, "stored four");
  state = beginDraftSave(
    editDraft(state, { body: "second local", verdict: null, comments: [] }),
  );
  state = chooseRemoteDraft(conflictDraftSave(state, draft(6, "stored six")));
  assert.deepEqual(
    state.draft.supersededLocalDrafts.map((item) => [
      item.displacedByVersion,
      item.content.body,
    ]),
    [
      [4, "first local"],
      [6, "second local"],
    ],
  );
  assert.equal(state.draft.local.body, "stored six");
  const partial = dismissSupersededDraft(state, 4);
  assert.deepEqual(
    partial.draft.supersededLocalDrafts.map((item) => item.content.body),
    ["second local"],
  );
  assert.deepEqual(
    dismissSupersededDraft(partial, 6).draft.supersededLocalDrafts,
    [],
  );
});

test("keeping local text is refused, not applied, when the stored draft cannot be saved over", () => {
  let editable = adoptDraft(createReviewWorkflowState(review, revision), draft(3, "server"));
  editable = beginDraftSave(
    editDraft(editable, { body: "my precious text", verdict: null, comments: [] }),
  );
  for (const [draftState, pattern] of [
    ["submitted", /already submitted/],
    ["submitting", /submission attempt elsewhere/],
    ["partial", /submission attempt elsewhere/],
    ["unknown", /submission attempt elsewhere/],
  ]) {
    const conflicted = conflictDraftSave(editable, {
      ...draft(4, "stored elsewhere"),
      state: draftState,
    });
    assert.match(keepLocalDraftRefusal(conflicted), pattern);
    assert.throws(() => keepLocalDraft(conflicted), /no longer editable/);
    const theirs = chooseRemoteDraft(conflicted);
    assert.equal(theirs.draft.conflict, null);
    assert.equal(theirs.draft.supersededLocalDrafts[0].content.body, "my precious text");
  }

  const commentId = "33333333-3333-4333-8333-333333333333";
  const comment = { id: commentId, kind: "general", body: "keep" };
  let withComment = adoptDraft(createReviewWorkflowState(review, revision), {
    ...draft(3, "server"),
    comments: [comment],
  });
  withComment = beginDraftSave(
    editDraft(withComment, { body: "my text", verdict: null, comments: [comment] }),
  );
  withComment = conflictDraftSave(withComment, draft(4, "stored elsewhere"));
  assert.equal(keepLocalDraftRefusal(withComment), null);
  const blanked = editDraft(withComment, {
    body: "my text",
    verdict: null,
    comments: [{ ...comment, body: "" }],
  });
  assert.match(keepLocalDraftRefusal(blanked), /empty draft comments/);
  assert.equal(
    keepLocalDraftRefusal(chooseRemoteDraft(withComment)),
    "No draft conflict is waiting to be resolved.",
  );
});

test("taking an identical stored version retains no superseded copy", () => {
  let state = adoptDraft(createReviewWorkflowState(review, revision), draft(3, "server"));
  state = beginDraftSave(editDraft(state, { body: "same", verdict: null, comments: [] }));
  state = conflictDraftSave(state, draft(4, "same"));
  const theirs = chooseRemoteDraft(state);
  assert.deepEqual(theirs.draft.supersededLocalDrafts, []);
  assert.equal(theirs.draft.local.body, "same");
});

test("draft save results bind the same draft, revision, and version", () => {
  let state = adoptDraft(createReviewWorkflowState(review, revision), draft(1, "server"));
  state = beginDraftSave(editDraft(state, { body: "local", verdict: null, comments: [] }));
  assert.throws(
    () => finishDraftSave(state, { ...draft(2, "local"), id: "44444444-4444-4444-8444-444444444444" }),
    /pending version/,
  );
  assert.throws(
    () => finishDraftSave(state, { ...draft(2, "local"), revision: { ...revision, head_sha: "other" } }),
    /pending version/,
  );
  assert.throws(() => finishDraftSave(state, draft(3, "local")), /pending version/);
  assert.throws(() => conflictDraftSave(state, draft(1, "server")), /pending version/);

  state = beginDraftSave(editDraft(
    adoptDraft(createReviewWorkflowState(review, revision), draft(3, "server")),
    { body: "new local", verdict: null, comments: [] },
  ));
  assert.throws(() => conflictDraftSave(state, draft(2, "older")), /pending version/);
});

test("empty retained comments block saving until edited or deliberately removed", () => {
  const comment = {
    id: "33333333-3333-4333-8333-333333333333",
    kind: "general",
    body: "comment",
  };
  let state = adoptDraft(createReviewWorkflowState(review, revision), {
    ...draft(1, "server"),
    comments: [comment],
  });
  state = editDraft(state, {
    body: "server",
    verdict: null,
    comments: [{ ...comment, body: "" }],
  });
  assert.equal(canSaveDraft(state), false);
  assert.throws(() => beginDraftSave(state), /empty draft comments/);
  state = editDraft(state, { body: "server", verdict: null, comments: [] });
  assert.equal(canSaveDraft(state), true);
});

test("old-revision drafts load intact and stay blocked across either head-change order", () => {
  const nextRevision = { ...revision, head_sha: "new-head" };
  const stale = {
    ...draft(4, "preserved body"),
    comments: [{
      id: "33333333-3333-4333-8333-333333333333",
      kind: "inline",
      body: "preserved inline",
      anchor: {
        revision,
        old_path: "old.py",
        new_path: "new.py",
        old_line: null,
        new_line: 2,
        side: "new",
        context_fingerprint: "a".repeat(64),
        start_line: null,
        start_side: null,
        stale: true,
      },
    }],
  };
  let loadedAfter = observeReviewRevision(
    createReviewWorkflowState(review, revision),
    nextRevision,
  );
  loadedAfter = adoptDraft(loadedAfter, stale);
  assert.equal(loadedAfter.draft.local.body, "preserved body");
  assert.equal(loadedAfter.draft.local.comments[0].body, "preserved inline");
  assert.equal(canStartSubmission(loadedAfter), false);
  assert.equal(canCaptureDraftInline(loadedAfter), false);

  let changedAfter = adoptDraft(createReviewWorkflowState(review, revision), {
    ...stale,
    comments: [],
  });
  assert.equal(canStartSubmission(changedAfter), true);
  changedAfter = observeReviewRevision(changedAfter, nextRevision);
  assert.equal(canStartSubmission(changedAfter), false);
  assert.equal(canCaptureDraftInline(changedAfter), false);
});

test("explicit current-revision fork copies only portable content and preserves stale draft", () => {
  const nextRevision = { ...revision, head_sha: "new-head" };
  const stale = {
    ...draft(2, "portable body"),
    verdict: "approve",
    comments: [
      { id: "33333333-3333-4333-8333-333333333333", kind: "general", body: "general" },
      { id: "44444444-4444-4444-8444-444444444444", kind: "reply", body: "reply", thread_id: "thread" },
    ],
  };
  let state = adoptDraft(
    observeReviewRevision(createReviewWorkflowState(review, revision), nextRevision),
    stale,
  );
  assert.deepEqual(portableDraftContent(stale).comments.map((item) => item.kind), ["general"]);
  const fresh = {
    ...draft(1, "portable body"),
    id: "55555555-5555-4555-8555-555555555555",
    revision: nextRevision,
    verdict: "approve",
    comments: [{ id: "33333333-3333-4333-8333-333333333333", kind: "general", body: "general" }],
  };
  state = forkDraftToCurrentRevision(state, fresh);
  assert.equal(state.draft.remote.id, fresh.id);
  assert.equal(state.draft.preservedStaleDrafts[0].id, stale.id);
  assert.equal(state.draft.preservedStaleDrafts[0].comments[1].body, "reply");
  assert.equal(state.displayed.revision.head_sha, "new-head");
});

test("known save rejection clears pending state without inventing a version conflict", () => {
  let state = adoptDraft(createReviewWorkflowState(review, revision), draft(1, "server"));
  state = editDraft(state, { body: "local", verdict: null, comments: [] });
  state = beginDraftSave(state);
  state = failDraftSave(state);
  assert.equal(state.draft.local.body, "local");
  assert.equal(state.draft.dirty, true);
  assert.equal(state.draft.pendingSave, null);
  assert.equal(state.draft.conflict, null);
});

test("submission recovery distinguishes confirmed, paused, and unknown steps", () => {
  let state = adoptDraft(createReviewWorkflowState(review, revision), draft(1, "ready"));
  assert.equal(canStartSubmission(state), true);
  state = beginSubmission(state, "start");
  assert.deepEqual(
    {
      kind: state.submission.pending.kind,
      review: state.submission.pending.review,
      draftId: state.submission.pending.draftId,
      frozenVersion: state.submission.pending.frozenVersion,
    },
    { kind: "start", review, draftId, frozenVersion: 1 },
  );
  assert.throws(
    () => finishSubmission(state, { ...progress("paused", [], ["comment:0"]), draft_id: "44444444-4444-4444-8444-444444444444" }),
    /pending attempt/,
  );
  state = finishSubmission(state, progress("paused", [], ["comment:0"]));
  assert.equal(canStartSubmission(state), false);
  assert.throws(
    () => editDraft(state, { body: "unsafe", verdict: null, comments: [] }),
    /locked by its durable submission/,
  );
  assert.match(state.submission.message, /confirmed steps/);
  const resuming = beginSubmission(state, "resume");
  assert.equal(resuming.submission.pending.attemptId, attemptId);
  assert.throws(
    () => finishSubmission(resuming, { ...progress("submitted", [], ["comment:0"]), attempt_id: "44444444-4444-4444-8444-444444444444" }),
    /pending attempt/,
  );

  state = recoverSubmission(state, progress("unknown", ["verdict"], ["comment:0"]));
  assert.match(state.submission.message, /Reconcile before continuing/);
  assert.throws(() => beginSubmission(state, "resume"), /paused/);
  assert.doesNotThrow(() => beginSubmission(state, "reconcile"));
});

test("submission recovery cannot bind progress to another active draft", () => {
  const state = adoptDraft(
    createReviewWorkflowState(review, revision),
    draft(2, "active"),
  );
  assert.throws(
    () => recoverSubmission(state, {
      ...progress("unknown", ["verdict"], ["comment:0"]),
      draft_id: "44444444-4444-4444-8444-444444444444",
    }),
    /active draft/,
  );
});

test("context fingerprint matches Python length-prefixed UTF-8 and rejects changed selection", async () => {
  assert.equal(
    await contextFingerprint(["α", "新 line", "tail"]),
    "d363c19a3720b6f631892875fa879c709488d76173e741581b5b0027ef7f95c6",
  );
  const selection = {
    review,
    revision,
    oldPath: "old.py",
    newPath: "new.py",
    side: "new",
    oldLine: null,
    newLine: 2,
    startLine: null,
    startSide: null,
    contextLines: ["α", "新 line", "tail"],
    contextComplete: true,
  };
  const anchor = await captureDraftAnchor(selection, () => selection);
  assert.equal(anchor.context_fingerprint, "d363c19a3720b6f631892875fa879c709488d76173e741581b5b0027ef7f95c6");
  await assert.rejects(
    captureDraftAnchor(selection, () => ({ ...selection, newLine: 3 })),
    /selection changed/,
  );
  await assert.rejects(
    captureDraftAnchor({ ...selection, contextComplete: false }, () => selection),
    /context is partial/,
  );
});

/**
 * The wording the drawer and the composer overflow both put in front of the
 * reader. The corner the two surfaces cannot reach on their own is a review
 * whose comments are all gone: it must not claim comments it does not have,
 * and it must not offer a summary that was never written.
 */
test("the discard confirmation names only what the draft actually holds", () => {
  const base = createReviewWorkflowState(review, revision);
  const summaryOnly = adoptDraft(base, draft(1, "a summary and nothing else"));
  assert.equal(discardSubject(summaryOnly), "the summary");
  assert.equal(
    discardConfirmationPrompt(summaryOnly),
    "Discard the summary? This cannot be undone.",
  );

  const empty = adoptDraft(base, draft(1, "   "));
  assert.equal(discardSubject(empty), "this pending review");
  assert.equal(
    discardConfirmationPrompt(empty),
    "Discard this pending review? This cannot be undone.",
  );

  const both = adoptDraft(base, {
    ...draft(2, "a summary"),
    comments: [
      { id: "c1", kind: "general", body: "one" },
      { id: "c2", kind: "general", body: "two" },
    ],
  });
  assert.equal(discardSubject(both), "2 pending comments and the summary");
  assert.equal(
    discardConfirmationPrompt(both),
    "Discard 2 pending comments and the summary? This cannot be undone.",
  );

  const oneComment = adoptDraft(base, {
    ...draft(3, ""),
    comments: [{ id: "c1", kind: "general", body: "one" }],
  });
  assert.equal(discardSubject(oneComment), "1 pending comment");
});

function draft(version, body) {
  return {
    id: draftId,
    review,
    revision,
    version,
    body,
    verdict: null,
    comments: [],
    state: "editable",
    created_at: "2026-09-08T00:00:00+00:00",
    updated_at: "2026-09-08T00:00:00+00:00",
  };
}

function progress(outcome, unknown, completed) {
  return {
    attempt_id: attemptId,
    draft_id: draftId,
    review,
    frozen_version: 1,
    state: outcome === "unknown" ? "unknown" : "partial",
    outcome,
    steps: [
      { id: "comment:0", kind: "general_comment", comment_ids: [] },
      { id: "verdict", kind: "verdict", comment_ids: [] },
    ],
    receipts: completed.map((step_id) => ({
      step_id,
      remote_id: `remote-${step_id}`,
      recorded_at: "2026-09-08T00:00:01+00:00",
      resync_required: false,
    })),
    completed_step_ids: completed,
    unknown_step_ids: unknown,
    atomic: false,
    resync_required: outcome !== "submitted",
    failure: outcome === "paused"
      ? { code: "authorization", message: "Denied.", retryable: false, step_id: "verdict" }
      : null,
    plan_available: true,
  };
}

function actionOutcome(operationId, outcome) {
  return {
    operation_id: operationId,
    action: "close",
    review,
    revision,
    expected_state: "open",
    outcome,
    remote_id: outcome === "known" ? "remote" : null,
    merge_sha: null,
    source_cleanup: "not_requested",
    error: outcome === "known" ? null : { code: "network", message: "Unknown.", retryable: true },
    resync_required: true,
  };
}
