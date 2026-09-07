export const PROTOCOL_MAJOR = 1 as const;
export const REQUIRED_CAPABILITIES = Object.freeze([
  "assets", "cancellation", "events", "opaque_handles", "paged_diffs",
  "paged_logs", "plugins",
] as const);

export type JsonScalar = string | number | boolean | null;
export type JsonValue = JsonScalar | JsonValue[] | { [key: string]: JsonValue };
export type JsonObject = { [key: string]: JsonValue };
export type OpaqueHandle = string;

export interface ServiceErrorDto { readonly code: string; readonly message: string; readonly retryable: boolean; }
export interface UserDto { readonly username: string; readonly display_name: string; }
export interface ReviewRevisionDto { readonly head_sha: string; readonly base_sha: string; readonly start_sha: string | null; }
export interface ForgeCapabilitiesDto { readonly batched_review: boolean; readonly thread_resolution: boolean; readonly draft_notes: boolean; readonly unapprove: boolean; readonly job_cancel: boolean; }
export interface RepositoryDto { readonly handle: OpaqueHandle; readonly display_name: string; readonly forge_type: "github" | "gitlab"; }
export interface RepositoryListResult { readonly repositories: readonly RepositoryDto[]; }
export interface OpenRepositoryParams { readonly hostname: string; readonly project_path: string; }

export interface ReviewSummaryDto {
  readonly number: number; readonly title: string; readonly author: UserDto;
  readonly state: string; readonly is_draft: boolean; readonly source_branch: string;
  readonly target_branch: string; readonly ci_status: string; readonly created_at: string;
  readonly updated_at: string; readonly web_url: string; readonly comment_count: number;
  readonly has_conflicts: boolean; readonly labels: readonly string[];
  readonly review_decision: string | null; readonly additions: number | null;
  readonly deletions: number | null;
}
export interface ReviewDetailDto extends ReviewSummaryDto {
  readonly description: string; readonly merge_status: string;
  readonly approvals: readonly UserDto[]; readonly reviewers: readonly UserDto[];
  readonly assignees: readonly UserDto[]; readonly changes_count: number;
  readonly detailed_merge_status: string | null; readonly draft_notes_count: number | null;
  readonly status_check_rollup: string | null;
}
export interface ReviewListItemDto { readonly handle: OpaqueHandle; readonly repository: OpaqueHandle; readonly summary: ReviewSummaryDto; }
export interface ReviewFailureDto extends ServiceErrorDto { readonly repository: OpaqueHandle | null; }
export interface ListReviewsParams { readonly scope: string; readonly repository?: OpaqueHandle; readonly state?: string; readonly per_page?: number; }
export interface ReviewListResult { readonly items: readonly ReviewListItemDto[]; readonly failures: readonly ReviewFailureDto[]; }
export interface ReviewSnapshotDto {
  readonly handle: OpaqueHandle; readonly repository: OpaqueHandle;
  readonly detail: ReviewDetailDto; readonly capabilities: ForgeCapabilitiesDto;
  readonly revision: ReviewRevisionDto | null; readonly revision_error: ServiceErrorDto | null;
}

export interface DiffFileRow { readonly kind: "file"; readonly file_index: number; readonly old_path: string; readonly new_path: string; readonly status: string; readonly additions: number; readonly deletions: number; readonly is_binary: boolean; readonly language: string | null; readonly is_truncated: boolean; readonly is_empty: boolean; readonly is_mode_only: boolean; readonly is_unavailable: boolean; }
export interface DiffHunkRow { readonly kind: "hunk"; readonly file_index: number; readonly hunk_index: number; readonly header: string; readonly old_start: number; readonly old_count: number; readonly new_start: number; readonly new_count: number; readonly context_text: string; }
export interface DiffLineRow { readonly kind: "line"; readonly file_index: number; readonly hunk_index: number; readonly old_line: number | null; readonly new_line: number | null; readonly content: string; readonly line_type: string; }
export type DiffRow = DiffFileRow | DiffHunkRow | DiffLineRow;
export interface SnapshotPage<T, R extends JsonObject> { readonly snapshot_id: string; readonly resource: OpaqueHandle; readonly revision: R; readonly cursor: number; readonly next_cursor: number | null; readonly entries: readonly T[]; }
export type DiffPage = SnapshotPage<DiffRow, ReviewRevisionDto & JsonObject>;
export interface OpenDiffParams { readonly review: OpaqueHandle; readonly max_items?: number; }
export interface PageParams { readonly snapshot: string; readonly resource: OpaqueHandle; readonly cursor: number; readonly max_items?: number; }

export interface InlineCommentDto { readonly id: string; readonly author: UserDto; readonly body: string; readonly created_at: string; readonly file_path: string; readonly old_line: number | null; readonly new_line: number | null; readonly is_resolved: boolean; readonly replies: readonly InlineCommentDto[]; }
export interface DiscussionDto { readonly id: string; readonly is_inline: boolean; readonly root_comment: InlineCommentDto; readonly is_resolved: boolean; readonly resolvable: boolean; }
export interface DiscussionsResult { readonly discussions: readonly DiscussionDto[]; }
export interface CommitDto { readonly sha: string; readonly short_sha: string; readonly title: string; readonly message: string; readonly author: UserDto; readonly created_at: string | null; readonly web_url: string; }
export interface CommitsResult { readonly commits: readonly CommitDto[]; }
export interface PipelineDto { readonly id: number; readonly status: string; readonly ref: string; readonly sha: string; readonly web_url: string; readonly source: string; readonly created_at: string | null; readonly finished_at: string | null; readonly duration_seconds: number | null; }
export interface PipelineItemDto { readonly handle: OpaqueHandle; readonly value: PipelineDto; }
export interface PipelinesResult { readonly pipelines: readonly PipelineItemDto[]; }
export interface ListPipelinesParams { readonly repository: OpaqueHandle; readonly per_page?: number; }
export interface ListReviewPipelinesParams { readonly review: OpaqueHandle; readonly per_page?: number; }
export interface JobDto { readonly id: number; readonly name: string; readonly stage: string; readonly status: string; readonly web_url: string; readonly started_at: string | null; readonly finished_at: string | null; readonly duration_seconds: number | null; readonly allow_failure: boolean; }
export interface JobItemDto { readonly handle: OpaqueHandle; readonly value: JobDto; }
export interface JobsResult { readonly jobs: readonly JobItemDto[]; }
export interface LogRow { readonly text: string; }
export interface LogRevisionDto extends JsonObject { readonly sha256: string; readonly byte_count: number; }
export type LogPage = SnapshotPage<LogRow, LogRevisionDto>;
export interface OpenLogParams { readonly job: OpaqueHandle; readonly max_items?: number; }

export interface PluginManifestDto { readonly title: string; readonly version: string; readonly api_major: number; readonly modules: readonly JsonObject[]; readonly navigation: readonly JsonObject[]; readonly commands: readonly JsonObject[]; readonly methods: readonly string[]; readonly events: readonly string[]; readonly focus_targets: readonly JsonObject[]; readonly help_asset: OpaqueHandle | null; readonly reads: readonly string[]; readonly assets_available: boolean; }
export interface PluginDto { readonly plugin_id: string; readonly state: string; readonly has_terminal_entry_point: boolean; readonly has_desktop_entry_point: boolean; readonly error: ServiceErrorDto | null; readonly manifest: PluginManifestDto | null; }
export interface PluginsResult { readonly plugins: readonly PluginDto[]; }
export interface InvokePluginParams { readonly plugin: string; readonly method: string; readonly params: JsonObject; }
export interface PluginResult { readonly value: JsonValue; }
export interface LocationParams { readonly location: JsonObject | null; }
export interface AcceptedResult { readonly accepted: true; }

export interface DesktopEvent { readonly sequence: number; readonly name: string; readonly data: JsonValue; }
export interface AssetDescriptor { readonly source: "core" | "plugin"; readonly asset_id: string; readonly plugin_id: string | null; readonly kind: string; readonly media_type: string; readonly byte_count: number; readonly sha256: string; readonly url: string; }
export interface DesktopRead<T> { readonly requestToken: string; readonly result: Promise<T>; }

export interface DesktopBridge {
  discoverRepositories(): DesktopRead<RepositoryListResult>;
  openRepository(params: OpenRepositoryParams): DesktopRead<RepositoryDto>;
  listReviews(params: ListReviewsParams): DesktopRead<ReviewListResult>;
  getReview(review: OpaqueHandle): DesktopRead<ReviewSnapshotDto>;
  openDiff(params: OpenDiffParams): DesktopRead<DiffPage>;
  pageDiff(params: PageParams): DesktopRead<DiffPage>;
  listDiscussions(review: OpaqueHandle): DesktopRead<DiscussionsResult>;
  listCommits(review: OpaqueHandle): DesktopRead<CommitsResult>;
  listPipelines(params: ListPipelinesParams): DesktopRead<PipelinesResult>;
  listReviewPipelines(params: ListReviewPipelinesParams): DesktopRead<PipelinesResult>;
  listJobs(pipeline: OpaqueHandle): DesktopRead<JobsResult>;
  openLog(params: OpenLogParams): DesktopRead<LogPage>;
  pageLog(params: PageParams): DesktopRead<LogPage>;
  listPlugins(): DesktopRead<PluginsResult>;
  invokePlugin(params: InvokePluginParams): DesktopRead<PluginResult>;
  setLocation(params: LocationParams): Promise<AcceptedResult>;
  listAssets(): DesktopRead<readonly AssetDescriptor[]>;
  cancelRead(requestToken: string): Promise<boolean>;
  onEvent(listener: (event: DesktopEvent) => void): () => void;
  openExternal(url: string): Promise<boolean>;
}

export const IPC_CHANNELS = Object.freeze({
  discoverRepositories: "tongs:repositories.discover", openRepository: "tongs:repositories.open",
  listReviews: "tongs:reviews.list", getReview: "tongs:reviews.get", openDiff: "tongs:diff.open",
  pageDiff: "tongs:diff.page", listDiscussions: "tongs:discussions.list", listCommits: "tongs:commits.list",
  listPipelines: "tongs:pipelines.list", listReviewPipelines: "tongs:review-pipelines.list", listJobs: "tongs:jobs.list",
  openLog: "tongs:logs.open", pageLog: "tongs:logs.page", listPlugins: "tongs:plugins.list",
  invokePlugin: "tongs:plugins.invoke", setLocation: "tongs:host.set-location", listAssets: "tongs:assets.list",
  cancelRead: "tongs:read.cancel", openExternal: "tongs:external.open", event: "tongs:event",
} as const);

declare global { interface Window { readonly tongs: DesktopBridge; } }
