import type { OpaqueHandle } from "./bridge.js";

export type EditorOpenOutcome =
  | "started"
  | "busy"
  | "disabled"
  | "missing"
  | "malformed"
  | "terminal_unsupported"
  | "log_too_large"
  | "capacity_exceeded"
  | "failed";

export interface UtilityResult<Outcome extends string> {
  readonly outcome: Outcome;
  readonly message: string;
}

export type CopyReviewUrlResult = UtilityResult<"copied" | "failed">;
export type ClearCacheResult = UtilityResult<"cleared" | "failed">;
export type OpenJobLogEditorResult = UtilityResult<EditorOpenOutcome>;

export interface WorkspaceUtilityBridge {
  copyReviewUrl(review: OpaqueHandle): Promise<CopyReviewUrlResult>;
  clearCache(): Promise<ClearCacheResult>;
  openJobLogInEditor(job: OpaqueHandle): Promise<OpenJobLogEditorResult>;
}

export const UTILITY_IPC_CHANNELS = Object.freeze({
  copyReviewUrl: "tongs:utilities.copy-review-url",
  clearCache: "tongs:utilities.clear-cache",
  openJobLogInEditor: "tongs:utilities.open-job-log-in-editor",
} as const);

export const UTILITY_PROTOCOL_METHODS = Object.freeze([
  "utilities.review_url",
  "utilities.cache_clear",
  "utilities.job_log_export",
  "utilities.job_log_release",
] as const);
