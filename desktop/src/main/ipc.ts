import {
  ipcMain,
  shell,
  type BrowserWindow,
  type IpcMainInvokeEvent,
} from "electron";
import {
  IPC_CHANNELS,
  type DesktopEvent,
  type JsonObject,
  type JsonValue,
} from "../shared/bridge.js";
import type { AssetCatalog } from "./assets.js";
import {
  assertCIParams,
  assertCIResult,
  CI_OPERATIONS,
  type CIOperation,
} from "./ci.js";
import type {
  CIMutationIPCResult,
  CIMutationReceipt,
} from "../shared/ci.js";
import {
  assertAuthorizedSender,
  assertHttpsExternalUrl,
  assertParams,
  assertResult,
} from "./security.js";
import type { SidecarTransport } from "./sidecar.js";

interface Invocation {
  readonly requestToken: string;
  readonly params: unknown;
}
interface Binding {
  readonly requestId: string;
  readonly generation: number;
  readonly senderId: number;
}

const OPERATIONS = new Map<string, string>([
  [IPC_CHANNELS.discoverRepositories, "repositories.discover"],
  [IPC_CHANNELS.openRepository, "repositories.open"],
  [IPC_CHANNELS.listReviews, "reviews.list"],
  [IPC_CHANNELS.getReview, "reviews.get"],
  [IPC_CHANNELS.openDiff, "diff.open"],
  [IPC_CHANNELS.pageDiff, "diff.page"],
  [IPC_CHANNELS.listDiscussions, "discussions.list"],
  [IPC_CHANNELS.listCommits, "commits.list"],
  [IPC_CHANNELS.listPipelines, "pipelines.list"],
  [IPC_CHANNELS.listReviewPipelines, "review_pipelines.list"],
  [IPC_CHANNELS.listJobs, "jobs.list"],
  [IPC_CHANNELS.openLog, "logs.open"],
  [IPC_CHANNELS.pageLog, "logs.page"],
  [IPC_CHANNELS.listPlugins, "plugins.list"],
  [IPC_CHANNELS.invokePlugin, "plugins.invoke"],
]);

export class DesktopIpcController {
  private readonly bindings = new Map<string, Binding>();
  private eventListener: ((event: DesktopEvent) => void) | null = null;

  constructor(
    private readonly window: BrowserWindow,
    private readonly transport: SidecarTransport,
    private readonly assets: AssetCatalog,
  ) {}

  register(): void {
    for (const [channel, method] of OPERATIONS) {
      ipcMain.handle(channel, (event, invocation) =>
        this.read(event, method, invocation),
      );
    }
    for (const operation of CI_OPERATIONS) {
      ipcMain.handle(operation.channel, (event, value) =>
        operation.mutation
          ? this.mutate(event, operation, value)
          : this.read(
              event,
              operation.method,
              value,
              assertCIParams,
              assertCIResult,
            ),
      );
    }
    ipcMain.handle(IPC_CHANNELS.listAssets, (event, invocation) =>
      this.listAssets(event, invocation),
    );
    ipcMain.handle(IPC_CHANNELS.setLocation, (event, params) =>
      this.call(event, "host.set_location", params),
    );
    ipcMain.handle(IPC_CHANNELS.cancelRead, (event, token) =>
      this.cancel(event, token),
    );
    ipcMain.handle(IPC_CHANNELS.openExternal, (event, url) =>
      this.openExternal(event, url),
    );
    this.eventListener = (event) => {
      if (!this.window.isDestroyed()) {
        this.window.webContents.send(IPC_CHANNELS.event, event);
      }
    };
    this.transport.on("event", this.eventListener);
  }

  reset(): void {
    this.bindings.clear();
  }

  dispose(): void {
    this.reset();
    for (const channel of [
      ...OPERATIONS.keys(),
      ...CI_OPERATIONS.map((operation) => operation.channel),
      IPC_CHANNELS.listAssets,
      IPC_CHANNELS.setLocation,
      IPC_CHANNELS.cancelRead,
      IPC_CHANNELS.openExternal,
    ]) {
      ipcMain.removeHandler(channel);
    }
    if (this.eventListener) {
      this.transport.off("event", this.eventListener);
    }
    this.eventListener = null;
  }

  private async read(
    event: IpcMainInvokeEvent,
    method: string,
    value: unknown,
    validateParams: ParamsValidator = assertParams,
    validateResult: ResultValidator = assertResult,
  ): Promise<JsonValue> {
    assertAuthorizedSender(event, this.window.webContents);
    const invocation = parseInvocation(value);
    validateParams(method, invocation.params);
    if (this.bindings.has(invocation.requestToken)) {
      throw new Error("Duplicate desktop request token");
    }
    const request = this.transport.requestRead(method, invocation.params);
    const binding = this.bindingFor(event, request.requestId);
    this.bindings.set(invocation.requestToken, binding);
    try {
      const result = await request.result;
      validateResult(method, result);
      return result;
    } finally {
      this.deleteBinding(invocation.requestToken, binding);
    }
  }

  private async mutate(
    event: IpcMainInvokeEvent,
    operation: CIOperation,
    value: unknown,
  ): Promise<JsonValue> {
    assertAuthorizedSender(event, this.window.webContents);
    assertCIParams(operation.method, value);
    try {
      const result = await this.transport.requestMutation(
        operation.method,
        value,
      ).result;
      assertCIResult(operation.method, result);
      const response = {
        receipt: result as unknown as CIMutationReceipt,
        error: null,
      } satisfies CIMutationIPCResult;
      return response as unknown as JsonValue;
    } catch (error) {
      const response = {
        receipt: null,
        error: safeMutationError(error),
      } satisfies CIMutationIPCResult;
      return response as unknown as JsonValue;
    }
  }

  private async listAssets(
    event: IpcMainInvokeEvent,
    value: unknown,
  ): Promise<JsonValue> {
    assertAuthorizedSender(event, this.window.webContents);
    const invocation = parseInvocation(value);
    assertParams("assets.list", invocation.params);
    if (this.bindings.has(invocation.requestToken)) {
      throw new Error("Duplicate desktop request token");
    }
    const request = this.assets.beginRefresh();
    const binding = this.bindingFor(event, request.requestId);
    this.bindings.set(invocation.requestToken, binding);
    try {
      return [...(await request.result)] as unknown as JsonValue;
    } finally {
      this.deleteBinding(invocation.requestToken, binding);
    }
  }

  private async call(
    event: IpcMainInvokeEvent,
    method: string,
    params: unknown,
  ): Promise<JsonValue> {
    assertAuthorizedSender(event, this.window.webContents);
    assertParams(method, params);
    const result = await this.transport.requestRead(method, params).result;
    assertResult(method, result);
    return result;
  }

  private cancel(event: IpcMainInvokeEvent, token: unknown): boolean {
    assertAuthorizedSender(event, this.window.webContents);
    if (typeof token !== "string" || token.length > 128) return false;
    const binding = this.bindings.get(token);
    if (
      !binding ||
      binding.senderId !== event.sender.id ||
      binding.generation !== this.transport.sessionGeneration
    ) {
      return false;
    }
    return this.transport.cancelRead(binding.requestId);
  }

  private async openExternal(
    event: IpcMainInvokeEvent,
    value: unknown,
  ): Promise<boolean> {
    assertAuthorizedSender(event, this.window.webContents);
    await shell.openExternal(assertHttpsExternalUrl(value));
    return true;
  }

  private bindingFor(event: IpcMainInvokeEvent, requestId: string): Binding {
    return {
      requestId,
      generation: this.transport.sessionGeneration,
      senderId: event.sender.id,
    };
  }

  private deleteBinding(token: string, binding: Binding): void {
    if (this.bindings.get(token) === binding) this.bindings.delete(token);
  }
}

function safeMutationError(error: unknown): {
  readonly code: string;
  readonly message: string;
  readonly retryable: boolean;
} {
  const code =
    error !== null &&
    typeof error === "object" &&
    "code" in error &&
    typeof error.code === "string" &&
    /^[a-z][a-z0-9_]{0,79}$/.test(error.code)
      ? error.code
      : "invalid_response";
  const retryable =
    error !== null &&
    typeof error === "object" &&
    "retryable" in error &&
    error.retryable === true;
  return {
    code,
    message:
      code === "mutation_timeout" ||
      code === "unexpected_eof" ||
      code === "write_failed" ||
      code === "invalid_response"
        ? "The CI action result could not be confirmed."
        : "The CI action was rejected with a known result.",
    retryable,
  };
}

type ParamsValidator = (
  method: string,
  value: unknown,
) => asserts value is JsonObject;
type ResultValidator = (
  method: string,
  value: unknown,
) => asserts value is JsonValue;

function parseInvocation(value: unknown): Invocation {
  if (
    !isRecord(value) ||
    typeof value.requestToken !== "string" ||
    value.requestToken.length < 16 ||
    value.requestToken.length > 128 ||
    !Object.hasOwn(value, "params")
  ) {
    throw new Error("Invalid desktop request envelope");
  }
  return { requestToken: value.requestToken, params: value.params };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
