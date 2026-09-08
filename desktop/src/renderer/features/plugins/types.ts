import type {
  AssetDescriptor,
  JsonObject,
  JsonValue,
  PluginDto,
} from "../../../shared/bridge.js";

const LOCAL_ID = /^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$/;
const PLUGIN_STATES = new Set([
  "discovered",
  "started",
  "disabled",
  "terminal_only",
  "incompatible",
  "failed",
  "stopped",
]);

export type PluginState =
  | "discovered"
  | "started"
  | "disabled"
  | "terminal_only"
  | "incompatible"
  | "failed"
  | "stopped";

export interface PluginModuleDeclaration {
  readonly id: string;
  readonly title: string;
  readonly entryAsset: string | null;
  readonly stylesheetAssets: readonly (string | null)[];
}

export interface PluginNavigationDeclaration {
  readonly id: string;
  readonly title: string;
  readonly moduleId: string;
}

export interface PluginCommandDeclaration {
  readonly id: string;
  readonly title: string;
  readonly navigationId: string;
  readonly helpText: string;
}

export interface PluginFocusDeclaration {
  readonly id: string;
  readonly title: string;
  readonly moduleId: string;
}

export interface PluginManifest {
  readonly title: string;
  readonly version: string;
  readonly apiMajor: number;
  readonly modules: readonly PluginModuleDeclaration[];
  readonly navigation: readonly PluginNavigationDeclaration[];
  readonly commands: readonly PluginCommandDeclaration[];
  readonly methods: ReadonlySet<string>;
  readonly events: ReadonlySet<string>;
  readonly focusTargets: readonly PluginFocusDeclaration[];
  readonly helpAsset: string | null;
  readonly assetsAvailable: boolean;
}

export interface PluginRecord {
  readonly pluginId: string;
  readonly state: PluginState;
  readonly hasTerminalEntryPoint: boolean;
  readonly hasDesktopEntryPoint: boolean;
  readonly error: string | null;
  readonly manifest: PluginManifest | null;
}

export interface PluginResources {
  readonly moduleUrl: string;
  readonly stylesheetUrls: readonly string[];
  readonly helpUrl: string | null;
}

export interface DesktopPluginModule {
  readonly mount: (
    container: HTMLElement,
    api: PluginUiApi,
  ) => PluginCleanup | Promise<PluginCleanup>;
}

export type PluginCleanup = () => void | Promise<void>;
export type PluginNotificationSeverity = "information" | "warning" | "error";

export interface PluginUiApi {
  readonly pluginId: string;
  readonly signal: AbortSignal;
  readonly invoke: (methodId: string, params: JsonObject) => Promise<JsonValue>;
  readonly on: (
    eventId: string,
    listener: (payload: Readonly<JsonObject>) => void,
  ) => () => void;
  readonly navigate: (navigationId: string) => void;
  readonly notify: (
    message: string,
    severity?: PluginNotificationSeverity,
  ) => void;
  readonly currentLocation: () => Readonly<JsonObject>;
  readonly bindFocusTarget: (
    targetId: string,
    element: HTMLElement,
  ) => () => void;
}

export function parsePluginRecords(
  values: readonly PluginDto[],
): readonly PluginRecord[] {
  const pluginIds = new Set<string>();
  return Object.freeze(
    values.map((value) => {
      if (!validId(value.plugin_id) || pluginIds.has(value.plugin_id))
        return invalidRecord(value, "Invalid or duplicate plugin id");
      pluginIds.add(value.plugin_id);
      if (!PLUGIN_STATES.has(value.state))
        return invalidRecord(value, "Unknown plugin state");
      if (
        (value.state === "started" && value.manifest === null) ||
        (value.state === "terminal_only" && value.has_desktop_entry_point)
      )
        return invalidRecord(value, "Inconsistent plugin state");
      try {
        return Object.freeze({
          pluginId: value.plugin_id,
          state: value.state as PluginState,
          hasTerminalEntryPoint: value.has_terminal_entry_point,
          hasDesktopEntryPoint: value.has_desktop_entry_point,
          error: value.error?.message ?? null,
          manifest: value.manifest ? parseManifest(value) : null,
        });
      } catch {
        return invalidRecord(value, "The plugin manifest is invalid");
      }
    }),
  );
}

export function resolvePluginResources(
  record: PluginRecord,
  moduleId: string,
  assets: readonly AssetDescriptor[],
): PluginResources {
  if (record.state !== "started" || !record.manifest?.assetsAvailable)
    throw new Error("Plugin resources are unavailable");
  const module = record.manifest.modules.find((item) => item.id === moduleId);
  if (!module?.entryAsset) throw new Error("Unknown plugin module");
  const resolve = (handle: string, kind: string): string => {
    const expectedPath = `/assets/${encodeURIComponent(handle)}`;
    const matches = assets.filter((asset) => {
      try {
        const url = new URL(asset.url);
        return (
          asset.source === "plugin" &&
          asset.plugin_id === record.pluginId &&
          asset.kind === kind &&
          url.protocol === "tongs:" &&
          url.host === "app" &&
          !url.search &&
          !url.hash &&
          url.pathname === expectedPath
        );
      } catch {
        return false;
      }
    });
    if (matches.length !== 1) throw new Error("Plugin resource is unavailable");
    return matches[0]!.url;
  };
  return Object.freeze({
    moduleUrl: resolve(module.entryAsset, "module"),
    stylesheetUrls: Object.freeze(
      module.stylesheetAssets.map((handle) => {
        if (!handle) throw new Error("Plugin stylesheet is unavailable");
        return resolve(handle, "stylesheet");
      }),
    ),
    helpUrl: record.manifest.helpAsset
      ? resolve(record.manifest.helpAsset, "help")
      : null,
  });
}

function parseManifest(value: PluginDto): PluginManifest {
  const source = value.manifest!;
  if (value.state === "started" && source.api_major !== 1)
    throw new Error("Unsupported plugin API");
  const modules = source.modules.map((item) =>
    Object.freeze({
      id: localId(item, "id"),
      title: text(item, "title"),
      entryAsset: nullableText(item, "entry_asset"),
      stylesheetAssets: Object.freeze(nullableTextArray(item, "stylesheets")),
    }),
  );
  unique(modules.map((item) => item.id));
  const moduleIds = new Set(modules.map((item) => item.id));
  const navigation = source.navigation.map((item) => {
    const moduleId = localId(item, "module_id");
    if (!moduleIds.has(moduleId)) throw new Error("Unknown module");
    return Object.freeze({
      id: localId(item, "id"),
      title: text(item, "title"),
      moduleId,
    });
  });
  unique(navigation.map((item) => item.id));
  const navigationIds = new Set(navigation.map((item) => item.id));
  const commands = source.commands.map((item) => {
    const navigationId = localId(item, "navigation_id");
    if (!navigationIds.has(navigationId)) throw new Error("Unknown navigation");
    return Object.freeze({
      id: localId(item, "id"),
      title: text(item, "title"),
      navigationId,
      helpText: optionalText(item, "help_text"),
    });
  });
  unique(commands.map((item) => item.id));
  const focusTargets = source.focus_targets.map((item) => {
    const moduleId = localId(item, "module_id");
    if (!moduleIds.has(moduleId)) throw new Error("Unknown focus module");
    return Object.freeze({
      id: localId(item, "id"),
      title: text(item, "title"),
      moduleId,
    });
  });
  unique(focusTargets.map((item) => item.id));
  const methods = new Set(source.methods.map(assertLocalId));
  const events = new Set(source.events.map(assertLocalId));
  if (
    methods.size !== source.methods.length ||
    events.size !== source.events.length
  )
    throw new Error("Duplicate declaration");
  if (value.state === "started") {
    if (
      !source.assets_available ||
      modules.some(
        (item) =>
          !item.entryAsset || item.stylesheetAssets.some((asset) => !asset),
      )
    )
      throw new Error("Missing started resources");
  }
  return Object.freeze({
    title: source.title,
    version: source.version,
    apiMajor: source.api_major,
    modules: Object.freeze(modules),
    navigation: Object.freeze(navigation),
    commands: Object.freeze(commands),
    methods,
    events,
    focusTargets: Object.freeze(focusTargets),
    helpAsset: source.help_asset,
    assetsAvailable: source.assets_available,
  });
}

function invalidRecord(value: PluginDto, error: string): PluginRecord {
  return Object.freeze({
    pluginId: value.plugin_id,
    state: "failed",
    hasTerminalEntryPoint: value.has_terminal_entry_point,
    hasDesktopEntryPoint: value.has_desktop_entry_point,
    error,
    manifest: null,
  });
}

function text(value: JsonObject, key: string): string {
  const item = value[key];
  if (typeof item !== "string" || !item || item.length > 2048)
    throw new Error("Invalid text");
  return item;
}

function optionalText(value: JsonObject, key: string): string {
  const item = value[key];
  if (typeof item !== "string" || item.length > 2048)
    throw new Error("Invalid text");
  return item;
}

function nullableText(value: JsonObject, key: string): string | null {
  const item = value[key];
  if (item === null) return null;
  if (typeof item !== "string" || !item || item.length > 2048)
    throw new Error("Invalid text");
  return item;
}

function localId(value: JsonObject, key: string): string {
  return assertLocalId(text(value, key));
}

function assertLocalId(value: string): string {
  if (!validId(value)) throw new Error("Invalid local id");
  return value;
}

function validId(value: string): boolean {
  return value.length <= 80 && LOCAL_ID.test(value);
}

function nullableTextArray(value: JsonObject, key: string): (string | null)[] {
  const item = value[key];
  if (!Array.isArray(item) || item.length > 64)
    throw new Error("Invalid array");
  return item.map((entry) => {
    if (entry === null) return null;
    if (typeof entry !== "string" || !entry) throw new Error("Invalid array");
    return entry;
  });
}

function unique(values: readonly string[]): void {
  if (new Set(values).size !== values.length) throw new Error("Duplicate id");
}
