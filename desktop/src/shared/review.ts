import type {
  DesktopRead,
  OpaqueHandle,
  ReviewRevisionDto,
  ServiceErrorDto,
} from "./bridge.js";

export type ReviewVerdict = "comment" | "approve" | "request_changes";
export type DraftState =
  | "editable"
  | "submitting"
  | "partial"
  | "unknown"
  | "submitted";
export type SubmissionOutcome =
  | "submitted"
  | "paused"
  | "unknown"
  | "editable";
export type SubmissionStepKind =
  | "general_comment"
  | "inline_comment"
  | "reply"
  | "body"
  | "verdict"
  | "github_review";
export type ReconciliationResolution =
  | "retry_remaining"
  | "return_editable"
  | "mark_submitted";
export type ReviewAction = "merge" | "close" | "reopen" | "unapprove";
export type SourceCleanupOutcome =
  | "not_requested"
  | "confirmed"
  | "rejected"
  | "unknown";

const REVIEW_MUTATION_MESSAGES: Readonly<Record<string, string>> = Object.freeze({
  authentication_failed: "Sign in to the forge again, then retry the review action.",
  closed: "This review session is closed. Reopen it before continuing.",
  configuration_invalid:
    "Review actions are unavailable because the local service configuration is invalid.",
  conflict: "The review changed remotely. Refresh it before choosing another action.",
  invalid_input: "Check the review action input and the current remote state.",
  invalid_response: "The review action result could not be confirmed.",
  mutation_timeout: "The review action result could not be confirmed.",
  network_unavailable:
    "The forge could not be reached. Refresh remote state before deciding whether to retry.",
  not_found: "The review or selected target no longer exists. Refresh the review.",
  not_running: "Restart the local review service, then refresh the review.",
  not_started: "This review operation has not started. Refresh its status before continuing.",
  permission_denied: "You do not have permission to perform this review action.",
  rate_limited: "The forge rate limit blocked this action. Wait, then refresh before retrying.",
  request_cancelled: "The review action result could not be confirmed.",
  resource_not_issued: "Reload the review before performing this action.",
  revision_changed: "The review revision changed. Reload the latest revision before continuing.",
  revision_unavailable: "The selected review revision is unavailable. Reload the review.",
  shutting_down: "The local review service is shutting down. Restart it and refresh the review.",
  shutdown_failed: "Restart the local review service, then refresh the review.",
  unexpected_eof: "The review action result could not be confirmed.",
  unsupported: "This forge does not support the selected review action.",
  write_failed: "The review action result could not be confirmed.",
});

export function reviewMutationMessage(code: string): string {
  return (
    REVIEW_MUTATION_MESSAGES[code] ??
    "The review action could not be completed. Refresh the review before deciding whether to retry."
  );
}

export interface ReviewMutationCapabilitiesDto {
  readonly general_comment: boolean;
  readonly inline_comment: boolean;
  readonly multiline_comment: boolean;
  readonly reply: boolean;
  readonly resolve: boolean;
  readonly approve: boolean;
  readonly request_changes: boolean;
  readonly comment_verdict: boolean;
  readonly atomic_review_batch: boolean;
}

export interface ReviewActionCapabilitiesDto {
  readonly merge: boolean;
  readonly close: boolean;
  readonly reopen: boolean;
  readonly unapprove: boolean;
}

export interface ReviewCapabilitiesResult<T> {
  readonly review: OpaqueHandle;
  readonly capabilities: T;
}

export interface MutationDiffAnchorDto {
  readonly old_path: string;
  readonly new_path: string;
  readonly line: number;
  readonly side: "LEFT" | "RIGHT";
  readonly start_line?: number | null;
  readonly start_side?: "LEFT" | "RIGHT" | null;
}

export interface DraftInlineAnchorInputDto {
  readonly revision: ReviewRevisionDto;
  readonly old_path: string;
  readonly new_path: string;
  readonly old_line: number | null;
  readonly new_line: number | null;
  readonly side: "old" | "new";
  readonly context_fingerprint: string;
  readonly start_line?: number | null;
  readonly start_side?: "old" | "new" | null;
  readonly stale?: boolean;
}

export interface DraftInlineAnchorDto extends Omit<DraftInlineAnchorInputDto, "start_line" | "start_side" | "stale"> {
  readonly start_line: number | null;
  readonly start_side: "old" | "new" | null;
  readonly stale: boolean;
}

interface DraftCommentBase {
  readonly id: string;
  readonly body: string;
}
export interface GeneralDraftCommentDto extends DraftCommentBase {
  readonly kind: "general";
}
export interface InlineDraftCommentDto extends DraftCommentBase {
  readonly kind: "inline";
  readonly anchor: DraftInlineAnchorDto;
}
export interface ReplyDraftCommentDto extends DraftCommentBase {
  readonly kind: "reply";
  readonly thread_id: string;
}
export type DraftCommentDto =
  | GeneralDraftCommentDto
  | InlineDraftCommentDto
  | ReplyDraftCommentDto;

export type DraftCommentInputDto =
  | GeneralDraftCommentDto
  | (Omit<InlineDraftCommentDto, "anchor"> & {
      readonly anchor: DraftInlineAnchorInputDto;
    })
  | ReplyDraftCommentDto;

export interface DraftContentInputDto {
  readonly body: string;
  readonly verdict: ReviewVerdict | null;
  readonly comments: readonly DraftCommentInputDto[];
}

export interface DraftContentParamsDto {
  readonly body?: string;
  readonly verdict?: ReviewVerdict | null;
  readonly comments?: readonly DraftCommentInputDto[];
}

export interface DraftContentDto extends DraftContentInputDto {
  readonly comments: readonly DraftCommentDto[];
}

export interface DraftSnapshotDto extends DraftContentDto {
  readonly id: string;
  readonly review: OpaqueHandle;
  readonly revision: ReviewRevisionDto;
  readonly version: number;
  readonly state: DraftState;
  readonly created_at: string;
  readonly updated_at: string;
}

export interface MutationReceiptDto {
  readonly remote_id: string;
  readonly comment_id: string | null;
  readonly discussion_id: string | null;
}

export interface MutationOutcomeDto {
  readonly operation_id: string;
  readonly outcome: "known" | "unknown";
  readonly receipt: MutationReceiptDto | null;
  readonly reason: string | null;
  readonly resync_required: boolean;
}

export interface ReviewActionReceiptDto {
  readonly operation_id: string;
  readonly action: ReviewAction;
  readonly review: OpaqueHandle;
  readonly revision: ReviewRevisionDto;
  readonly expected_state: "open" | "closed";
  readonly outcome: "known" | "unknown";
  readonly remote_id: string | null;
  readonly merge_sha: string | null;
  readonly source_cleanup: SourceCleanupOutcome;
  readonly error: ServiceErrorDto | null;
  readonly resync_required: boolean;
}

export interface SubmissionStepDto {
  readonly id: string;
  readonly kind: SubmissionStepKind;
  readonly comment_ids: readonly string[];
}

export interface SubmissionReceiptDto {
  readonly step_id: string;
  readonly remote_id: string;
  readonly recorded_at: string;
  readonly resync_required: boolean;
}

export interface SubmissionFailureDto {
  readonly code: string;
  readonly message: string;
  readonly retryable: boolean;
  readonly step_id: string | null;
}

export interface SubmissionProgressDto {
  readonly attempt_id: string;
  readonly draft_id: string;
  readonly review: OpaqueHandle;
  readonly frozen_version: number;
  readonly state: DraftState;
  readonly outcome: SubmissionOutcome;
  readonly steps: readonly SubmissionStepDto[];
  readonly receipts: readonly SubmissionReceiptDto[];
  readonly completed_step_ids: readonly string[];
  readonly unknown_step_ids: readonly string[];
  readonly atomic: boolean;
  readonly resync_required: boolean;
  readonly failure: SubmissionFailureDto | null;
  readonly plan_available: boolean;
}

export interface ReviewOnlyParams {
  readonly review: OpaqueHandle;
}
export interface OperationParams extends ReviewOnlyParams {
  readonly operation_id: string;
}
export interface RevisionOperationParams extends OperationParams {
  readonly revision: ReviewRevisionDto;
}
export interface GeneralCommentParams extends OperationParams {
  readonly body: string;
}
export interface InlineCommentParams extends RevisionOperationParams {
  readonly anchor: MutationDiffAnchorDto;
  readonly body: string;
}
export interface ReplyParams extends RevisionOperationParams {
  readonly discussion_id: string;
  readonly body: string;
}
export interface ResolveParams extends RevisionOperationParams {
  readonly discussion_id: string;
  readonly resolved: boolean;
}
export interface VerdictInlineCommentDto {
  readonly anchor: MutationDiffAnchorDto;
  readonly body: string;
}
export interface VerdictParams extends RevisionOperationParams {
  readonly verdict: ReviewVerdict;
  readonly body?: string;
  readonly inline_comments?: readonly VerdictInlineCommentDto[];
}
export interface MergeParams extends RevisionOperationParams {
  readonly squash?: boolean;
  readonly source_cleanup?: { readonly branch: string } | null;
}
export interface ActionReceiptParams extends RevisionOperationParams {
  readonly action: ReviewAction;
}
export interface CreateDraftParams extends ReviewOnlyParams {
  readonly revision: ReviewRevisionDto;
  readonly content?: DraftContentParamsDto;
}
export interface GetDraftParams extends ReviewOnlyParams {
  readonly draft_id: string;
}
export interface ListDraftsParams extends ReviewOnlyParams {
  readonly states?: readonly DraftState[];
  readonly cursor?: number;
  readonly max_items?: number;
}
export interface SaveDraftParams extends GetDraftParams {
  readonly expected_version: number;
  readonly content: DraftContentParamsDto;
}
export interface DiscardDraftParams extends GetDraftParams {
  readonly expected_version: number;
}
export interface StartSubmissionParams extends DiscardDraftParams {}
export interface AttemptParams extends ReviewOnlyParams {
  readonly attempt_id: string;
}
export interface ListSubmissionsParams extends ReviewOnlyParams {
  readonly cursor?: number;
  readonly max_items?: number;
}
export interface ReconcileSubmissionParams extends AttemptParams {
  readonly resolution: ReconciliationResolution;
}

export interface DraftListResult {
  readonly cursor: number;
  readonly next_cursor: number | null;
  readonly drafts: readonly DraftSnapshotDto[];
}
export interface SubmissionListResult {
  readonly cursor: number;
  readonly next_cursor: number | null;
  readonly attempts: readonly SubmissionProgressDto[];
}

export interface ReviewRpcContract {
  readonly "review_mutations.capabilities": {
    readonly params: ReviewOnlyParams;
    readonly result: ReviewCapabilitiesResult<ReviewMutationCapabilitiesDto>;
    readonly mutation: false;
  };
  readonly "review_mutations.comment": {
    readonly params: GeneralCommentParams;
    readonly result: MutationOutcomeDto;
    readonly mutation: true;
  };
  readonly "review_mutations.inline_comment": {
    readonly params: InlineCommentParams;
    readonly result: MutationOutcomeDto;
    readonly mutation: true;
  };
  readonly "review_mutations.reply": {
    readonly params: ReplyParams;
    readonly result: MutationOutcomeDto;
    readonly mutation: true;
  };
  readonly "review_mutations.resolve": {
    readonly params: ResolveParams;
    readonly result: MutationOutcomeDto;
    readonly mutation: true;
  };
  readonly "review_mutations.verdict": {
    readonly params: VerdictParams;
    readonly result: MutationOutcomeDto;
    readonly mutation: true;
  };
  readonly "review_actions.capabilities": {
    readonly params: ReviewOnlyParams;
    readonly result: ReviewCapabilitiesResult<ReviewActionCapabilitiesDto>;
    readonly mutation: false;
  };
  readonly "review_actions.merge": {
    readonly params: MergeParams;
    readonly result: ReviewActionReceiptDto;
    readonly mutation: true;
  };
  readonly "review_actions.close": {
    readonly params: RevisionOperationParams;
    readonly result: ReviewActionReceiptDto;
    readonly mutation: true;
  };
  readonly "review_actions.reopen": {
    readonly params: RevisionOperationParams;
    readonly result: ReviewActionReceiptDto;
    readonly mutation: true;
  };
  readonly "review_actions.unapprove": {
    readonly params: RevisionOperationParams;
    readonly result: ReviewActionReceiptDto;
    readonly mutation: true;
  };
  readonly "review_actions.receipt": {
    readonly params: ActionReceiptParams;
    readonly result: { readonly receipt: ReviewActionReceiptDto | null };
    readonly mutation: false;
  };
  readonly "drafts.create": {
    readonly params: CreateDraftParams;
    readonly result: DraftSnapshotDto;
    readonly mutation: true;
  };
  readonly "drafts.get": {
    readonly params: GetDraftParams;
    readonly result: DraftSnapshotDto;
    readonly mutation: false;
  };
  readonly "drafts.list": {
    readonly params: ListDraftsParams;
    readonly result: DraftListResult;
    readonly mutation: false;
  };
  readonly "drafts.save": {
    readonly params: SaveDraftParams;
    readonly result: DraftSnapshotDto;
    readonly mutation: true;
  };
  readonly "drafts.discard": {
    readonly params: DiscardDraftParams;
    readonly result: { readonly discarded: DraftSnapshotDto };
    readonly mutation: true;
  };
  readonly "review_submissions.start": {
    readonly params: StartSubmissionParams;
    readonly result: SubmissionProgressDto;
    readonly mutation: true;
  };
  readonly "review_submissions.status": {
    readonly params: AttemptParams;
    readonly result: SubmissionProgressDto;
    readonly mutation: false;
  };
  readonly "review_submissions.list": {
    readonly params: ListSubmissionsParams;
    readonly result: SubmissionListResult;
    readonly mutation: false;
  };
  readonly "review_submissions.resume": {
    readonly params: AttemptParams;
    readonly result: SubmissionProgressDto;
    readonly mutation: true;
  };
  readonly "review_submissions.reconcile": {
    readonly params: ReconcileSubmissionParams;
    readonly result: SubmissionProgressDto;
    readonly mutation: true;
  };
}

export type ReviewRpcMethod = keyof ReviewRpcContract;
export type ReviewRpcParams<M extends ReviewRpcMethod> =
  ReviewRpcContract[M]["params"];
export type ReviewRpcResult<M extends ReviewRpcMethod> =
  ReviewRpcContract[M]["result"];
export type ReviewMutationMethod = {
  [M in ReviewRpcMethod]: ReviewRpcContract[M]["mutation"] extends true
    ? M
    : never;
}[ReviewRpcMethod];
export type ReviewReadMethod = Exclude<ReviewRpcMethod, ReviewMutationMethod>;

export type ReviewMutationIPCResult<T> =
  | { readonly result: T; readonly error: null }
  | {
      readonly result: null;
      readonly error: ServiceErrorDto;
    };

export interface ReviewDesktopBridge {
  getReviewMutationCapabilities(
    review: OpaqueHandle,
  ): DesktopRead<ReviewCapabilitiesResult<ReviewMutationCapabilitiesDto>>;
  postReviewComment(params: GeneralCommentParams): Promise<MutationOutcomeDto>;
  postInlineReviewComment(
    params: InlineCommentParams,
  ): Promise<MutationOutcomeDto>;
  replyReviewDiscussion(params: ReplyParams): Promise<MutationOutcomeDto>;
  resolveReviewDiscussion(params: ResolveParams): Promise<MutationOutcomeDto>;
  submitReviewVerdict(params: VerdictParams): Promise<MutationOutcomeDto>;
  getReviewActionCapabilities(
    review: OpaqueHandle,
  ): DesktopRead<ReviewCapabilitiesResult<ReviewActionCapabilitiesDto>>;
  mergeReview(params: MergeParams): Promise<ReviewActionReceiptDto>;
  closeReview(params: RevisionOperationParams): Promise<ReviewActionReceiptDto>;
  reopenReview(params: RevisionOperationParams): Promise<ReviewActionReceiptDto>;
  unapproveReview(
    params: RevisionOperationParams,
  ): Promise<ReviewActionReceiptDto>;
  getReviewActionReceipt(
    params: ActionReceiptParams,
  ): DesktopRead<{ readonly receipt: ReviewActionReceiptDto | null }>;
  createReviewDraft(params: CreateDraftParams): Promise<DraftSnapshotDto>;
  getReviewDraft(params: GetDraftParams): DesktopRead<DraftSnapshotDto>;
  listReviewDrafts(params: ListDraftsParams): DesktopRead<DraftListResult>;
  saveReviewDraft(params: SaveDraftParams): Promise<DraftSnapshotDto>;
  discardReviewDraft(
    params: DiscardDraftParams,
  ): Promise<{ readonly discarded: DraftSnapshotDto }>;
  startReviewSubmission(
    params: StartSubmissionParams,
  ): Promise<SubmissionProgressDto>;
  getReviewSubmission(
    params: AttemptParams,
  ): DesktopRead<SubmissionProgressDto>;
  listReviewSubmissions(
    params: ListSubmissionsParams,
  ): DesktopRead<SubmissionListResult>;
  resumeReviewSubmission(
    params: AttemptParams,
  ): Promise<SubmissionProgressDto>;
  reconcileReviewSubmission(
    params: ReconcileSubmissionParams,
  ): Promise<SubmissionProgressDto>;
}

export const REVIEW_IPC_CHANNELS = Object.freeze({
  mutationCapabilities: "tongs:review-mutations.capabilities",
  comment: "tongs:review-mutations.comment",
  inlineComment: "tongs:review-mutations.inline-comment",
  reply: "tongs:review-mutations.reply",
  resolve: "tongs:review-mutations.resolve",
  verdict: "tongs:review-mutations.verdict",
  actionCapabilities: "tongs:review-actions.capabilities",
  merge: "tongs:review-actions.merge",
  close: "tongs:review-actions.close",
  reopen: "tongs:review-actions.reopen",
  unapprove: "tongs:review-actions.unapprove",
  actionReceipt: "tongs:review-actions.receipt",
  createDraft: "tongs:review-drafts.create",
  getDraft: "tongs:review-drafts.get",
  listDrafts: "tongs:review-drafts.list",
  saveDraft: "tongs:review-drafts.save",
  discardDraft: "tongs:review-drafts.discard",
  startSubmission: "tongs:review-submissions.start",
  getSubmission: "tongs:review-submissions.status",
  listSubmissions: "tongs:review-submissions.list",
  resumeSubmission: "tongs:review-submissions.resume",
  reconcileSubmission: "tongs:review-submissions.reconcile",
} as const);
