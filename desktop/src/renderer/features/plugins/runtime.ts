import type {
  AssetDescriptor,
  DesktopBridge,
  DesktopEvent,
  JsonObject,
  JsonValue,
} from "../../../shared/bridge.js";
import type { AppRoute } from "../../core/navigation.js";
import {
  parsePluginRecords,
  resolvePluginResources,
  type DesktopPluginModule,
  type PluginCleanup,
  type PluginManifest,
  type PluginNotificationSeverity,
  type PluginRecord,
  type PluginUiApi,
} from "./types.js";

const MAX_HELP_BYTES = 1024 * 1024;
const MAX_NOTIFICATIONS = 6;
const DEFAULT_CLEANUP_TIMEOUT_MS = 1_000;

export interface PluginNotice {
  readonly id: number;
  readonly pluginId: string;
  readonly message: string;
  readonly severity: PluginNotificationSeverity;
}

export interface PluginCatalogSnapshot {
  readonly generation: number;
  readonly loading: boolean;
  readonly error: string | null;
  readonly records: readonly PluginRecord[];
  readonly notices: readonly PluginNotice[];
}

export interface PluginRuntimeDependencies {
  readonly loadModule: (url: string) => Promise<unknown>;
  readonly loadHelp: (url: string, signal: AbortSignal) => Promise<string>;
  readonly addStylesheet: (
    url: string,
    signal: AbortSignal,
    root: ShadowRoot,
  ) => Promise<() => void>;
  readonly cleanupTimeoutMs: number;
}

export interface PluginMountStatus {
  readonly loading: boolean;
  readonly error: string | null;
}

interface ActiveRoute {
  readonly pluginId: string;
  readonly navigationId: string;
  readonly moduleId: string;
}

export class PluginLocationPublisher {
  private sequence = 0;
  private queue = Promise.resolve();

  constructor(private readonly bridge: Pick<DesktopBridge, "setLocation">) {}

  publish(route: AppRoute | null): Promise<void> {
    const sequence = ++this.sequence;
    this.queue = this.queue
      .catch(() => undefined)
      .then(async () => {
        if (sequence !== this.sequence) return;
        await this.bridge.setLocation({ location: routeLocation(route) });
      })
      .catch(() => undefined);
    return this.queue;
  }

  dispose(): Promise<void> {
    return this.publish(null);
  }
}

export class PluginRuntime {
  private listeners = new Set<() => void>();
  private eventCleanup: (() => void) | null = null;
  private refreshGeneration = 0;
  private pendingTokens = new Set<string>();
  private active: PluginMountSession | null = null;
  private nextNoticeId = 0;
  private snapshotValue: PluginCatalogSnapshot = Object.freeze({
    generation: 0,
    loading: false,
    error: null,
    records: Object.freeze([]),
    notices: Object.freeze([]),
  });

  constructor(
    private readonly bridge: DesktopBridge,
    private readonly dependencies: PluginRuntimeDependencies = defaultDependencies,
  ) {
    if (
      !Number.isFinite(dependencies.cleanupTimeoutMs) ||
      dependencies.cleanupTimeoutMs <= 0
    )
      throw new Error("Plugin cleanup timeout must be positive");
  }

  get snapshot(): PluginCatalogSnapshot {
    return this.snapshotValue;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  start(): void {
    if (!this.eventCleanup)
      this.eventCleanup = this.bridge.onEvent((event) =>
        this.dispatchEvent(event),
      );
    void this.refresh();
  }

  async refresh(): Promise<void> {
    const generation = ++this.refreshGeneration;
    this.update({ loading: true, error: null });
    const pluginRead = this.bridge.listPlugins();
    const assetRead = this.bridge.listAssets();
    this.pendingTokens.add(pluginRead.requestToken);
    this.pendingTokens.add(assetRead.requestToken);
    try {
      const [plugins, assets] = await Promise.all([
        pluginRead.result,
        assetRead.result,
      ]);
      if (generation !== this.refreshGeneration) return;
      const records = parsePluginRecords(plugins.plugins);
      this.snapshotValue = Object.freeze({
        ...this.snapshotValue,
        generation: this.snapshotValue.generation + 1,
        loading: false,
        error: null,
        records,
      });
      this.assets = Object.freeze([...assets]);
      this.emit();
    } catch {
      if (generation !== this.refreshGeneration) return;
      this.update({
        loading: false,
        error:
          "Installed plugins could not be loaded. Core reviews remain available.",
      });
    } finally {
      this.pendingTokens.delete(pluginRead.requestToken);
      this.pendingTokens.delete(assetRead.requestToken);
    }
  }

  private assets: readonly AssetDescriptor[] = Object.freeze([]);

  async mount(
    route: ActiveRoute,
    container: HTMLElement,
    navigate: (route: AppRoute) => void,
    status: (value: PluginMountStatus) => void,
  ): Promise<void> {
    await this.unmount();
    const record = this.record(route.pluginId);
    if (!record || record.state !== "started" || !record.manifest) {
      status({ loading: false, error: "This plugin module is unavailable." });
      return;
    }
    const navigation = record.manifest.navigation.find(
      (item) =>
        item.id === route.navigationId && item.moduleId === route.moduleId,
    );
    if (!navigation) {
      status({
        loading: false,
        error: "This plugin navigation target is unavailable.",
      });
      return;
    }
    let resources;
    try {
      resources = resolvePluginResources(record, route.moduleId, this.assets);
    } catch {
      status({
        loading: false,
        error: "The plugin resources changed or are missing.",
      });
      return;
    }
    const session = new PluginMountSession(
      this.bridge,
      this.dependencies,
      record,
      route,
      resources,
      container,
      navigate,
      (message, severity) => this.notify(record.pluginId, message, severity),
      status,
    );
    this.active = session;
    await session.start();
    if (this.active !== session) await session.stop();
  }

  async unmount(): Promise<void> {
    const active = this.active;
    if (!active) return;
    this.active = null;
    await active.stop();
  }

  async loadHelp(
    pluginId: string,
    signal: AbortSignal,
  ): Promise<string | null> {
    const record = this.record(pluginId);
    if (!record?.manifest?.helpAsset || record.state !== "started") return null;
    const moduleId = record.manifest.modules[0]?.id;
    if (!moduleId) return null;
    const resources = resolvePluginResources(record, moduleId, this.assets);
    if (!resources.helpUrl) return null;
    const source = await this.dependencies.loadHelp(resources.helpUrl, signal);
    if (new TextEncoder().encode(source).byteLength > MAX_HELP_BYTES)
      throw new Error("Plugin help is too large");
    return source;
  }

  async dispose(): Promise<void> {
    ++this.refreshGeneration;
    this.eventCleanup?.();
    this.eventCleanup = null;
    const tokens = [...this.pendingTokens];
    this.pendingTokens.clear();
    await boundedWait(
      Promise.allSettled(tokens.map((token) => this.bridge.cancelRead(token))),
      this.dependencies.cleanupTimeoutMs,
    );
    await this.unmount();
    this.listeners.clear();
  }

  record(pluginId: string): PluginRecord | undefined {
    return this.snapshotValue.records.find(
      (record) => record.pluginId === pluginId,
    );
  }

  dismissNotice(id: number): void {
    this.snapshotValue = Object.freeze({
      ...this.snapshotValue,
      notices: Object.freeze(
        this.snapshotValue.notices.filter((item) => item.id !== id),
      ),
    });
    this.emit();
  }

  private dispatchEvent(event: DesktopEvent): void {
    if (!isObject(event.data)) return;
    const pluginId = event.data.plugin;
    if (typeof pluginId !== "string" || !this.record(pluginId)) return;
    if (event.name === "plugin.event") {
      const eventId = event.data.event;
      const payload = event.data.payload;
      if (typeof eventId !== "string" || !isObject(payload)) return;
      this.active?.publish(pluginId, eventId, payload);
    } else if (event.name === "plugin.notification") {
      const message = event.data.message;
      const severity = event.data.severity;
      if (typeof message !== "string" || !isSeverity(severity)) return;
      this.notify(pluginId, message, severity);
    } else if (event.name === "plugin.focus") {
      const target = event.data.target;
      if (typeof target !== "string") return;
      this.active?.focus(pluginId, target);
    }
  }

  private notify(
    pluginId: string,
    message: string,
    severity: PluginNotificationSeverity,
  ): void {
    const safeMessage = message.trim().slice(0, 2048);
    if (!safeMessage) return;
    const notice = Object.freeze({
      id: ++this.nextNoticeId,
      pluginId,
      message: safeMessage,
      severity,
    });
    this.snapshotValue = Object.freeze({
      ...this.snapshotValue,
      notices: Object.freeze(
        [...this.snapshotValue.notices, notice].slice(-MAX_NOTIFICATIONS),
      ),
    });
    this.emit();
  }

  private update(
    changes: Pick<PluginCatalogSnapshot, "loading" | "error">,
  ): void {
    this.snapshotValue = Object.freeze({ ...this.snapshotValue, ...changes });
    this.emit();
  }

  private emit(): void {
    for (const listener of this.listeners) listener();
  }
}

class PluginMountSession {
  private readonly controller = new AbortController();
  private readonly eventListeners = new Map<
    string,
    Set<(payload: Readonly<JsonObject>) => void>
  >();
  private readonly focusTargets = new Map<string, HTMLElement>();
  private readonly pendingTokens = new Set<string>();
  private stylesheetCleanups: (() => void)[] = [];
  private cleanup: PluginCleanup | null = null;
  private stopped = false;
  private mountRoot: ShadowRoot | null = null;
  private mountContainer: HTMLElement | null = null;

  constructor(
    private readonly bridge: DesktopBridge,
    private readonly dependencies: PluginRuntimeDependencies,
    private readonly record: PluginRecord,
    private readonly route: ActiveRoute,
    private readonly resources: ReturnType<typeof resolvePluginResources>,
    private readonly container: HTMLElement,
    private readonly navigateHost: (route: AppRoute) => void,
    private readonly notifyHost: (
      message: string,
      severity: PluginNotificationSeverity,
    ) => void,
    private readonly status: (value: PluginMountStatus) => void,
  ) {}

  async start(): Promise<void> {
    this.status({ loading: true, error: null });
    try {
      this.mountRoot =
        this.container.shadowRoot ??
        this.container.attachShadow({ mode: "open" });
      this.mountContainer = document.createElement("div");
      this.mountContainer.className = "plugin-surface";
      this.mountRoot.replaceChildren(this.mountContainer);
      this.stylesheetCleanups = await Promise.all(
        this.resources.stylesheetUrls.map((url) =>
          this.dependencies.addStylesheet(
            url,
            this.controller.signal,
            this.mountRoot!,
          ),
        ),
      );
      this.ensureActive();
      const imported = await this.dependencies.loadModule(
        this.resources.moduleUrl,
      );
      this.ensureActive();
      const pluginModule = validateModule(imported);
      const cleanup = await pluginModule.mount(this.mountContainer, this.api());
      if (typeof cleanup !== "function")
        throw new Error("Plugin module did not return cleanup");
      if (this.stopped || this.controller.signal.aborted) {
        await boundedCleanup(cleanup, this.dependencies.cleanupTimeoutMs);
        return;
      }
      this.cleanup = cleanup;
      this.status({ loading: false, error: null });
    } catch {
      if (!this.stopped) {
        await this.releaseResources();
        this.status({
          loading: false,
          error:
            "The plugin module could not be displayed. Core reviews remain available.",
        });
      }
    }
  }

  publish(pluginId: string, eventId: string, payload: JsonObject): void {
    if (
      this.stopped ||
      pluginId !== this.record.pluginId ||
      !this.record.manifest?.events.has(eventId)
    )
      return;
    for (const listener of this.eventListeners.get(eventId) ?? []) {
      try {
        listener(Object.freeze({ ...payload }));
      } catch {
        this.notifyHost("A plugin event listener failed.", "error");
      }
    }
  }

  focus(pluginId: string, targetId: string): void {
    if (this.stopped || pluginId !== this.record.pluginId) return;
    const declaration = this.record.manifest?.focusTargets.find(
      (item) => item.id === targetId && item.moduleId === this.route.moduleId,
    );
    const element = declaration ? this.focusTargets.get(targetId) : undefined;
    if (element && this.mountContainer?.contains(element)) element.focus();
  }

  async stop(): Promise<void> {
    if (this.stopped) return;
    this.stopped = true;
    this.controller.abort();
    const tokens = [...this.pendingTokens];
    this.pendingTokens.clear();
    await boundedWait(
      Promise.allSettled(tokens.map((token) => this.bridge.cancelRead(token))),
      this.dependencies.cleanupTimeoutMs,
    );
    await this.releaseResources();
  }

  private api(): PluginUiApi {
    const manifest = this.record.manifest!;
    return Object.freeze({
      pluginId: this.record.pluginId,
      signal: this.controller.signal,
      invoke: async (
        methodId: string,
        params: JsonObject,
      ): Promise<JsonValue> => {
        this.ensureActive();
        if (!manifest.methods.has(methodId))
          throw new Error("Unknown plugin method");
        const safeParams = cloneJsonObject(params);
        const read = this.bridge.invokePlugin({
          plugin: this.record.pluginId,
          method: methodId,
          params: safeParams,
        });
        this.pendingTokens.add(read.requestToken);
        try {
          const result = await read.result;
          this.ensureActive();
          return result.value;
        } finally {
          this.pendingTokens.delete(read.requestToken);
        }
      },
      on: (
        eventId: string,
        listener: (payload: Readonly<JsonObject>) => void,
      ): (() => void) => {
        this.ensureActive();
        if (!manifest.events.has(eventId) || typeof listener !== "function")
          throw new Error("Unknown plugin event");
        const listeners = this.eventListeners.get(eventId) ?? new Set();
        listeners.add(listener);
        this.eventListeners.set(eventId, listeners);
        return () => listeners.delete(listener);
      },
      navigate: (navigationId: string): void => {
        this.ensureActive();
        const navigation = manifest.navigation.find(
          (item) => item.id === navigationId,
        );
        if (!navigation) throw new Error("Unknown plugin navigation target");
        this.navigateHost({
          kind: "plugin",
          pluginId: this.record.pluginId,
          navigationId: navigation.id,
          moduleId: navigation.moduleId,
        });
      },
      notify: (
        message: string,
        severity: PluginNotificationSeverity = "information",
      ): void => {
        this.ensureActive();
        if (typeof message !== "string" || !isSeverity(severity))
          throw new Error("Invalid plugin notification");
        this.notifyHost(message, severity);
      },
      currentLocation: (): Readonly<JsonObject> =>
        Object.freeze({
          kind: "plugin",
          plugin_id: this.record.pluginId,
          navigation_id: this.route.navigationId,
          module_id: this.route.moduleId,
        }),
      bindFocusTarget: (
        targetId: string,
        element: HTMLElement,
      ): (() => void) => {
        this.ensureActive();
        const target = manifest.focusTargets.find(
          (item) =>
            item.id === targetId && item.moduleId === this.route.moduleId,
        );
        if (
          !target ||
          !(element instanceof HTMLElement) ||
          !this.mountContainer?.contains(element) ||
          !isFocusable(element) ||
          this.focusTargets.has(targetId)
        )
          throw new Error("Invalid plugin focus target");
        this.focusTargets.set(targetId, element);
        return () => {
          if (this.focusTargets.get(targetId) === element)
            this.focusTargets.delete(targetId);
        };
      },
    });
  }

  private async releaseResources(): Promise<void> {
    const cleanup = this.cleanup;
    this.cleanup = null;
    this.eventListeners.clear();
    this.focusTargets.clear();
    if (cleanup) {
      const outcome = await boundedCleanup(
        cleanup,
        this.dependencies.cleanupTimeoutMs,
      );
      if (outcome !== "complete")
        this.notifyHost(
          outcome === "timeout"
            ? "Plugin cleanup reached its time limit."
            : "Plugin cleanup failed.",
          "warning",
        );
    }
    for (const remove of this.stylesheetCleanups.splice(0)) remove();
    this.mountRoot?.replaceChildren();
    this.mountContainer = null;
    this.container.replaceChildren();
  }

  private ensureActive(): void {
    if (this.stopped || this.controller.signal.aborted)
      throw new DOMException("Plugin module stopped", "AbortError");
  }
}

const defaultDependencies: PluginRuntimeDependencies = Object.freeze({
  loadModule: async (url: string): Promise<unknown> => import(url),
  loadHelp: async (url: string, signal: AbortSignal): Promise<string> => {
    const response = await fetch(url, {
      signal,
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
    });
    if (!response.ok) throw new Error("Plugin help could not be loaded");
    return response.text();
  },
  addStylesheet: (
    url: string,
    signal: AbortSignal,
    root: ShadowRoot,
  ): Promise<() => void> =>
    new Promise((resolve, reject) => {
      const link = document.createElement("link");
      const failed = (): void => {
        link.remove();
        reject(new Error("Plugin stylesheet could not be loaded"));
      };
      const loaded = (): void => {
        signal.removeEventListener("abort", failed);
        resolve(() => link.remove());
      };
      link.rel = "stylesheet";
      link.href = url;
      link.addEventListener("load", loaded, { once: true });
      link.addEventListener("error", failed, { once: true });
      signal.addEventListener("abort", failed, { once: true });
      root.append(link);
    }),
  cleanupTimeoutMs: DEFAULT_CLEANUP_TIMEOUT_MS,
});

function validateModule(value: unknown): DesktopPluginModule {
  if (!isObject(value) || typeof value.mount !== "function")
    throw new Error("Invalid plugin module");
  return value as unknown as DesktopPluginModule;
}

async function boundedCleanup(
  cleanup: PluginCleanup,
  timeoutMs: number,
): Promise<"complete" | "failed" | "timeout"> {
  let timeout: ReturnType<typeof setTimeout> | undefined;
  try {
    const result = await Promise.race([
      Promise.resolve()
        .then(cleanup)
        .then(
          () => "complete" as const,
          () => "failed" as const,
        ),
      new Promise<"timeout">((resolve) => {
        timeout = setTimeout(() => resolve("timeout"), timeoutMs);
      }),
    ]);
    return result;
  } finally {
    if (timeout) clearTimeout(timeout);
  }
}

async function boundedWait(
  promise: Promise<unknown>,
  timeoutMs: number,
): Promise<void> {
  let timeout: ReturnType<typeof setTimeout> | undefined;
  try {
    await Promise.race([
      promise.catch(() => undefined),
      new Promise<void>((resolve) => {
        timeout = setTimeout(resolve, timeoutMs);
      }),
    ]);
  } finally {
    if (timeout) clearTimeout(timeout);
  }
}

function isObject(value: unknown): value is JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isSeverity(value: unknown): value is PluginNotificationSeverity {
  return value === "information" || value === "warning" || value === "error";
}

function isFocusable(element: HTMLElement): boolean {
  return (
    element.tabIndex >= 0 ||
    element.matches("a[href],button,input,select,textarea")
  );
}

function cloneJsonObject(value: JsonObject): JsonObject {
  return cloneJson(value, 0) as JsonObject;
}

function cloneJson(value: JsonValue, depth: number): JsonValue {
  if (depth > 16) throw new Error("Plugin data is too deep");
  if (
    value === null ||
    typeof value === "string" ||
    typeof value === "boolean" ||
    (typeof value === "number" && Number.isFinite(value))
  )
    return value;
  if (Array.isArray(value)) {
    if (value.length > 4096) throw new Error("Plugin data is too large");
    return value.map((item) => cloneJson(item, depth + 1));
  }
  if (!isObject(value)) throw new Error("Plugin data is not JSON");
  const prototype = Object.getPrototypeOf(value);
  if (prototype !== Object.prototype && prototype !== null)
    throw new Error("Plugin data is not a plain object");
  const entries = Object.entries(value);
  if (entries.length > 4096) throw new Error("Plugin data is too large");
  const result: JsonObject = {};
  for (const [key, item] of entries) {
    if (["__proto__", "prototype", "constructor"].includes(key))
      throw new Error("Plugin data has an invalid key");
    result[key] = cloneJson(item, depth + 1);
  }
  return result;
}

export function routeLocation(route: AppRoute | null): JsonObject | null {
  if (route === null) return null;
  if (route.kind === "inbox")
    return route.repository
      ? { kind: "inbox", repository: route.repository.handle }
      : { kind: "inbox" };
  if (route.kind === "review")
    return { kind: "review", review: route.item.handle, panel: route.panel };
  return {
    kind: "plugin",
    plugin_id: route.pluginId,
    navigation_id: route.navigationId,
    module_id: route.moduleId,
  };
}
