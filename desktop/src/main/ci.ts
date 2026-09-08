import type { JsonObject, JsonValue } from "../shared/bridge.js";
import {
  CI_IPC_CHANNELS,
  type CIMutationAction,
} from "../shared/ci.js";

const MAX_OPERATION_ID = 128;
const MAX_TEXT = 2048;
const OPERATION_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

export interface CIOperation {
  readonly channel: string;
  readonly method: string;
  readonly mutation: boolean;
}

export const CI_OPERATIONS: readonly CIOperation[] = Object.freeze([
  {
    channel: CI_IPC_CHANNELS.capabilities,
    method: "ci.capabilities",
    mutation: false,
  },
  {
    channel: CI_IPC_CHANNELS.receipt,
    method: "ci.receipt",
    mutation: false,
  },
  {
    channel: CI_IPC_CHANNELS.retryPipeline,
    method: "pipelines.retry",
    mutation: true,
  },
  {
    channel: CI_IPC_CHANNELS.cancelPipeline,
    method: "pipelines.cancel",
    mutation: true,
  },
  {
    channel: CI_IPC_CHANNELS.retryJob,
    method: "jobs.retry",
    mutation: true,
  },
  {
    channel: CI_IPC_CHANNELS.cancelJob,
    method: "jobs.cancel",
    mutation: true,
  },
]);

export function assertCIParams(
  method: string,
  value: unknown,
): asserts value is JsonObject {
  if (!isRecord(value)) throw new Error("CI parameters must be an object");
  if (method === "ci.capabilities") {
    assertKeys(value, ["repository"]);
    handle(value.repository);
    return;
  }
  if (method === "pipelines.retry" || method === "pipelines.cancel") {
    assertKeys(value, ["operation_id", "pipeline"]);
    operationId(value.operation_id);
    handle(value.pipeline);
    return;
  }
  if (method === "jobs.retry" || method === "jobs.cancel") {
    assertKeys(value, ["operation_id", "pipeline", "job"]);
    operationId(value.operation_id);
    handle(value.pipeline);
    handle(value.job);
    return;
  }
  if (method === "ci.receipt") {
    const action = mutationAction(value.action);
    const jobAction = action === "retry_job" || action === "cancel_job";
    assertKeys(
      value,
      jobAction
        ? ["operation_id", "action", "pipeline", "job"]
        : ["operation_id", "action", "pipeline"],
    );
    operationId(value.operation_id);
    handle(value.pipeline);
    if (jobAction) handle(value.job);
    return;
  }
  throw new Error("Unsupported desktop CI operation");
}

export function assertCIResult(
  method: string,
  value: unknown,
): asserts value is JsonValue {
  if (method === "ci.capabilities") {
    assertKeys(value, ["repository", "capabilities"]);
    handle(value.repository);
    assertKeys(value.capabilities, [
      "retry_pipeline",
      "cancel_pipeline",
      "retry_job",
      "cancel_job",
    ]);
    for (const item of Object.values(value.capabilities)) bool(item);
    return;
  }
  if (method === "ci.receipt") {
    assertKeys(value, ["receipt"]);
    if (value.receipt !== null) receipt(value.receipt);
    return;
  }
  if (
    method === "pipelines.retry" ||
    method === "pipelines.cancel" ||
    method === "jobs.retry" ||
    method === "jobs.cancel"
  ) {
    receipt(value);
    if (value.action !== expectedAction(method))
      throw new Error("CI receipt action does not match the operation");
    return;
  }
  throw new Error("Unsupported desktop CI result");
}

function expectedAction(method: string): CIMutationAction {
  if (method === "pipelines.retry") return "retry_pipeline";
  if (method === "pipelines.cancel") return "cancel_pipeline";
  if (method === "jobs.retry") return "retry_job";
  if (method === "jobs.cancel") return "cancel_job";
  throw new Error("Unsupported desktop CI mutation");
}

function receipt(value: unknown): asserts value is Record<string, unknown> {
  assertKeys(value, [
    "operation_id",
    "action",
    "outcome",
    "error",
    "resync_required",
  ]);
  operationId(value.operation_id);
  mutationAction(value.action);
  if (value.outcome !== "known" && value.outcome !== "unknown")
    throw new Error("Invalid CI mutation outcome");
  if (value.error !== null) {
    assertKeys(value.error, ["code", "message", "retryable"]);
    boundedText(value.error.code);
    boundedText(value.error.message);
    bool(value.error.retryable);
  }
  if (
    (value.outcome === "known" && value.error !== null) ||
    (value.outcome === "unknown" && value.error === null)
  ) {
    throw new Error("Invalid CI mutation receipt state");
  }
  bool(value.resync_required);
}

function mutationAction(value: unknown): CIMutationAction {
  if (
    value !== "retry_pipeline" &&
    value !== "cancel_pipeline" &&
    value !== "retry_job" &&
    value !== "cancel_job"
  ) {
    throw new Error("Invalid CI mutation action");
  }
  return value;
}

function operationId(value: unknown): void {
  if (
    typeof value !== "string" ||
    value.length > MAX_OPERATION_ID ||
    !OPERATION_ID.test(value)
  ) {
    throw new Error("Invalid CI operation ID");
  }
}

function handle(value: unknown): void {
  boundedText(value);
  if ([...value].some((character) => character.charCodeAt(0) < 32))
    throw new Error("Invalid CI handle");
}

function boundedText(value: unknown): asserts value is string {
  if (typeof value !== "string" || value.length === 0 || value.length > MAX_TEXT)
    throw new Error("Invalid CI text");
}

function bool(value: unknown): asserts value is boolean {
  if (typeof value !== "boolean") throw new Error("Invalid CI boolean");
}

function assertKeys(
  value: unknown,
  keys: readonly string[],
): asserts value is Record<string, unknown> {
  if (
    !isRecord(value) ||
    Object.keys(value).length !== keys.length ||
    keys.some((key) => !Object.hasOwn(value, key))
  ) {
    throw new Error("Invalid CI fields");
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
