import { decodeReadFailure, type ServiceErrorDto } from "../../shared/bridge.js";

export class RendererReadError extends Error implements ServiceErrorDto {
  constructor(
    readonly code: string,
    message: string,
    readonly retryable: boolean,
  ) {
    super(message);
    this.name = "RendererReadError";
  }
}

/** Recover the typed failure a read carries, whether raised here or in main. */
export function serviceErrorOf(error: unknown): ServiceErrorDto | null {
  return isServiceError(error) ? error : decodeReadFailure(error);
}

export function safeError(error: unknown): string {
  const failure = serviceErrorOf(error);
  if (failure !== null) {
    if (failure.code === "snapshot_expired")
      return "This diff snapshot expired. Reload the diff to continue.";
    if (failure.code === "revision_changed")
      return "The review changed while its diff was loading. Reload the latest revision.";
    if (failure.code === "pagination_limit")
      return "The diff exceeded the safe desktop paging limit. A bounded partial snapshot is shown.";
    if (failure.code === "invalid_response")
      return `The local service returned an inconsistent diff page (${failure.message}). Reload the diff to try again.`;
    if (failure.retryable)
      return `The local service could not complete this read: ${failure.message} Try again.`;
    return `The local service could not complete this read: ${failure.message}`;
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
