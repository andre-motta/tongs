const { contextBridge, ipcRenderer } = require("electron") as typeof import("electron");
import type {
  AcceptedResult, AssetDescriptor, CommitsResult, DesktopBridge, DesktopEvent,
  DesktopRead, DiffPage, DiscussionsResult, InvokePluginParams, JobsResult,
  JsonValue, ListPipelinesParams, ListReviewPipelinesParams, ListReviewsParams,
  LocationParams, LogPage, OpenDiffParams, OpenLogParams, OpenRepositoryParams,
  PageParams, PipelinesResult, PluginResult, PluginsResult,
  RepositoryDto, RepositoryListResult, ReviewListResult, ReviewSnapshotDto,
} from "../shared/bridge.js";
import type {
  CICapabilitiesResult,
  CIReceiptParams,
  CIReceiptResult,
  CIMutationReceipt,
  CIMutationIPCResult,
  JobMutationParams,
  PipelineMutationParams,
} from "../shared/ci.js";
import type {
  ActionReceiptParams,
  AttemptParams,
  CreateDraftParams,
  DiscardDraftParams,
  DraftListResult,
  DraftSnapshotDto,
  GeneralCommentParams,
  InlineCommentParams,
  ListDraftsParams,
  ListSubmissionsParams,
  MergeParams,
  MutationOutcomeDto,
  ReconcileSubmissionParams,
  ReplyParams,
  ResolveParams,
  ReviewActionCapabilitiesDto,
  ReviewActionReceiptDto,
  ReviewCapabilitiesResult,
  ReviewMutationCapabilitiesDto,
  ReviewMutationIPCResult,
  RevisionOperationParams,
  SaveDraftParams,
  StartSubmissionParams,
  SubmissionListResult,
  SubmissionProgressDto,
  VerdictParams,
} from "../shared/review.js";

const IPC_CHANNELS = Object.freeze({
  discoverRepositories: "tongs:repositories.discover", openRepository: "tongs:repositories.open", listReviews: "tongs:reviews.list", getReview: "tongs:reviews.get",
  openDiff: "tongs:diff.open", pageDiff: "tongs:diff.page", listDiscussions: "tongs:discussions.list", listCommits: "tongs:commits.list",
  listPipelines: "tongs:pipelines.list", listReviewPipelines: "tongs:review-pipelines.list", listJobs: "tongs:jobs.list", openLog: "tongs:logs.open", pageLog: "tongs:logs.page",
  listPlugins: "tongs:plugins.list", invokePlugin: "tongs:plugins.invoke", setLocation: "tongs:host.set-location", listAssets: "tongs:assets.list",
  cancelRead: "tongs:read.cancel", openExternal: "tongs:external.open", event: "tongs:event",
} as const);
const CI_IPC_CHANNELS = Object.freeze({
  capabilities: "tongs:ci.capabilities",
  retryPipeline: "tongs:pipelines.retry",
  cancelPipeline: "tongs:pipelines.cancel",
  retryJob: "tongs:jobs.retry",
  cancelJob: "tongs:jobs.cancel",
  receipt: "tongs:ci.receipt",
} as const);
const REVIEW_IPC_CHANNELS = Object.freeze({
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

const owned = new Set<string>();
function read<T>(channel: string, params: object): DesktopRead<T> {
  const requestToken = crypto.randomUUID();
  owned.add(requestToken);
  const result = ipcRenderer.invoke(channel, { requestToken, params }).finally(() => owned.delete(requestToken)) as Promise<T>;
  return Object.freeze({ requestToken, result });
}
async function mutate(
  channel: string,
  params: PipelineMutationParams | JobMutationParams,
): Promise<CIMutationReceipt> {
  const value = (await ipcRenderer.invoke(channel, params)) as CIMutationIPCResult;
  if (value.error !== null) throw value.error;
  return value.receipt;
}
async function mutateReview<T>(channel: string, params: object): Promise<T> {
  const value = (await ipcRenderer.invoke(
    channel,
    params,
  )) as ReviewMutationIPCResult<T>;
  if (value.error !== null) throw value.error;
  return value.result;
}
const bridge: DesktopBridge = Object.freeze({
  discoverRepositories: (): DesktopRead<RepositoryListResult> => read(IPC_CHANNELS.discoverRepositories, {}),
  openRepository: (params: OpenRepositoryParams): DesktopRead<RepositoryDto> => read(IPC_CHANNELS.openRepository, params),
  listReviews: (params: ListReviewsParams): DesktopRead<ReviewListResult> => read(IPC_CHANNELS.listReviews, params),
  getReview: (review: string): DesktopRead<ReviewSnapshotDto> => read(IPC_CHANNELS.getReview, { review }),
  openDiff: (params: OpenDiffParams): DesktopRead<DiffPage> => read(IPC_CHANNELS.openDiff, params),
  pageDiff: (params: PageParams): DesktopRead<DiffPage> => read(IPC_CHANNELS.pageDiff, params),
  listDiscussions: (review: string): DesktopRead<DiscussionsResult> => read(IPC_CHANNELS.listDiscussions, { review }),
  listCommits: (review: string): DesktopRead<CommitsResult> => read(IPC_CHANNELS.listCommits, { review }),
  listPipelines: (params: ListPipelinesParams): DesktopRead<PipelinesResult> => read(IPC_CHANNELS.listPipelines, params),
  listReviewPipelines: (params: ListReviewPipelinesParams): DesktopRead<PipelinesResult> => read(IPC_CHANNELS.listReviewPipelines, params),
  listJobs: (pipeline: string): DesktopRead<JobsResult> => read(IPC_CHANNELS.listJobs, { pipeline }),
  openLog: (params: OpenLogParams): DesktopRead<LogPage> => read(IPC_CHANNELS.openLog, params),
  pageLog: (params: PageParams): DesktopRead<LogPage> => read(IPC_CHANNELS.pageLog, params),
  getCICapabilities: (repository: string): DesktopRead<CICapabilitiesResult> => read(CI_IPC_CHANNELS.capabilities, { repository }),
  retryPipeline: (params: PipelineMutationParams): Promise<CIMutationReceipt> => mutate(CI_IPC_CHANNELS.retryPipeline, params),
  cancelPipeline: (params: PipelineMutationParams): Promise<CIMutationReceipt> => mutate(CI_IPC_CHANNELS.cancelPipeline, params),
  retryJob: (params: JobMutationParams): Promise<CIMutationReceipt> => mutate(CI_IPC_CHANNELS.retryJob, params),
  cancelJob: (params: JobMutationParams): Promise<CIMutationReceipt> => mutate(CI_IPC_CHANNELS.cancelJob, params),
  getCIReceipt: (params: CIReceiptParams): DesktopRead<CIReceiptResult> => read(CI_IPC_CHANNELS.receipt, params),
  getReviewMutationCapabilities: (review: string): DesktopRead<ReviewCapabilitiesResult<ReviewMutationCapabilitiesDto>> => read(REVIEW_IPC_CHANNELS.mutationCapabilities, { review }),
  postReviewComment: (params: GeneralCommentParams): Promise<MutationOutcomeDto> => mutateReview(REVIEW_IPC_CHANNELS.comment, params),
  postInlineReviewComment: (params: InlineCommentParams): Promise<MutationOutcomeDto> => mutateReview(REVIEW_IPC_CHANNELS.inlineComment, params),
  replyReviewDiscussion: (params: ReplyParams): Promise<MutationOutcomeDto> => mutateReview(REVIEW_IPC_CHANNELS.reply, params),
  resolveReviewDiscussion: (params: ResolveParams): Promise<MutationOutcomeDto> => mutateReview(REVIEW_IPC_CHANNELS.resolve, params),
  submitReviewVerdict: (params: VerdictParams): Promise<MutationOutcomeDto> => mutateReview(REVIEW_IPC_CHANNELS.verdict, params),
  getReviewActionCapabilities: (review: string): DesktopRead<ReviewCapabilitiesResult<ReviewActionCapabilitiesDto>> => read(REVIEW_IPC_CHANNELS.actionCapabilities, { review }),
  mergeReview: (params: MergeParams): Promise<ReviewActionReceiptDto> => mutateReview(REVIEW_IPC_CHANNELS.merge, params),
  closeReview: (params: RevisionOperationParams): Promise<ReviewActionReceiptDto> => mutateReview(REVIEW_IPC_CHANNELS.close, params),
  reopenReview: (params: RevisionOperationParams): Promise<ReviewActionReceiptDto> => mutateReview(REVIEW_IPC_CHANNELS.reopen, params),
  unapproveReview: (params: RevisionOperationParams): Promise<ReviewActionReceiptDto> => mutateReview(REVIEW_IPC_CHANNELS.unapprove, params),
  getReviewActionReceipt: (params: ActionReceiptParams): DesktopRead<{ readonly receipt: ReviewActionReceiptDto | null }> => read(REVIEW_IPC_CHANNELS.actionReceipt, params),
  createReviewDraft: (params: CreateDraftParams): Promise<DraftSnapshotDto> => mutateReview(REVIEW_IPC_CHANNELS.createDraft, params),
  getReviewDraft: (params: { readonly review: string; readonly draft_id: string }): DesktopRead<DraftSnapshotDto> => read(REVIEW_IPC_CHANNELS.getDraft, params),
  listReviewDrafts: (params: ListDraftsParams): DesktopRead<DraftListResult> => read(REVIEW_IPC_CHANNELS.listDrafts, params),
  saveReviewDraft: (params: SaveDraftParams): Promise<DraftSnapshotDto> => mutateReview(REVIEW_IPC_CHANNELS.saveDraft, params),
  discardReviewDraft: (params: DiscardDraftParams): Promise<{ readonly discarded: DraftSnapshotDto }> => mutateReview(REVIEW_IPC_CHANNELS.discardDraft, params),
  startReviewSubmission: (params: StartSubmissionParams): Promise<SubmissionProgressDto> => mutateReview(REVIEW_IPC_CHANNELS.startSubmission, params),
  getReviewSubmission: (params: AttemptParams): DesktopRead<SubmissionProgressDto> => read(REVIEW_IPC_CHANNELS.getSubmission, params),
  listReviewSubmissions: (params: ListSubmissionsParams): DesktopRead<SubmissionListResult> => read(REVIEW_IPC_CHANNELS.listSubmissions, params),
  resumeReviewSubmission: (params: AttemptParams): Promise<SubmissionProgressDto> => mutateReview(REVIEW_IPC_CHANNELS.resumeSubmission, params),
  reconcileReviewSubmission: (params: ReconcileSubmissionParams): Promise<SubmissionProgressDto> => mutateReview(REVIEW_IPC_CHANNELS.reconcileSubmission, params),
  listPlugins: (): DesktopRead<PluginsResult> => read(IPC_CHANNELS.listPlugins, {}),
  invokePlugin: (params: InvokePluginParams): DesktopRead<PluginResult> => read(IPC_CHANNELS.invokePlugin, params),
  setLocation: async (params: LocationParams): Promise<AcceptedResult> => ipcRenderer.invoke(IPC_CHANNELS.setLocation, params) as Promise<AcceptedResult>,
  listAssets: (): DesktopRead<readonly AssetDescriptor[]> => read(IPC_CHANNELS.listAssets, {}),
  cancelRead: async (token: string): Promise<boolean> => owned.has(token) && await ipcRenderer.invoke(IPC_CHANNELS.cancelRead, token) === true,
  onEvent: (listener: (event: DesktopEvent) => void): (() => void) => {
    const wrapped = (_event: unknown, value: JsonValue): void => listener(value as unknown as DesktopEvent);
    ipcRenderer.on(IPC_CHANNELS.event, wrapped);
    return () => ipcRenderer.removeListener(IPC_CHANNELS.event, wrapped);
  },
  openExternal: async (url: string): Promise<boolean> => await ipcRenderer.invoke(IPC_CHANNELS.openExternal, url) === true,
});
contextBridge.exposeInMainWorld("tongs", bridge);
