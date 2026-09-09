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

const UNREPORTED_READ = "The local service could not complete this read.";

export function safeError(error: unknown): string {
  const failure = serviceErrorOf(error);
  if (failure === null) return UNREPORTED_READ;
  const cause = typeof failure.message === "string" ? failure.message.trim() : "";
  if (failure.code === "snapshot_expired")
    return "This diff snapshot expired. Reload the diff to continue.";
  if (failure.code === "revision_changed")
    return "The review changed while its diff was loading. Reload the latest revision.";
  if (failure.code === "pagination_limit")
    return "The diff exceeded the safe desktop paging limit. A bounded partial snapshot is shown.";
  // Only the diff surface raises this code, so only it advises a diff reload.  A
  // failure the main process could not type arrives as invalid_response for any
  // operation, and telling the threads or repositories screen to reload a diff
  // would be worse than saying nothing about it.
  if (failure.code === "invalid_diff_page")
    return cause === ""
      ? "The local service returned an inconsistent diff page. Reload the diff to try again."
      : `The local service returned an inconsistent diff page (${cause}). Reload the diff to try again.`;
  const reported =
    cause === ""
      ? UNREPORTED_READ
      : `The local service could not complete this read: ${cause}`;
  return failure.retryable ? `${reported} Try again.` : reported;
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
