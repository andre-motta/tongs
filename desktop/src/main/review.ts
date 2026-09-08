import type { JsonObject, JsonValue } from "../shared/bridge.js";
import {
  REVIEW_IPC_CHANNELS,
  type ReviewAction,
  type ReviewMutationMethod,
  type ReviewRpcMethod,
  type ReviewRpcParams,
  type ReviewRpcResult,
  type ReviewVerdict,
} from "../shared/review.js";

const MAX_OPERATION_ID = 128;
const MAX_HANDLE = 2048;
const MAX_BODY_BYTES = 65_536;
const MAX_TEXT = 2048;
const MAX_PATH = 4096;
const MAX_COMMENTS = 200;
const MAX_PAGE_ITEMS = 100;
const MAX_SUBMISSION_STEPS = 204;
const OPERATION_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const FINGERPRINT = /^[0-9a-f]{64}$/;

export interface ReviewOperation {
  readonly channel: string;
  readonly method: ReviewRpcMethod;
  readonly mutation: boolean;
}

export const REVIEW_OPERATIONS: readonly ReviewOperation[] = Object.freeze([
  operation(REVIEW_IPC_CHANNELS.mutationCapabilities, "review_mutations.capabilities", false),
  operation(REVIEW_IPC_CHANNELS.comment, "review_mutations.comment", true),
  operation(REVIEW_IPC_CHANNELS.inlineComment, "review_mutations.inline_comment", true),
  operation(REVIEW_IPC_CHANNELS.reply, "review_mutations.reply", true),
  operation(REVIEW_IPC_CHANNELS.resolve, "review_mutations.resolve", true),
  operation(REVIEW_IPC_CHANNELS.verdict, "review_mutations.verdict", true),
  operation(REVIEW_IPC_CHANNELS.actionCapabilities, "review_actions.capabilities", false),
  operation(REVIEW_IPC_CHANNELS.merge, "review_actions.merge", true),
  operation(REVIEW_IPC_CHANNELS.close, "review_actions.close", true),
  operation(REVIEW_IPC_CHANNELS.reopen, "review_actions.reopen", true),
  operation(REVIEW_IPC_CHANNELS.unapprove, "review_actions.unapprove", true),
  operation(REVIEW_IPC_CHANNELS.actionReceipt, "review_actions.receipt", false),
  operation(REVIEW_IPC_CHANNELS.createDraft, "drafts.create", true),
  operation(REVIEW_IPC_CHANNELS.getDraft, "drafts.get", false),
  operation(REVIEW_IPC_CHANNELS.listDrafts, "drafts.list", false),
  operation(REVIEW_IPC_CHANNELS.saveDraft, "drafts.save", true),
  operation(REVIEW_IPC_CHANNELS.discardDraft, "drafts.discard", true),
  operation(REVIEW_IPC_CHANNELS.startSubmission, "review_submissions.start", true),
  operation(REVIEW_IPC_CHANNELS.getSubmission, "review_submissions.status", false),
  operation(REVIEW_IPC_CHANNELS.listSubmissions, "review_submissions.list", false),
  operation(REVIEW_IPC_CHANNELS.resumeSubmission, "review_submissions.resume", true),
  operation(REVIEW_IPC_CHANNELS.reconcileSubmission, "review_submissions.reconcile", true),
]);

export function assertReviewParams<M extends ReviewRpcMethod>(
  method: M,
  value: unknown,
): asserts value is ReviewRpcParams<M> & JsonObject {
  const params = record(value);
  switch (method) {
    case "review_mutations.capabilities":
    case "review_actions.capabilities":
      keys(params, ["review"]);
      handle(params.review);
      return;
    case "review_mutations.comment":
      keys(params, ["operation_id", "review", "body"]);
      operationId(params.operation_id);
      handle(params.review);
      body(params.body, false);
      return;
    case "review_mutations.inline_comment":
      keys(params, ["operation_id", "review", "revision", "anchor", "body"]);
      operationId(params.operation_id);
      handle(params.review);
      revision(params.revision);
      mutationAnchor(params.anchor);
      body(params.body, false);
      return;
    case "review_mutations.reply":
      keys(params, ["operation_id", "review", "revision", "discussion_id", "body"]);
      operationId(params.operation_id);
      handle(params.review);
      revision(params.revision);
      text(params.discussion_id, 512);
      body(params.body, false);
      return;
    case "review_mutations.resolve":
      keys(params, ["operation_id", "review", "revision", "discussion_id", "resolved"]);
      operationId(params.operation_id);
      handle(params.review);
      revision(params.revision);
      text(params.discussion_id, 512);
      bool(params.resolved);
      return;
    case "review_mutations.verdict":
      keys(params, ["operation_id", "review", "revision", "verdict"], ["body", "inline_comments"]);
      operationId(params.operation_id);
      handle(params.review);
      revision(params.revision);
      verdict(params.verdict);
      if (Object.hasOwn(params, "body")) body(params.body, true);
      if (Object.hasOwn(params, "inline_comments")) {
        array(params.inline_comments, MAX_COMMENTS).forEach(verdictComment);
      }
      return;
    case "review_actions.merge":
      keys(params, ["operation_id", "review", "revision"], ["squash", "source_cleanup"]);
      revisionOperation(params);
      if (Object.hasOwn(params, "squash")) bool(params.squash);
      if (Object.hasOwn(params, "source_cleanup")) {
        if (params.source_cleanup !== null) {
          const cleanup = record(params.source_cleanup);
          keys(cleanup, ["branch"]);
          text(cleanup.branch, 500);
        }
      }
      return;
    case "review_actions.close":
    case "review_actions.reopen":
    case "review_actions.unapprove":
      keys(params, ["operation_id", "review", "revision"]);
      revisionOperation(params);
      return;
    case "review_actions.receipt":
      keys(params, ["operation_id", "review", "revision", "action"]);
      revisionOperation(params);
      action(params.action);
      return;
    case "drafts.create":
      keys(params, ["review", "revision"], ["content"]);
      handle(params.review);
      revision(params.revision);
      if (Object.hasOwn(params, "content")) draftContent(params.content, false);
      return;
    case "drafts.get":
      keys(params, ["review", "draft_id"]);
      handle(params.review);
      uuid(params.draft_id);
      return;
    case "drafts.list":
      keys(params, ["review"], ["states", "cursor", "max_items"]);
      handle(params.review);
      if (Object.hasOwn(params, "states"))
        array(params.states, 5).forEach(draftState);
      optionalPage(params);
      return;
    case "drafts.save":
      keys(params, ["review", "draft_id", "expected_version", "content"]);
      draftIdentity(params);
      draftContent(params.content, false);
      return;
    case "drafts.discard":
    case "review_submissions.start":
      keys(params, ["review", "draft_id", "expected_version"]);
      draftIdentity(params);
      return;
    case "review_submissions.status":
    case "review_submissions.resume":
      keys(params, ["review", "attempt_id"]);
      attemptIdentity(params);
      return;
    case "review_submissions.list":
      keys(params, ["review"], ["cursor", "max_items"]);
      handle(params.review);
      optionalPage(params);
      return;
    case "review_submissions.reconcile":
      keys(params, ["review", "attempt_id", "resolution"]);
      attemptIdentity(params);
      oneOf(params.resolution, ["retry_remaining", "return_editable", "mark_submitted"]);
      return;
  }
}

export function assertReviewResult<M extends ReviewRpcMethod>(
  method: M,
  value: unknown,
): asserts value is ReviewRpcResult<M> & JsonValue {
  switch (method) {
    case "review_mutations.capabilities":
      capabilities(value, [
        "general_comment", "inline_comment", "multiline_comment", "reply", "resolve",
        "approve", "request_changes", "comment_verdict", "atomic_review_batch",
      ]);
      return;
    case "review_actions.capabilities":
      capabilities(value, ["merge", "close", "reopen", "unapprove"]);
      return;
    case "review_mutations.comment":
    case "review_mutations.inline_comment":
    case "review_mutations.reply":
    case "review_mutations.resolve":
    case "review_mutations.verdict":
      mutationOutcome(value);
      return;
    case "review_actions.merge":
    case "review_actions.close":
    case "review_actions.reopen":
    case "review_actions.unapprove":
      actionReceipt(value, expectedAction(method));
      return;
    case "review_actions.receipt": {
      const result = record(value);
      keys(result, ["receipt"]);
      if (result.receipt !== null) actionReceipt(result.receipt);
      return;
    }
    case "drafts.create":
    case "drafts.get":
    case "drafts.save":
      draftSnapshot(value);
      return;
    case "drafts.discard": {
      const result = record(value);
      keys(result, ["discarded"]);
      draftSnapshot(result.discarded);
      return;
    }
    case "drafts.list": {
      const result = record(value);
      keys(result, ["cursor", "next_cursor", "drafts"]);
      page(result);
      array(result.drafts, MAX_PAGE_ITEMS).forEach(draftSnapshot);
      return;
    }
    case "review_submissions.start":
    case "review_submissions.status":
    case "review_submissions.resume":
    case "review_submissions.reconcile":
      submission(value);
      return;
    case "review_submissions.list": {
      const result = record(value);
      keys(result, ["cursor", "next_cursor", "attempts"]);
      page(result);
      array(result.attempts, MAX_PAGE_ITEMS).forEach(submission);
      return;
    }
  }
}

function operation(
  channel: string,
  method: ReviewRpcMethod,
  mutation: boolean,
): ReviewOperation {
  return Object.freeze({ channel, method, mutation });
}

function revisionOperation(value: Record<string, unknown>): void {
  operationId(value.operation_id);
  handle(value.review);
  revision(value.revision);
}

function draftIdentity(value: Record<string, unknown>): void {
  handle(value.review);
  uuid(value.draft_id);
  positiveInteger(value.expected_version);
}

function attemptIdentity(value: Record<string, unknown>): void {
  handle(value.review);
  uuid(value.attempt_id);
}

function optionalPage(value: Record<string, unknown>): void {
  if (Object.hasOwn(value, "cursor")) nonnegativeInteger(value.cursor);
  if (Object.hasOwn(value, "max_items")) {
    positiveInteger(value.max_items);
    if ((value.max_items as number) > MAX_PAGE_ITEMS)
      throw new Error("Review page size exceeds the limit");
  }
}

function capabilities(value: unknown, names: readonly string[]): void {
  const result = record(value);
  keys(result, ["review", "capabilities"]);
  handle(result.review);
  const flags = record(result.capabilities);
  keys(flags, names);
  Object.values(flags).forEach(bool);
}

function mutationAnchor(value: unknown): void {
  const anchor = record(value);
  keys(anchor, ["old_path", "new_path", "line", "side"], ["start_line", "start_side"]);
  path(anchor.old_path);
  path(anchor.new_path);
  positiveInteger(anchor.line);
  oneOf(anchor.side, ["LEFT", "RIGHT"]);
  const hasStartLine = Object.hasOwn(anchor, "start_line") && anchor.start_line !== null;
  const hasStartSide = Object.hasOwn(anchor, "start_side") && anchor.start_side !== null;
  if (hasStartLine) positiveInteger(anchor.start_line);
  if (hasStartSide) oneOf(anchor.start_side, ["LEFT", "RIGHT"]);
  if (hasStartLine !== hasStartSide)
    throw new Error("Review multiline anchor is incomplete");
}

function draftAnchor(value: unknown, output: boolean): void {
  const anchor = record(value);
  keys(
    anchor,
    ["revision", "old_path", "new_path", "old_line", "new_line", "side", "context_fingerprint", "start_line", "start_side"],
    output ? ["stale"] : [],
  );
  revision(anchor.revision);
  path(anchor.old_path);
  path(anchor.new_path);
  nullablePositiveInteger(anchor.old_line);
  nullablePositiveInteger(anchor.new_line);
  oneOf(anchor.side, ["old", "new"]);
  if (typeof anchor.context_fingerprint !== "string" || !FINGERPRINT.test(anchor.context_fingerprint))
    throw new Error("Invalid review context fingerprint");
  nullablePositiveInteger(anchor.start_line);
  if (anchor.start_side !== null) oneOf(anchor.start_side, ["old", "new"]);
  if ((anchor.start_line === null) !== (anchor.start_side === null))
    throw new Error("Review draft multiline anchor is incomplete");
  if (output) bool(anchor.stale);
}

function verdictComment(value: unknown): void {
  const comment = record(value);
  keys(comment, ["anchor", "body"]);
  mutationAnchor(comment.anchor);
  body(comment.body, false);
}

function draftContent(value: unknown, output: boolean): void {
  const content = record(value);
  keys(content, ["body", "verdict", "comments"]);
  body(content.body, true);
  if (content.verdict !== null) verdict(content.verdict);
  array(content.comments, MAX_COMMENTS).forEach((comment) => draftComment(comment, output));
}

function draftComment(value: unknown, output: boolean): void {
  const comment = record(value);
  if (comment.kind === "general") {
    keys(comment, ["id", "kind", "body"]);
  } else if (comment.kind === "inline") {
    keys(comment, ["id", "kind", "body", "anchor"]);
    draftAnchor(comment.anchor, output);
  } else if (comment.kind === "reply") {
    keys(comment, ["id", "kind", "body", "thread_id"]);
    text(comment.thread_id, 512);
  } else {
    throw new Error("Invalid review draft comment kind");
  }
  uuid(comment.id);
  body(comment.body, false);
}

function mutationOutcome(value: unknown): void {
  const outcome = record(value);
  keys(outcome, ["operation_id", "outcome", "receipt", "reason", "resync_required"]);
  operationId(outcome.operation_id);
  oneOf(outcome.outcome, ["known", "unknown"]);
  if (outcome.receipt !== null) {
    const receipt = record(outcome.receipt);
    keys(receipt, ["remote_id", "comment_id", "discussion_id"]);
    text(receipt.remote_id, MAX_TEXT);
    nullableText(receipt.comment_id, MAX_TEXT);
    nullableText(receipt.discussion_id, MAX_TEXT);
  }
  nullableText(outcome.reason, MAX_TEXT);
  bool(outcome.resync_required);
  if ((outcome.outcome === "known") !== (outcome.receipt !== null))
    throw new Error("Invalid review mutation outcome state");
}

function actionReceipt(value: unknown, expected?: ReviewAction): void {
  const receipt = record(value);
  keys(receipt, [
    "operation_id", "action", "review", "revision", "expected_state", "outcome",
    "remote_id", "merge_sha", "source_cleanup", "error", "resync_required",
  ]);
  operationId(receipt.operation_id);
  const actual = action(receipt.action);
  if (expected !== undefined && actual !== expected)
    throw new Error("Review action receipt does not match the operation");
  handle(receipt.review);
  revision(receipt.revision);
  oneOf(receipt.expected_state, ["open", "closed"]);
  oneOf(receipt.outcome, ["known", "unknown"]);
  nullableText(receipt.remote_id, MAX_TEXT);
  nullableText(receipt.merge_sha, MAX_TEXT);
  oneOf(receipt.source_cleanup, ["not_requested", "confirmed", "rejected", "unknown"]);
  if (receipt.error !== null) serviceError(receipt.error);
  bool(receipt.resync_required);
  if ((receipt.outcome === "known") !== (receipt.error === null && receipt.remote_id !== null))
    throw new Error("Invalid review action receipt state");
  if (actual !== "merge" && (receipt.merge_sha !== null || receipt.source_cleanup !== "not_requested"))
    throw new Error("Invalid non-merge review receipt details");
}

function draftSnapshot(value: unknown): void {
  const draft = record(value);
  keys(draft, [
    "id", "review", "revision", "version", "body", "verdict", "comments",
    "state", "created_at", "updated_at",
  ]);
  uuid(draft.id);
  handle(draft.review);
  revision(draft.revision);
  positiveInteger(draft.version);
  body(draft.body, true);
  if (draft.verdict !== null) verdict(draft.verdict);
  array(draft.comments, MAX_COMMENTS).forEach((comment) => draftComment(comment, true));
  draftState(draft.state);
  timestamp(draft.created_at);
  timestamp(draft.updated_at);
}

function submission(value: unknown): void {
  const progress = record(value);
  keys(progress, [
    "attempt_id", "draft_id", "review", "frozen_version", "state", "outcome",
    "steps", "receipts", "completed_step_ids", "unknown_step_ids", "atomic",
    "resync_required", "failure", "plan_available",
  ]);
  uuid(progress.attempt_id);
  uuid(progress.draft_id);
  handle(progress.review);
  positiveInteger(progress.frozen_version);
  draftState(progress.state);
  oneOf(progress.outcome, ["submitted", "paused", "unknown", "editable"]);
  const steps = array(progress.steps, MAX_SUBMISSION_STEPS);
  const stepIds = new Set<string>();
  for (const item of steps) {
    const step = record(item);
    keys(step, ["id", "kind", "comment_ids"]);
    text(step.id, MAX_TEXT);
    if (stepIds.has(step.id as string)) throw new Error("Duplicate review submission step");
    stepIds.add(step.id as string);
    oneOf(step.kind, ["general_comment", "inline_comment", "reply", "body", "verdict", "github_review"]);
    array(step.comment_ids, MAX_COMMENTS).forEach(uuid);
  }
  const receiptSteps = new Set<string>();
  for (const item of array(progress.receipts, MAX_SUBMISSION_STEPS)) {
    const receipt = record(item);
    keys(receipt, ["step_id", "remote_id", "recorded_at", "resync_required"]);
    text(receipt.step_id, MAX_TEXT);
    text(receipt.remote_id, MAX_TEXT);
    timestamp(receipt.recorded_at);
    bool(receipt.resync_required);
    if (!stepIds.has(receipt.step_id as string) || receiptSteps.has(receipt.step_id as string))
      throw new Error("Invalid review submission receipt step");
    receiptSteps.add(receipt.step_id as string);
  }
  const completed = idList(progress.completed_step_ids, stepIds);
  const unknown = idList(progress.unknown_step_ids, stepIds);
  if ([...completed].some((id) => unknown.has(id)))
    throw new Error("Review submission step cannot be confirmed and unknown");
  bool(progress.atomic);
  bool(progress.resync_required);
  if (progress.failure !== null) submissionFailure(progress.failure, stepIds);
  bool(progress.plan_available);
}

function submissionFailure(value: unknown, stepIds: ReadonlySet<string>): void {
  const failure = record(value);
  keys(failure, ["code", "message", "retryable", "step_id"]);
  text(failure.code, MAX_TEXT);
  text(failure.message, MAX_TEXT);
  bool(failure.retryable);
  nullableText(failure.step_id, MAX_TEXT);
  if (failure.step_id !== null && !stepIds.has(failure.step_id as string))
    throw new Error("Invalid review submission failure step");
}

function idList(value: unknown, valid: ReadonlySet<string>): Set<string> {
  const result = new Set<string>();
  for (const item of array(value, MAX_SUBMISSION_STEPS)) {
    text(item, MAX_TEXT);
    if (!valid.has(item) || result.has(item))
      throw new Error("Invalid review submission step reference");
    result.add(item);
  }
  return result;
}

function page(value: Record<string, unknown>): void {
  nonnegativeInteger(value.cursor);
  if (value.next_cursor !== null) {
    positiveInteger(value.next_cursor);
    if ((value.next_cursor as number) <= (value.cursor as number))
      throw new Error("Review page cursor did not advance");
  }
}

function expectedAction(method: ReviewMutationMethod): ReviewAction {
  if (method === "review_actions.merge") return "merge";
  if (method === "review_actions.close") return "close";
  if (method === "review_actions.reopen") return "reopen";
  if (method === "review_actions.unapprove") return "unapprove";
  throw new Error("Unsupported review action mutation");
}

function action(value: unknown): ReviewAction {
  return oneOf(value, ["merge", "close", "reopen", "unapprove"]);
}

function verdict(value: unknown): ReviewVerdict {
  return oneOf(value, ["comment", "approve", "request_changes"]);
}

function draftState(value: unknown): void {
  oneOf(value, ["editable", "submitting", "partial", "unknown", "submitted"]);
}

function revision(value: unknown): void {
  const item = record(value);
  keys(item, ["head_sha", "base_sha", "start_sha"]);
  text(item.head_sha, 128);
  text(item.base_sha, 128);
  nullableText(item.start_sha, 128);
}

function serviceError(value: unknown): void {
  const error = record(value);
  keys(error, ["code", "message", "retryable"]);
  text(error.code, MAX_TEXT);
  text(error.message, MAX_TEXT);
  bool(error.retryable);
}

function operationId(value: unknown): void {
  if (typeof value !== "string" || value.length > MAX_OPERATION_ID || !OPERATION_ID.test(value))
    throw new Error("Invalid review operation ID");
}

function uuid(value: unknown): void {
  if (typeof value !== "string" || !UUID.test(value))
    throw new Error("Invalid review UUID");
}

function handle(value: unknown): void {
  text(value, MAX_HANDLE);
  if ([...(value as string)].some((character) => character.charCodeAt(0) < 32))
    throw new Error("Invalid review handle");
}

function path(value: unknown): void {
  text(value, MAX_PATH);
  if ((value as string).includes("\n") || (value as string).includes("\r"))
    throw new Error("Invalid review path");
}

function body(value: unknown, allowEmpty: boolean): void {
  if (typeof value !== "string" || (!allowEmpty && value.length === 0))
    throw new Error("Invalid review body");
  if (new TextEncoder().encode(value).length > MAX_BODY_BYTES)
    throw new Error("Review body exceeds the byte limit");
}

function text(value: unknown, limit: number): asserts value is string {
  if (typeof value !== "string" || value.length === 0 || value.length > limit)
    throw new Error("Invalid review text");
}

function nullableText(value: unknown, limit: number): void {
  if (value !== null) text(value, limit);
}

function bool(value: unknown): asserts value is boolean {
  if (typeof value !== "boolean") throw new Error("Invalid review boolean");
}

function positiveInteger(value: unknown): asserts value is number {
  if (!Number.isSafeInteger(value) || (value as number) <= 0)
    throw new Error("Invalid positive review integer");
}

function nonnegativeInteger(value: unknown): asserts value is number {
  if (!Number.isSafeInteger(value) || (value as number) < 0)
    throw new Error("Invalid nonnegative review integer");
}

function nullablePositiveInteger(value: unknown): void {
  if (value !== null) positiveInteger(value);
}

function timestamp(value: unknown): void {
  text(value, 64);
  if (Number.isNaN(Date.parse(value))) throw new Error("Invalid review timestamp");
}

function array(value: unknown, limit: number): readonly unknown[] {
  if (!Array.isArray(value) || value.length > limit)
    throw new Error("Invalid review array");
  return value;
}

function oneOf<const T extends string>(value: unknown, values: readonly T[]): T {
  if (typeof value !== "string" || !values.includes(value as T))
    throw new Error("Invalid review enum value");
  return value as T;
}

function keys(
  value: Record<string, unknown>,
  required: readonly string[],
  optional: readonly string[] = [],
): void {
  const allowed = new Set([...required, ...optional]);
  const actual = Object.keys(value);
  if (
    required.some((key) => !Object.hasOwn(value, key)) ||
    actual.some((key) => !allowed.has(key))
  ) {
    throw new Error("Invalid review fields");
  }
}

function record(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    throw new Error("Review value must be an object");
  return value as Record<string, unknown>;
}

