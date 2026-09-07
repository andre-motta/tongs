import type { IpcMainInvokeEvent, WebContents } from "electron";
import type { JsonObject, JsonValue } from "../shared/bridge.js";

export const APP_ORIGIN = "tongs://app";
export const APP_DOCUMENT = `${APP_ORIGIN}/index.html`;
export const CONTENT_SECURITY_POLICY = [
  "default-src 'none'", "script-src 'self'", "style-src 'self'",
  "img-src 'self' data:", "font-src 'self'", "connect-src 'none'",
  "object-src 'none'", "base-uri 'none'", "form-action 'none'", "frame-ancestors 'none'",
].join("; ");

const MAX_IPC_BYTES = 256 * 1024;
const MAX_TEXT = 2048;

export function assertAuthorizedSender(event: IpcMainInvokeEvent, owner: WebContents): void {
  if (event.sender !== owner || event.senderFrame !== owner.mainFrame || event.senderFrame.url !== APP_DOCUMENT) {
    throw new Error("Unauthorized desktop IPC sender");
  }
}

export function assertParams(method: string, value: unknown): asserts value is JsonObject {
  if (!isRecord(value)) throw new Error("Desktop parameters must be an object");
  let encoded: string;
  try { encoded = JSON.stringify(value); } catch { throw new Error("Invalid desktop parameters"); }
  if (Buffer.byteLength(encoded, "utf8") > MAX_IPC_BYTES) throw new Error("Desktop parameters are too large");
  const specs: Record<string, { required: readonly string[]; optional?: readonly string[] }> = {
    "repositories.discover": { required: [] },
    "repositories.open": { required: ["hostname", "project_path"] },
    "reviews.list": { required: ["scope"], optional: ["repository", "state", "per_page"] },
    "reviews.get": { required: ["review"] }, "discussions.list": { required: ["review"] },
    "commits.list": { required: ["review"] }, "jobs.list": { required: ["pipeline"] },
    "diff.open": { required: ["review"], optional: ["max_items"] },
    "logs.open": { required: ["job"], optional: ["max_items"] },
    "diff.page": { required: ["snapshot", "resource", "cursor"], optional: ["max_items"] },
    "logs.page": { required: ["snapshot", "resource", "cursor"], optional: ["max_items"] },
    "pipelines.list": { required: ["repository"], optional: ["per_page"] },
    "review_pipelines.list": { required: ["review"], optional: ["per_page"] },
    "plugins.list": { required: [] }, "assets.list": { required: [] },
    "plugins.invoke": { required: ["plugin", "method", "params"] },
    "host.set_location": { required: ["location"] },
  };
  const spec = specs[method];
  if (!spec) throw new Error("Unsupported desktop operation");
  const allowed = new Set([...spec.required, ...(spec.optional ?? [])]);
  if (Object.keys(value).some((key) => !allowed.has(key)) || spec.required.some((key) => !Object.hasOwn(value, key))) throw new Error("Invalid desktop parameter fields");
  for (const [key, item] of Object.entries(value)) {
    if (["per_page", "max_items", "cursor"].includes(key)) {
      const maximum = key === "cursor" ? Number.MAX_SAFE_INTEGER : key === "per_page" ? 100 : 1000;
      if (!Number.isSafeInteger(item) || Number(item) < (key === "cursor" ? 0 : 1) || Number(item) > maximum) throw new Error("Invalid desktop numeric parameter");
    } else if (key === "params" || key === "location") {
      if (item !== null && !isRecord(item)) throw new Error("Invalid desktop object parameter");
      assertJson(item);
    } else if (item !== null && (typeof item !== "string" || item.length === 0 || item.length > MAX_TEXT)) {
      throw new Error("Invalid desktop text parameter");
    }
  }
  if (method === "reviews.list" && !["all_open", "my_reviews", "my_mrs"].includes(String(value.scope))) throw new Error("Invalid review scope");
  if (value.state !== undefined && !["open", "closed", "merged"].includes(String(value.state))) throw new Error("Invalid review state");
}

export function assertResult(method: string, value: unknown): asserts value is JsonValue {
  assertJson(value);
  if (method === "repositories.discover") return assertArrayField(value, "repositories", assertRepository);
  if (method === "repositories.open") return assertRepository(value);
  if (method === "reviews.list") {
    assertKeys(value, ["items", "failures"]);
    assertArray(value.items, (item) => { assertKeys(item, ["handle", "repository", "summary"]); text(item.handle); text(item.repository); assertReviewSummary(item.summary); });
    return assertArray(value.failures, (failure) => { assertKeys(failure, ["code", "message", "retryable", "repository"]); text(failure.code); text(failure.message); bool(failure.retryable); nullableText(failure.repository); });
  }
  if (method === "reviews.get") {
    assertKeys(value, ["handle", "repository", "detail", "capabilities", "revision", "revision_error"]);
    text(value.handle); text(value.repository); assertReviewDetail(value.detail);
    assertExactRecord(value.capabilities, ["batched_review", "thread_resolution", "draft_notes", "unapprove", "job_cancel"], bool);
    if (value.revision !== null) assertRevision(value.revision);
    if (value.revision_error !== null) assertServiceError(value.revision_error);
    return;
  }
  if (method === "diff.open" || method === "diff.page") return assertSnapshot(value, assertDiffRow, assertRevision);
  if (method === "logs.open" || method === "logs.page") return assertSnapshot(value, (row) => { assertKeys(row, ["text"]); text(row.text, 512 * 1024); }, (revision) => { assertKeys(revision, ["sha256", "byte_count"]); digest(revision.sha256); integer(revision.byte_count, 0); });
  if (method === "discussions.list") return assertArrayField(value, "discussions", assertDiscussion);
  if (method === "commits.list") return assertArrayField(value, "commits", assertCommit);
  if (method === "pipelines.list" || method === "review_pipelines.list") return assertArrayField(value, "pipelines", (item) => { assertKeys(item, ["handle", "value"]); text(item.handle); assertPipeline(item.value); });
  if (method === "jobs.list") return assertArrayField(value, "jobs", (item) => { assertKeys(item, ["handle", "value"]); text(item.handle); assertJob(item.value); });
  if (method === "plugins.list") return assertArrayField(value, "plugins", assertPlugin);
  if (method === "plugins.invoke") { assertKeys(value, ["value"]); return; }
  if (method === "host.set_location") { assertKeys(value, ["accepted"]); if (value.accepted !== true) throw new Error("Invalid desktop result"); return; }
  throw new Error("Unsupported desktop result");
}

export function assertHttpsExternalUrl(raw: unknown): string {
  if (typeof raw !== "string" || raw.length > 4096) throw new Error("Invalid external URL");
  const value = new URL(raw);
  if (value.protocol !== "https:" || value.username || value.password) throw new Error("Only credential-free HTTPS links are allowed");
  return value.href;
}

export function isAllowedAppUrl(raw: string): boolean {
  try { const value = new URL(raw); return value.protocol === "tongs:" && value.host === "app"; }
  catch { return false; }
}

function assertJson(value: unknown, depth = 0): asserts value is JsonValue {
  if (depth > 24) throw new Error("Desktop JSON is too deep");
  if (value === null || typeof value === "string" || typeof value === "boolean") return;
  if (typeof value === "number" && Number.isFinite(value)) return;
  if (Array.isArray(value)) { for (const item of value) assertJson(item, depth + 1); return; }
  if (!isRecord(value)) throw new Error("Invalid desktop JSON");
  for (const [key, item] of Object.entries(value)) {
    if (["__proto__", "prototype", "constructor"].includes(key)) throw new Error("Invalid desktop JSON key");
    assertJson(item, depth + 1);
  }
}

type RecordValue = Record<string, unknown>;
function assertKeys(value: unknown, keys: readonly string[]): asserts value is RecordValue {
  if (!isRecord(value) || Object.keys(value).length !== keys.length || keys.some((key) => !Object.hasOwn(value, key))) throw new Error("Invalid desktop result shape");
}
function assertArray(value: unknown, validate: (item: unknown) => void, maximum = 10_000): asserts value is unknown[] {
  if (!Array.isArray(value) || value.length > maximum) throw new Error("Invalid desktop result array");
  for (const item of value) validate(item);
}
function assertArrayField(value: unknown, key: string, validate: (item: unknown) => void): void { assertKeys(value, [key]); assertArray(value[key], validate); }
function assertExactRecord(value: unknown, keys: readonly string[], validate: (item: unknown) => void): void { assertKeys(value, keys); for (const key of keys) validate(value[key]); }
function text(value: unknown, maximum = MAX_TEXT): asserts value is string { if (typeof value !== "string" || value.length === 0 || value.length > maximum) throw new Error("Invalid desktop result text"); }
function nullableText(value: unknown): void { if (value !== null) text(value); }
function bool(value: unknown): asserts value is boolean { if (typeof value !== "boolean") throw new Error("Invalid desktop result boolean"); }
function integer(value: unknown, minimum = 0): asserts value is number { if (!Number.isSafeInteger(value) || Number(value) < minimum) throw new Error("Invalid desktop result integer"); }
function numberOrNull(value: unknown): void { if (value !== null && (typeof value !== "number" || !Number.isFinite(value))) throw new Error("Invalid desktop result number"); }
function digest(value: unknown): void { if (typeof value !== "string" || !/^[a-f0-9]{64}$/.test(value)) throw new Error("Invalid desktop result digest"); }
function assertUser(value: unknown): void { assertKeys(value, ["username", "display_name"]); text(value.username); text(value.display_name); }
function assertRepository(value: unknown): void { assertKeys(value, ["handle", "display_name", "forge_type"]); text(value.handle); text(value.display_name); if (value.forge_type !== "github" && value.forge_type !== "gitlab") throw new Error("Invalid forge type"); }
function assertRevision(value: unknown): void { assertKeys(value, ["head_sha", "base_sha", "start_sha"]); text(value.head_sha); text(value.base_sha); nullableText(value.start_sha); }
function assertServiceError(value: unknown): void { assertKeys(value, ["code", "message", "retryable"]); text(value.code); text(value.message); bool(value.retryable); }
const SUMMARY_KEYS = ["number", "title", "author", "state", "is_draft", "source_branch", "target_branch", "ci_status", "created_at", "updated_at", "web_url", "comment_count", "has_conflicts", "labels", "review_decision", "additions", "deletions"] as const;
function assertReviewSummary(value: unknown): void {
  assertKeys(value, SUMMARY_KEYS); integer(value.number); text(value.title); assertUser(value.author); text(value.state); bool(value.is_draft);
  for (const key of ["source_branch", "target_branch", "ci_status", "created_at", "updated_at", "web_url"] as const) text(value[key]);
  integer(value.comment_count); bool(value.has_conflicts); assertArray(value.labels, text, 256); nullableText(value.review_decision); numberOrNull(value.additions); numberOrNull(value.deletions);
}
function assertReviewDetail(value: unknown): void {
  const extra = ["description", "merge_status", "approvals", "reviewers", "assignees", "changes_count", "detailed_merge_status", "draft_notes_count", "status_check_rollup"] as const;
  assertKeys(value, [...SUMMARY_KEYS, ...extra]);
  const summary = Object.fromEntries(SUMMARY_KEYS.map((key) => [key, value[key]])); assertReviewSummary(summary);
  text(value.description, MAX_IPC_BYTES); text(value.merge_status); for (const key of ["approvals", "reviewers", "assignees"] as const) assertArray(value[key], assertUser, 1000);
  integer(value.changes_count); nullableText(value.detailed_merge_status); numberOrNull(value.draft_notes_count); nullableText(value.status_check_rollup);
}
function assertSnapshot(value: unknown, entry: (item: unknown) => void, revision: (item: unknown) => void): void {
  assertKeys(value, ["snapshot_id", "resource", "revision", "cursor", "next_cursor", "entries"]); text(value.snapshot_id); text(value.resource); revision(value.revision); integer(value.cursor); if (value.next_cursor !== null) integer(value.next_cursor); assertArray(value.entries, entry);
}
function assertDiffRow(value: unknown): void {
  if (!isRecord(value) || typeof value.kind !== "string") throw new Error("Invalid diff row");
  const common = ["kind", "file_index"];
  if (value.kind === "file") { assertKeys(value, [...common, "old_path", "new_path", "status", "additions", "deletions", "is_binary", "language", "is_truncated", "is_empty", "is_mode_only", "is_unavailable"]); integer(value.file_index); for (const key of ["old_path", "new_path", "status"] as const) text(value[key]); integer(value.additions); integer(value.deletions); for (const key of ["is_binary", "is_truncated", "is_empty", "is_mode_only", "is_unavailable"] as const) bool(value[key]); nullableText(value.language); return; }
  if (value.kind === "hunk") { assertKeys(value, [...common, "hunk_index", "header", "old_start", "old_count", "new_start", "new_count", "context_text"]); for (const key of ["file_index", "hunk_index", "old_start", "old_count", "new_start", "new_count"] as const) integer(value[key]); text(value.header); if (typeof value.context_text !== "string") throw new Error("Invalid hunk context"); return; }
  if (value.kind === "line") { assertKeys(value, [...common, "hunk_index", "old_line", "new_line", "content", "line_type"]); integer(value.file_index); integer(value.hunk_index); numberOrNull(value.old_line); numberOrNull(value.new_line); if (typeof value.content !== "string") throw new Error("Invalid diff content"); text(value.line_type); return; }
  throw new Error("Invalid diff row kind");
}
function assertDiscussion(value: unknown): void { assertKeys(value, ["id", "is_inline", "root_comment", "is_resolved", "resolvable"]); text(value.id); bool(value.is_inline); assertComment(value.root_comment); bool(value.is_resolved); bool(value.resolvable); }
function assertComment(value: unknown, depth = 0): void { if (depth > 16) throw new Error("Discussion replies are too deep"); assertKeys(value, ["id", "author", "body", "created_at", "file_path", "old_line", "new_line", "is_resolved", "replies"]); text(value.id); assertUser(value.author); text(value.body, MAX_IPC_BYTES); text(value.created_at); text(value.file_path); numberOrNull(value.old_line); numberOrNull(value.new_line); bool(value.is_resolved); assertArray(value.replies, (reply) => assertComment(reply, depth + 1), 1000); }
function assertCommit(value: unknown): void { assertKeys(value, ["sha", "short_sha", "title", "message", "author", "created_at", "web_url"]); for (const key of ["sha", "short_sha", "title", "message", "web_url"] as const) text(value[key], MAX_IPC_BYTES); assertUser(value.author); nullableText(value.created_at); }
function assertPipeline(value: unknown): void { assertKeys(value, ["id", "status", "ref", "sha", "web_url", "source", "created_at", "finished_at", "duration_seconds"]); integer(value.id); for (const key of ["status", "ref", "sha", "web_url", "source"] as const) text(value[key]); nullableText(value.created_at); nullableText(value.finished_at); numberOrNull(value.duration_seconds); }
function assertJob(value: unknown): void { assertKeys(value, ["id", "name", "stage", "status", "web_url", "started_at", "finished_at", "duration_seconds", "allow_failure"]); integer(value.id); for (const key of ["name", "stage", "status", "web_url"] as const) text(value[key]); nullableText(value.started_at); nullableText(value.finished_at); numberOrNull(value.duration_seconds); bool(value.allow_failure); }
function assertPlugin(value: unknown): void { assertKeys(value, ["plugin_id", "state", "has_terminal_entry_point", "has_desktop_entry_point", "error", "manifest"]); text(value.plugin_id); text(value.state); bool(value.has_terminal_entry_point); bool(value.has_desktop_entry_point); if (value.error !== null) assertServiceError(value.error); if (value.manifest !== null) { if (!isRecord(value.manifest)) throw new Error("Invalid plugin manifest"); assertJson(value.manifest); } }

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
