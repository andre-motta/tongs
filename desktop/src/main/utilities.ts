import { spawn, type ChildProcess } from "node:child_process";
import { constants } from "node:fs";
import {
  lstat,
  mkdir,
  open,
  unlink,
  type FileHandle,
} from "node:fs/promises";
import path from "node:path";
import type { JsonObject, JsonValue } from "../shared/bridge.js";
import type {
  ClearCacheResult,
  CopyReviewUrlResult,
  EditorOpenOutcome,
  OpenJobLogEditorResult,
} from "../shared/utilities.js";
import { assertHttpsExternalUrl } from "./security.js";

const MAX_EDITOR_LOG_BYTES = 4 * 1024 * 1024;
const MAX_EDITOR_ARGUMENTS = 64;
const MAX_EDITOR_ARGUMENT_BYTES = 4096;
const LAUNCH_TIMEOUT_MILLISECONDS = 5_000;
const EARLY_EXIT_MILLISECONDS = 250;
const EXPORT_NAME = /^tongs-slot-([1-8])-job-([1-9][0-9]{0,18})-([0-9a-f]{32})\.log$/;

interface UtilityTransport {
  requestRead(
    method: string,
    params: JsonObject,
  ): { readonly result: Promise<JsonValue> };
  requestMutation(
    method: string,
    params: JsonObject,
  ): { readonly result: Promise<JsonValue> };
}

interface ClipboardWriter {
  writeText(value: string): Promise<void> | void;
}

interface ReviewUrlPlan {
  readonly review: string;
  readonly url: string;
}

type EditorPlanStatus =
  | "ready"
  | "disabled"
  | "missing"
  | "malformed"
  | "terminal_unsupported"
  | "log_too_large"
  | "capacity_exceeded";

interface EditorLogPlan {
  readonly status: EditorPlanStatus;
  readonly message: string;
  readonly job: string;
  readonly job_id: number;
  readonly argv: readonly string[];
  readonly content: string | null;
  readonly slot: number | null;
  readonly token: string | null;
  readonly export_name: string | null;
}

interface EditorReservation {
  readonly slot: number;
  readonly token: string;
  readonly exportName: string;
}

interface FileIdentity {
  readonly device: number;
  readonly inode: number;
}

interface PrivateExport {
  readonly file: FileHandle;
  readonly identity: FileIdentity;
}

type SpawnEditor = (
  command: string,
  args: readonly string[],
) => ChildProcess;

export class WorkspaceUtilities {
  private openingEditor = false;
  private readonly exportRoot: string;

  constructor(
    private readonly transport: UtilityTransport,
    private readonly clipboard: ClipboardWriter,
    exportRoot: string,
    private readonly spawnEditor: SpawnEditor = defaultSpawnEditor,
  ) {
    if (!path.isAbsolute(exportRoot))
      throw new Error("The editor export root must be absolute");
    this.exportRoot = path.resolve(exportRoot);
  }

  async copyReviewUrl(review: unknown): Promise<CopyReviewUrlResult> {
    try {
      const handle = opaqueHandle(review);
      const value = await this.transport.requestRead(
        "utilities.review_url",
        { review: handle },
      ).result;
      const plan = assertReviewUrlPlan(value, handle);
      assertHttpsExternalUrl(plan.url);
      await this.clipboard.writeText(plan.url);
      return Object.freeze({
        outcome: "copied",
        message: "Review URL copied to the clipboard.",
      });
    } catch {
      return Object.freeze({
        outcome: "failed",
        message: "The review URL could not be copied. Check clipboard access and retry.",
      });
    }
  }

  async clearCache(value: unknown): Promise<ClearCacheResult> {
    try {
      emptyParams(value);
      const result = await this.transport.requestMutation(
        "utilities.cache_clear",
        {},
      ).result;
      exactKeys(result, ["cleared"]);
      if (result.cleared !== true) throw new Error("Cache was not cleared");
      return Object.freeze({
        outcome: "cleared",
        message: "Shared API cache cleared. Draft reviews were preserved.",
      });
    } catch {
      return Object.freeze({
        outcome: "failed",
        message: "The shared API cache could not be cleared. Retry after reconnecting the local service.",
      });
    }
  }

  async openJobLogInEditor(job: unknown): Promise<OpenJobLogEditorResult> {
    if (this.openingEditor) {
      return Object.freeze({
        outcome: "busy",
        message: "An editor launch is already in progress.",
      });
    }
    this.openingEditor = true;
    let exportPath: string | null = null;
    let reservation: EditorReservation | null = null;
    let identity: FileIdentity | null = null;
    try {
      const handle = opaqueHandle(job);
      await this.prepareExportRoot();
      const value = await this.transport.requestRead(
        "utilities.job_log_export",
        { job: handle },
      ).result;
      const plan = assertEditorLogPlan(value, handle);
      if (plan.status !== "ready") {
        return utilityResult(plan.status, plan.message);
      }
      reservation = {
        slot: plan.slot!,
        token: plan.token!,
        exportName: plan.export_name!,
      };
      const content = plan.content;
      if (content === null) throw new Error("Missing editor log content");
      const byteCount = Buffer.byteLength(content, "utf8");
      if (byteCount > MAX_EDITOR_LOG_BYTES)
        throw new Error("Editor log exceeds the accepted bound");
      exportPath = path.join(this.exportRoot, reservation.exportName);
      const created = await createPrivateExport(exportPath);
      identity = created.identity;
      try {
        await created.file.writeFile(content, { encoding: "utf8" });
      } finally {
        await created.file.close();
      }
      const started = await startEditor(
        this.spawnEditor,
        plan.argv,
        exportPath,
      );
      if (!started.ok) {
        if (started.retain) {
          const retainedPath = exportPath;
          const retainedReservation = reservation;
          const retainedIdentity = identity;
          exportPath = null;
          reservation = null;
          identity = null;
          if (started.exited) {
            await this.cleanupExport(
              retainedPath,
              retainedIdentity,
              retainedReservation,
            );
          } else {
            attachCleanup(started.child, () =>
              this.scheduleCleanup(
                retainedPath,
                retainedIdentity,
                retainedReservation,
              ),
            );
            started.child.unref();
          }
        } else {
          await this.cleanupExport(exportPath, identity, reservation);
          exportPath = null;
          reservation = null;
          identity = null;
        }
        return utilityResult("failed", started.message);
      }
      const retainedPath = exportPath;
      const retainedReservation = reservation;
      const retainedIdentity = identity;
      exportPath = null;
      reservation = null;
      identity = null;
      if (started.exited) {
        await this.cleanupExport(
          retainedPath,
          retainedIdentity,
          retainedReservation,
        );
        return utilityResult(
          "started",
          "Editor process started and exited. Tongs cannot confirm that the exported log was opened.",
        );
      }
      attachCleanup(started.child, () =>
        this.scheduleCleanup(
          retainedPath,
          retainedIdentity,
          retainedReservation,
        ),
      );
      started.child.unref();
      return utilityResult(
        "started",
        "Editor started. Tongs cannot confirm that the exported log was opened.",
      );
    } catch {
      if (reservation !== null) {
        await this.cleanupExport(exportPath, identity, reservation);
      }
      return utilityResult(
        "failed",
        "The editor could not be started. Check the configured command and retry.",
      );
    } finally {
      this.openingEditor = false;
    }
  }

  private async prepareExportRoot(): Promise<void> {
    try {
      await mkdir(this.exportRoot, { mode: 0o700 });
    } catch (error) {
      if (!isAlreadyExists(error)) throw error;
    }
    const root = await lstat(this.exportRoot);
    if (
      !root.isDirectory() ||
      root.isSymbolicLink() ||
      (typeof process.getuid === "function" && root.uid !== process.getuid()) ||
      (root.mode & 0o077) !== 0
    ) {
      throw new Error("The editor export root is not private");
    }
  }

  private scheduleCleanup(
    exportPath: string,
    identity: FileIdentity,
    reservation: EditorReservation,
  ): void {
    void this.cleanupExport(exportPath, identity, reservation);
  }

  private async cleanupExport(
    exportPath: string | null,
    identity: FileIdentity | null,
    reservation: EditorReservation,
  ): Promise<void> {
    const absent =
      exportPath === null
        ? true
        : await removeExport(
            this.exportRoot,
            exportPath,
            identity,
          ).catch(() => false);
    if (!absent) return;
    try {
      const result = await this.transport.requestMutation(
        "utilities.job_log_release",
        { slot: reservation.slot, token: reservation.token },
      ).result;
      exactKeys(result, ["released"]);
      if (typeof result.released !== "boolean")
        throw new Error("Invalid editor reservation release result");
    } catch {
      // The retained ledger row is reclaimed by a later explicit operation.
    }
  }
}

function defaultSpawnEditor(command: string, args: readonly string[]): ChildProcess {
  return spawn(command, args, {
    detached: true,
    shell: false,
    stdio: "ignore",
  });
}

function attachCleanup(child: ChildProcess, cleanup: () => void): void {
  let scheduled = false;
  const once = () => {
    if (scheduled) return;
    scheduled = true;
    cleanup();
  };
  child.once("exit", once);
  child.once("error", once);
}

async function startEditor(
  launch: SpawnEditor,
  argv: readonly string[],
  exportPath: string,
): Promise<
  | { readonly ok: true; readonly child: ChildProcess; readonly exited: boolean }
  | {
      readonly ok: false;
      readonly message: string;
      readonly retain: false;
    }
  | {
      readonly ok: false;
      readonly message: string;
      readonly retain: true;
      readonly child: ChildProcess;
      readonly exited: boolean;
    }
> {
  const [command, ...configuredArgs] = argv;
  if (command === undefined) throw new Error("Missing editor executable");
  const child = launch(command, [...configuredArgs, exportPath]);
  let exitObserved = false;
  let observedExitCode: number | null = null;
  let errorObserved = false;
  const exit = new Promise<number | null>((resolve) => {
    child.once("exit", (code) => {
      exitObserved = true;
      observedExitCode = code;
      resolve(code);
    });
  });
  const processError = new Promise<"error">((resolve) => {
    child.once("error", () => {
      errorObserved = true;
      resolve("error");
    });
  });
  const launchOutcome = await new Promise<"spawn" | "error" | "timeout">(
    (resolve) => {
      const timer = setTimeout(() => resolve("timeout"), LAUNCH_TIMEOUT_MILLISECONDS);
      child.once("spawn", () => {
        clearTimeout(timer);
        resolve("spawn");
      });
      child.once("error", () => {
        clearTimeout(timer);
        resolve("error");
      });
    },
  );
  if (launchOutcome === "error") {
    return {
      ok: false,
      message: "The configured editor executable is unavailable or could not start.",
      retain: false,
    };
  }
  if (launchOutcome === "timeout") {
    return {
      ok: false,
      message: "The editor did not report starting within five seconds.",
      retain: true,
      child,
      exited: exitObserved,
    };
  }
  const earlyExit = errorObserved
    ? "error"
    : exitObserved
    ? observedExitCode
    : await Promise.race<number | null | "error" | "running">([
        exit,
        processError,
        new Promise<"running">((resolve) =>
          setTimeout(() => resolve("running"), EARLY_EXIT_MILLISECONDS),
        ),
      ]);
  if (earlyExit === "error") {
    return {
      ok: false,
      message: "The configured editor reported an error immediately after starting.",
      retain: false,
    };
  }
  if (earlyExit !== "running" && earlyExit !== 0) {
    return {
      ok: false,
      message: "The configured editor exited with an error immediately after starting.",
      retain: false,
    };
  }
  return { ok: true, child, exited: earlyExit !== "running" };
}

async function createPrivateExport(filePath: string): Promise<PrivateExport> {
  const flags =
    constants.O_WRONLY |
    constants.O_CREAT |
    constants.O_EXCL |
    (constants.O_NOFOLLOW ?? 0);
  const file = await open(filePath, flags, 0o600);
  try {
    const metadata = await file.stat();
    if (
      !metadata.isFile() ||
      (typeof process.getuid === "function" && metadata.uid !== process.getuid())
    ) {
      throw new Error("The editor export file is not owned by this user");
    }
    return {
      file,
      identity: { device: metadata.dev, inode: metadata.ino },
    };
  } catch (error) {
    await file.close().catch(() => undefined);
    throw error;
  }
}

async function removeExport(
  exportRoot: string,
  filePath: string,
  identity: FileIdentity | null,
): Promise<boolean> {
  try {
    const metadata = await lstat(filePath);
    if (
      identity === null ||
      !EXPORT_NAME.test(path.basename(filePath)) ||
      path.dirname(filePath) !== exportRoot ||
      !metadata.isFile() ||
      metadata.isSymbolicLink() ||
      (typeof process.getuid === "function" && metadata.uid !== process.getuid()) ||
      metadata.dev !== identity.device ||
      metadata.ino !== identity.inode
    ) {
      return false;
    }
    await unlink(filePath);
    return true;
  } catch (error) {
    if (isMissing(error)) return true;
    throw error;
  }
}

function assertReviewUrlPlan(value: unknown, review: string): ReviewUrlPlan {
  exactKeys(value, ["review", "url"]);
  if (value.review !== review || typeof value.url !== "string")
    throw new Error("Invalid review URL plan");
  return value as unknown as ReviewUrlPlan;
}

function assertEditorLogPlan(value: unknown, job: string): EditorLogPlan {
  exactKeys(value, [
    "status",
    "message",
    "job",
    "job_id",
    "argv",
    "content",
    "slot",
    "token",
    "export_name",
  ]);
  const statuses = new Set<EditorPlanStatus>([
    "ready",
    "disabled",
    "missing",
    "malformed",
    "terminal_unsupported",
    "log_too_large",
    "capacity_exceeded",
  ]);
  const reservation =
    typeof value.export_name === "string"
      ? EXPORT_NAME.exec(value.export_name)
      : null;
  if (
    typeof value.status !== "string" ||
    !statuses.has(value.status as EditorPlanStatus) ||
    typeof value.message !== "string" ||
    value.message.length === 0 ||
    value.message.length > 1000 ||
    value.job !== job ||
    !Number.isSafeInteger(value.job_id) ||
    Number(value.job_id) <= 0 ||
    !Array.isArray(value.argv) ||
    value.argv.length > MAX_EDITOR_ARGUMENTS ||
    value.argv.some(
      (item) =>
        typeof item !== "string" ||
        item.length === 0 ||
        Buffer.byteLength(item, "utf8") > MAX_EDITOR_ARGUMENT_BYTES ||
        [...item].some((character) => character.codePointAt(0)! < 32),
    ) ||
    (value.content !== null && typeof value.content !== "string") ||
    (typeof value.content === "string" &&
      Buffer.byteLength(value.content, "utf8") > MAX_EDITOR_LOG_BYTES) ||
    (value.status === "ready" &&
      (value.argv.length === 0 ||
        typeof value.content !== "string" ||
        !Number.isSafeInteger(value.slot) ||
        Number(value.slot) < 1 ||
        Number(value.slot) > 8 ||
        typeof value.token !== "string" ||
        !/^[0-9a-f]{32}$/.test(value.token) ||
        typeof value.export_name !== "string" ||
        reservation === null ||
        Number(reservation[1]) !== value.slot ||
        Number(reservation[2]) !== value.job_id ||
        reservation[3] !== value.token)) ||
    (value.status !== "ready" &&
      (value.argv.length !== 0 ||
        value.content !== null ||
        value.slot !== null ||
        value.token !== null ||
        value.export_name !== null))
  ) {
    throw new Error("Invalid editor log plan");
  }
  return value as unknown as EditorLogPlan;
}

function opaqueHandle(value: unknown): string {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > 100 ||
    [...value].some((character) => character.codePointAt(0)! < 32)
  ) {
    throw new Error("Invalid opaque handle");
  }
  return value;
}

function emptyParams(value: unknown): void {
  exactKeys(value, []);
}

function exactKeys(
  value: unknown,
  expected: readonly string[],
): asserts value is Record<string, unknown> {
  if (
    value === null ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    Object.keys(value).length !== expected.length ||
    expected.some((key) => !Object.hasOwn(value, key))
  ) {
    throw new Error("Invalid utility value");
  }
}

function utilityResult(
  outcome: EditorOpenOutcome,
  message: string,
): OpenJobLogEditorResult {
  return Object.freeze({ outcome, message });
}

function isMissing(error: unknown): boolean {
  return (
    error !== null &&
    typeof error === "object" &&
    "code" in error &&
    error.code === "ENOENT"
  );
}

function isAlreadyExists(error: unknown): boolean {
  return (
    error !== null &&
    typeof error === "object" &&
    "code" in error &&
    error.code === "EEXIST"
  );
}
