import { EventEmitter } from "node:events";
import { spawn, type ChildProcessWithoutNullStreams, type SpawnOptions } from "node:child_process";
import { TextDecoder } from "node:util";

import {
  PROTOCOL_MAJOR,
  REQUIRED_CAPABILITIES,
  type DesktopEvent,
  type JsonObject,
  type JsonValue,
} from "../shared/bridge.js";
import { REVIEW_OPERATIONS } from "./review.js";
import { UTILITY_PROTOCOL_METHODS } from "../shared/utilities.js";
import {
  SIDECAR_ARGUMENTS,
  type DesktopLaunchConfig,
  validateLaunchConfig,
} from "./launch.js";

export const MAX_REQUEST_BYTES = 256 * 1024;
export const MAX_RESPONSE_BYTES = 8 * 1024 * 1024;
export const MAX_EVENT_BYTES = 64 * 1024;
export const MAX_PENDING = 64;
export const MAX_QUEUED_EVENTS = 256;
export const MAX_ASSET_CHUNK_BYTES = 512 * 1024;
const MAX_CRASH_HISTORY = 16;
const MAX_JSON_DEPTH = 24;
const MAX_JSON_ITEMS = 20_000;

export class SidecarError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly retryable = false,
  ) {
    super(message);
    this.name = "SidecarError";
  }
}

export interface SidecarCrash {
  readonly code: number | null;
  readonly signal: NodeJS.Signals | null;
  readonly unexpected: boolean;
}

export interface SidecarRequest<T extends JsonValue = JsonValue> {
  readonly requestId: string;
  readonly result: Promise<T>;
}

interface PendingRequest {
  readonly method: string;
  readonly read: boolean;
  readonly resolve: (value: JsonValue) => void;
  readonly reject: (error: Error) => void;
  readonly timer: NodeJS.Timeout;
}

interface HandshakeResult extends JsonObject {
  protocol_major: number;
  core_version: string;
  session_id: string;
  capabilities: JsonValue;
  accepted_capabilities: JsonValue;
  methods: JsonValue;
  limits: JsonValue;
}

type SpawnChild = (
  executable: string,
  args: readonly string[],
  options: SpawnOptions,
) => ChildProcessWithoutNullStreams;

const spawnSidecar: SpawnChild = (executable, args, options) =>
  spawn(executable, [...args], options) as ChildProcessWithoutNullStreams;

export class SidecarTransport extends EventEmitter {
  readonly launch: DesktopLaunchConfig;
  readonly crashHistory: SidecarCrash[] = [];
  private child: ChildProcessWithoutNullStreams | null = null;
  private pending = new Map<string, PendingRequest>();
  private buffer = Buffer.alloc(0);
  private nextId = 1;
  private generation = 0;
  private lastEventSequence = 0;
  private starting: Promise<void> | null = null;
  private stopping: Promise<void> | null = null;
  private expectedExit = false;
  private failureReported = false;
  private closeRequested = false;

  constructor(
    launch: DesktopLaunchConfig,
    private readonly requestTimeoutMs = 30_000,
    private readonly startupTimeoutMs = 10_000,
    private readonly shutdownTimeoutMs = 5_000,
    private readonly spawnChild: SpawnChild = spawnSidecar,
  ) {
    super();
    this.launch = validateLaunchConfig(launch);
    if ([requestTimeoutMs, startupTimeoutMs, shutdownTimeoutMs].some((v) => v <= 0)) {
      throw new Error("Sidecar timeouts must be positive");
    }
  }

  get sessionGeneration(): number {
    return this.generation;
  }

  get processId(): number | undefined {
    return this.child?.pid;
  }

  async start(): Promise<void> {
    if (this.closeRequested) {
      throw new SidecarError("shutting_down", "The desktop service is stopping.");
    }
    if (this.stopping) {
      await this.stopping;
      return this.start();
    }
    if (this.starting) return this.starting;
    if (this.child && !this.child.killed && !this.failureReported) return;
    this.starting = this.startOnce();
    try {
      await this.starting;
    } finally {
      this.starting = null;
    }
  }

  requestRead<T extends JsonValue = JsonValue>(
    method: string,
    params: JsonObject,
  ): SidecarRequest<T> {
    return this.request<T>(method, params, true, this.requestTimeoutMs);
  }

  requestMutation<T extends JsonValue = JsonValue>(
    method: string,
    params: JsonObject,
  ): SidecarRequest<T> {
    return this.request<T>(method, params, false, this.requestTimeoutMs);
  }

  cancelRead(requestId: string): boolean {
    const pending = this.pending.get(requestId);
    if (!pending?.read || !this.child?.stdin.writable) return false;
    this.writeFrame({ v: PROTOCOL_MAJOR, type: "cancel", id: requestId });
    return true;
  }

  async restart(): Promise<void> {
    if (this.closeRequested) {
      throw new SidecarError("shutting_down", "The desktop service is stopping.");
    }
    await this.stopSession();
    if (this.closeRequested) {
      throw new SidecarError("shutting_down", "The desktop service is stopping.");
    }
    await this.start();
  }

  async stop(): Promise<void> {
    this.closeRequested = true;
    await this.stopSession();
  }

  private async stopSession(): Promise<void> {
    if (this.stopping) return this.stopping;
    this.stopping = this.stopOnce();
    try {
      await this.stopping;
    } finally {
      this.stopping = null;
    }
  }

  private async startOnce(): Promise<void> {
    if (this.child) await this.reapBeforeReplacement(this.child);
    this.expectedExit = false;
    this.failureReported = false;
    this.buffer = Buffer.alloc(0);
    this.lastEventSequence = 0;
    this.generation += 1;
    const environment = { ...process.env };
    delete environment.PYTHONHOME;
    delete environment.PYTHONPATH;
    let child: ChildProcessWithoutNullStreams;
    try {
      child = this.spawnChild(this.launch.pythonExecutable, SIDECAR_ARGUMENTS, {
        cwd: this.launch.safeCwd,
        env: environment,
        stdio: ["pipe", "pipe", "pipe"],
        windowsHide: true,
      });
    } catch {
      throw new SidecarError("start_failed", "The desktop service could not start.");
    }
    this.child = child;
    const generation = this.generation;
    child.stdout.on("data", (chunk: Buffer) => this.consume(child, generation, chunk));
    child.stderr.on("data", () => undefined);
    child.once("error", () => this.failConnection(child, generation, "start_failed"));
    child.once("exit", (code, signal) => this.onExit(child, generation, code, signal));

    const handshake = this.request<HandshakeResult>(
      "handshake",
      {
        protocol_major: PROTOCOL_MAJOR,
        core_version: this.launch.coreVersion,
        capabilities: [...REQUIRED_CAPABILITIES],
        client: "tongs-electron-44.2.0",
      },
      false,
      this.startupTimeoutMs,
    );
    try {
      validateHandshake(await handshake.result, this.launch.coreVersion);
    } catch (error) {
      await this.stop();
      throw error;
    }
  }

  private request<T extends JsonValue>(
    method: string,
    params: JsonObject,
    read: boolean,
    timeoutMs: number,
  ): SidecarRequest<T> {
    if (!this.child?.stdin.writable) {
      throw new SidecarError("not_running", "The desktop service is not running.");
    }
    if (this.pending.size >= MAX_PENDING) {
      throw new SidecarError("too_many_requests", "Too many desktop requests are pending.");
    }
    validateJson(params);
    const requestId = `electron-${this.generation}-${this.nextId}`;
    this.nextId = this.nextId === Number.MAX_SAFE_INTEGER ? 1 : this.nextId + 1;
    if (this.pending.has(requestId)) {
      throw new SidecarError("request_id_exhausted", "Desktop request IDs are exhausted.");
    }
    let resolvePromise!: (value: T) => void;
    let rejectPromise!: (error: Error) => void;
    const result = new Promise<T>((resolve, reject) => {
      resolvePromise = resolve;
      rejectPromise = reject;
    });
    const timer = setTimeout(() => {
      const pending = this.pending.get(requestId);
      if (!pending) return;
      if (pending.read) this.cancelRead(requestId);
      this.pending.delete(requestId);
      pending.reject(
        pending.read
          ? new SidecarError("timeout", "The desktop read timed out.", true)
          : new SidecarError(
              "mutation_timeout",
              "The CI mutation response timed out. Its outcome is unknown.",
              true,
            ),
      );
    }, timeoutMs);
    this.pending.set(requestId, {
      method,
      read,
      resolve: resolvePromise as (value: JsonValue) => void,
      reject: rejectPromise,
      timer,
    });
    try {
      this.writeFrame({ v: PROTOCOL_MAJOR, type: "request", id: requestId, method, params });
    } catch (error) {
      clearTimeout(timer);
      this.pending.delete(requestId);
      throw error;
    }
    return { requestId, result };
  }

  private writeFrame(frame: JsonObject): void {
    const bytes = Buffer.from(`${JSON.stringify(frame)}\n`, "utf8");
    if (bytes.length > MAX_REQUEST_BYTES) {
      throw new SidecarError("request_too_large", "The desktop request is too large.");
    }
    const child = this.child;
    const generation = this.generation;
    if (!child?.stdin.writable) throw new SidecarError("not_running", "The desktop service is not running.");
    child.stdin.write(bytes, (error) => {
      if (error) this.failConnection(child, generation, "write_failed");
    });
  }

  private consume(child: ChildProcessWithoutNullStreams, generation: number, chunk: Buffer): void {
    if (this.child !== child || this.generation !== generation) return;
    this.buffer = Buffer.concat([this.buffer, chunk]);
    while (true) {
      const newline = this.buffer.indexOf(0x0a);
      if (newline < 0) {
        if (this.buffer.length > MAX_RESPONSE_BYTES) this.failConnection(child, generation, "frame_too_large");
        return;
      }
      const raw = this.buffer.subarray(0, newline);
      this.buffer = this.buffer.subarray(newline + 1);
      if (raw.length === 0 || raw.length > MAX_RESPONSE_BYTES) {
        this.failConnection(child, generation, "invalid_frame");
        return;
      }
      this.consumeFrame(child, generation, raw);
      if (!this.child) return;
    }
  }

  private consumeFrame(child: ChildProcessWithoutNullStreams, generation: number, raw: Buffer): void {
    let value: unknown;
    try {
      value = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(raw));
      validateJson(value);
    } catch {
      this.failConnection(child, generation, "invalid_frame");
      return;
    }
    if (!isRecord(value) || value.v !== PROTOCOL_MAJOR || typeof value.type !== "string") {
      this.failConnection(child, generation, "invalid_frame");
      return;
    }
    if (value.type === "event") {
      if (raw.length > MAX_EVENT_BYTES || !validEvent(value)) {
        this.failConnection(child, generation, "invalid_event");
        return;
      }
      const event: DesktopEvent = {
        sequence: value.sequence,
        name: value.event,
        data: value.data as JsonValue,
      };
      if (this.lastEventSequence && event.sequence !== this.lastEventSequence + 1) {
        this.emit("event", {
          sequence: event.sequence,
          name: "protocol.resync_required",
          data: { reason: "sequence_gap" },
        } satisfies DesktopEvent);
      }
      this.lastEventSequence = event.sequence;
      this.emit("event", event);
      return;
    }
    if (value.type !== "response" || !validResponse(value)) {
      this.failConnection(child, generation, "invalid_response");
      return;
    }
    const pending = this.pending.get(value.id);
    if (!pending) return;
    clearTimeout(pending.timer);
    this.pending.delete(value.id);
    if (Object.hasOwn(value, "result")) {
      pending.resolve(value.result as JsonValue);
      return;
    }
    const error = value.error as Record<string, unknown>;
    const code = mutationErrorCode(error, pending.read);
    pending.reject(
      new SidecarError(
        code,
        typeof error.message === "string" ? error.message : "The desktop request failed.",
        error.retryable === true,
      ),
    );
  }

  private failConnection(child: ChildProcessWithoutNullStreams, generation: number, code: string): void {
    if (this.child !== child || this.generation !== generation || this.failureReported) return;
    this.failureReported = true;
    const error = new SidecarError(code, "The desktop service connection failed.", true);
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(error);
    }
    this.pending.clear();
    if (!child.killed) child.kill("SIGTERM");
    this.emit("crash", error);
  }

  private onExit(child: ChildProcessWithoutNullStreams, generation: number, code: number | null, signal: NodeJS.Signals | null): void {
    if (this.child !== child || this.generation !== generation) return;
    const unexpected = !this.expectedExit;
    this.crashHistory.push({ code, signal, unexpected });
    if (this.crashHistory.length > MAX_CRASH_HISTORY) this.crashHistory.shift();
    if (unexpected && !this.failureReported) this.failConnection(child, generation, "unexpected_eof");
    this.child = null;
  }

  private async reapBeforeReplacement(child: ChildProcessWithoutNullStreams): Promise<void> {
    if (await waitForExit(child, this.shutdownTimeoutMs)) return;
    if (!child.killed) child.kill("SIGTERM");
    if (await waitForExit(child, 1_000)) return;
    child.kill("SIGKILL");
    if (!(await waitForExit(child, 1_000))) {
      throw new SidecarError("reap_failed", "The prior desktop service did not stop.");
    }
  }

  private async stopOnce(): Promise<void> {
    const child = this.child;
    this.expectedExit = true;
    const stopped = new SidecarError("shutting_down", "The desktop service is stopping.");
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(stopped);
    }
    this.pending.clear();
    if (!child) return;
    try {
      const shutdown = this.request<JsonObject>("shutdown", {}, false, this.shutdownTimeoutMs);
      await shutdown.result;
    } catch {
      // The bounded process shutdown below remains authoritative.
    }
    child.stdin.end();
    if (await waitForExit(child, this.shutdownTimeoutMs)) return;
    child.kill("SIGTERM");
    if (await waitForExit(child, 1_000)) return;
    child.kill("SIGKILL");
    await waitForExit(child, 1_000);
  }
}

const SERVICE_ERROR_CODES = new Set([
  "authentication_failed",
  "closed",
  "configuration_invalid",
  "conflict",
  "internal",
  "invalid_input",
  "invalid_response",
  "network_unavailable",
  "not_found",
  "not_started",
  "permission_denied",
  "rate_limited",
  "resource_not_issued",
  "revision_changed",
  "revision_unavailable",
  "shutdown_failed",
  "unsupported",
]);

function mutationErrorCode(
  error: Record<string, unknown>,
  read: boolean,
): string {
  const protocolCode =
    typeof error.code === "string" ? error.code : "service_error";
  if (read || protocolCode !== "service_error" || !isRecord(error.details))
    return protocolCode;
  const serviceCode = error.details.service_code;
  return typeof serviceCode === "string" && SERVICE_ERROR_CODES.has(serviceCode)
    ? serviceCode
    : protocolCode;
}

function validateHandshake(value: JsonValue, coreVersion: string): asserts value is HandshakeResult {
  if (!isRecord(value)) throw new SidecarError("invalid_handshake", "Invalid desktop handshake.");
  const required = [...REQUIRED_CAPABILITIES].sort();
  const capabilities = stringArray(value.capabilities);
  const accepted = stringArray(value.accepted_capabilities);
  const methods = stringArray(value.methods);
  if (
    value.protocol_major !== PROTOCOL_MAJOR ||
    value.core_version !== coreVersion ||
    typeof value.session_id !== "string" ||
    value.session_id.length < 16 ||
      !required.every((capability) => capabilities.includes(capability)) ||
    JSON.stringify(accepted) !== JSON.stringify(required) ||
    !REQUIRED_METHODS.every((method) => methods.includes(method)) ||
    !isRecord(value.limits) ||
    value.limits.request_frame_bytes !== MAX_REQUEST_BYTES ||
    value.limits.response_frame_bytes !== MAX_RESPONSE_BYTES ||
    value.limits.event_frame_bytes !== MAX_EVENT_BYTES ||
    value.limits.pending_requests !== MAX_PENDING ||
    value.limits.queued_events !== MAX_QUEUED_EVENTS ||
    value.limits.asset_chunk_bytes !== MAX_ASSET_CHUNK_BYTES
  ) {
    throw new SidecarError("incompatible_handshake", "The desktop service is incompatible.");
  }
}

const REQUIRED_METHODS = Object.freeze([
  "assets.list", "assets.read", "ci.capabilities", "ci.receipt", "commits.list", "diff.open", "diff.page",
  "discussions.list", "host.set_location", "jobs.list", "logs.open", "logs.page",
  "jobs.cancel", "jobs.retry", "pipelines.cancel", "pipelines.list", "pipelines.retry",
  "plugins.invoke", "plugins.list", "repositories.discover",
  "repositories.open", "review_pipelines.list", "reviews.get", "reviews.list", "shutdown",
  ...UTILITY_PROTOCOL_METHODS,
  ...REVIEW_OPERATIONS.map((operation) => operation.method),
].sort());

function validResponse(value: Record<string, unknown>): value is Record<string, JsonValue> & { id: string } {
  return (
    typeof value.id === "string" &&
    (Object.hasOwn(value, "result") !== Object.hasOwn(value, "error")) &&
    (!Object.hasOwn(value, "error") || isRecord(value.error))
  );
}

function validEvent(value: Record<string, unknown>): value is Record<string, unknown> & {
  sequence: number; event: string; data: JsonValue;
} {
  return Number.isSafeInteger(value.sequence) && Number(value.sequence) > 0 &&
    typeof value.event === "string" && value.event.length <= 120 && Object.hasOwn(value, "data");
}

function stringArray(value: unknown): string[] {
  if (!Array.isArray(value) || !value.every((item) => typeof item === "string")) return [];
  return [...value].sort();
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function validateJson(value: unknown, depth = 0, budget = { items: 0 }): asserts value is JsonValue {
  if (depth > MAX_JSON_DEPTH || budget.items++ > MAX_JSON_ITEMS) throw new Error("invalid JSON");
  if (value === null || typeof value === "string" || typeof value === "boolean") return;
  if (typeof value === "number" && Number.isFinite(value)) return;
  if (Array.isArray(value)) {
    for (const item of value) validateJson(item, depth + 1, budget);
    return;
  }
  if (!isRecord(value)) throw new Error("invalid JSON");
  for (const [key, item] of Object.entries(value)) {
    if (key === "__proto__" || key === "constructor" || key === "prototype") throw new Error("invalid JSON key");
    validateJson(item, depth + 1, budget);
  }
}

async function waitForExit(child: ChildProcessWithoutNullStreams, timeoutMs: number): Promise<boolean> {
  if (child.exitCode !== null || child.signalCode !== null) return true;
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve(false), timeoutMs);
    child.once("exit", () => {
      clearTimeout(timer);
      resolve(true);
    });
  });
}
