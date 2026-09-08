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
  captureDraftAnchor,
  conflictDraftSave,
  contextFingerprint,
  createReviewWorkflowState,
  editDraft,
  finishDraftSave,
  finishSubmission,
  keepLocalDraft,
  markQuickIntentUncertain,
  observeReviewRevision,
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

test("submission recovery distinguishes confirmed, paused, and unknown steps", () => {
  let state = adoptDraft(createReviewWorkflowState(review, revision), draft(1, "ready"));
  assert.equal(canStartSubmission(state), true);
  state = beginSubmission(state, "start");
  state = finishSubmission(state, progress("paused", [], ["comment:0"]));
  assert.match(state.submission.message, /confirmed steps/);
  assert.doesNotThrow(() => beginSubmission(state, "resume"));

  state = recoverSubmission(state, progress("unknown", ["verdict"], ["comment:0"]));
  assert.match(state.submission.message, /Reconcile before continuing/);
  assert.throws(() => beginSubmission(state, "resume"), /paused/);
  assert.doesNotThrow(() => beginSubmission(state, "reconcile"));
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
