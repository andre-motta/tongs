import type { ServiceErrorDto } from "../../shared/bridge.js";

export function safeError(error: unknown): string {
  if (isServiceError(error)) {
    if (error.code === "snapshot_expired")
      return "This diff snapshot expired. Reload the diff to continue.";
    if (error.code === "revision_changed")
      return "The review changed while its diff was loading. Reload the latest revision.";
    if (error.retryable)
      return "The local service could not complete this read. Try again.";
  }
  return "The local service could not complete this read.";
}

export function formatDate(value: string | null): string {
  if (!value) return "Unknown time";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
}

function isServiceError(value: unknown): value is ServiceErrorDto {
  return (
    value !== null &&
    typeof value === "object" &&
    "code" in value &&
    "retryable" in value
  );
}
