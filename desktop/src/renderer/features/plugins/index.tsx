import {
  createElement,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import type { DesktopBridge } from "../../../shared/bridge.js";
import { parseInertMarkdown } from "../../core/markdown.js";
import type {
  AppRoute,
  CommandContribution,
  FeatureContext,
  FeatureContribution,
} from "../../core/navigation.js";
import {
  PluginRuntime,
  type PluginCatalogSnapshot,
  type PluginMountStatus,
  type PluginRuntimeDependencies,
} from "./runtime.js";
import type { PluginRecord } from "./types.js";

export interface PluginsFeature extends FeatureContribution {
  readonly runtime: PluginRuntime;
  readonly commandsFor: (
    snapshot: PluginCatalogSnapshot,
  ) => readonly CommandContribution[];
  readonly navigation: (
    snapshot: PluginCatalogSnapshot,
    route: AppRoute,
    navigate: (route: AppRoute) => void,
  ) => ReactNode;
}

export function createPluginsFeature(
  bridge: DesktopBridge,
  dependencies?: PluginRuntimeDependencies,
): PluginsFeature {
  const runtime = new PluginRuntime(bridge, dependencies);
  return Object.freeze({
    id: "plugins",
    order: 50,
    runtime,
    matches: (route: AppRoute): boolean => route.kind === "plugin",
    render: (context: FeatureContext, route: AppRoute): ReactNode => {
      if (route.kind !== "plugin") throw new Error("Invalid plugin route");
      return (
        <PluginWorkspace context={context} route={route} runtime={runtime} />
      );
    },
    commandsFor: (snapshot: PluginCatalogSnapshot) => pluginCommands(snapshot),
    navigation: (
      snapshot: PluginCatalogSnapshot,
      route: AppRoute,
      navigate: (route: AppRoute) => void,
    ) => (
      <PluginNavigation
        snapshot={snapshot}
        route={route}
        navigate={navigate}
        refresh={() => void runtime.refresh()}
        dismiss={(id) => runtime.dismissNotice(id)}
      />
    ),
  });
}

export function usePluginSnapshot(
  runtime: PluginRuntime,
): PluginCatalogSnapshot {
  return useSyncExternalStore(
    (listener) => runtime.subscribe(listener),
    () => runtime.snapshot,
  );
}

function pluginCommands(
  snapshot: PluginCatalogSnapshot,
): readonly CommandContribution[] {
  const commands: CommandContribution[] = [];
  for (const record of snapshot.records) {
    if (record.state !== "started" || !record.manifest) continue;
    for (const command of record.manifest.commands) {
      const navigation = record.manifest.navigation.find(
        (item) => item.id === command.navigationId,
      );
      if (!navigation) continue;
      commands.push(
        Object.freeze({
          id: `plugin:${record.pluginId}:command:${command.id}`,
          label: command.title,
          order: 1_000,
          isVisible: (): boolean => true,
          disabledReason: (): string | null =>
            record.state === "started" ? null : "Plugin unavailable",
          run: (context: FeatureContext): void =>
            context.navigate({
              kind: "plugin",
              pluginId: record.pluginId,
              navigationId: navigation.id,
              moduleId: navigation.moduleId,
            }),
        }),
      );
    }
  }
  return Object.freeze(commands);
}

function PluginNavigation({
  snapshot,
  route,
  navigate,
  refresh,
  dismiss,
}: {
  readonly snapshot: PluginCatalogSnapshot;
  readonly route: AppRoute;
  readonly navigate: (route: AppRoute) => void;
  readonly refresh: () => void;
  readonly dismiss: (id: number) => void;
}): ReactNode {
  return (
    <section
      className="plugin-navigation"
      aria-labelledby="plugin-navigation-title"
    >
      <div className="plugin-navigation-heading">
        <h2 id="plugin-navigation-title" className="sidebar-title">
          Plugins
        </h2>
        <button
          className="button button-quiet plugin-refresh"
          disabled={snapshot.loading}
          onClick={refresh}
          aria-label="Refresh installed plugins"
          title="Refresh installed plugins"
        >
          ↻
        </button>
      </div>
      {snapshot.loading && snapshot.records.length === 0 && (
        <p className="plugin-state">Loading installed plugins…</p>
      )}
      {snapshot.error && (
        <div className="notice notice-error plugin-notice" role="alert">
          {snapshot.error}
        </div>
      )}
      {snapshot.records.map((record) => (
        <PluginNavigationRecord
          key={record.pluginId}
          record={record}
          route={route}
          navigate={navigate}
        />
      ))}
      {!snapshot.loading &&
        !snapshot.error &&
        snapshot.records.length === 0 && (
          <p className="plugin-state">No desktop plugins installed.</p>
        )}
      {snapshot.notices.map((notice) => (
        <div
          key={notice.id}
          className={`plugin-toast plugin-toast-${notice.severity}`}
          role={notice.severity === "error" ? "alert" : "status"}
        >
          <span>{notice.message}</span>
          <button
            className="button button-quiet"
            onClick={() => dismiss(notice.id)}
            aria-label="Dismiss plugin notification"
          >
            ×
          </button>
        </div>
      ))}
    </section>
  );
}

function PluginNavigationRecord({
  record,
  route,
  navigate,
}: {
  readonly record: PluginRecord;
  readonly route: AppRoute;
  readonly navigate: (route: AppRoute) => void;
}): ReactNode {
  const title = record.manifest?.title ?? record.pluginId;
  const state = statePresentation(record);
  return (
    <div className="plugin-navigation-record" data-plugin-state={record.state}>
      <p className="plugin-name">{title}</p>
      {record.state === "started" && record.manifest ? (
        record.manifest.navigation.length ? (
          record.manifest.navigation.map((navigation) => (
            <button
              key={`${record.pluginId}:${navigation.id}`}
              className={`nav-item ${
                route.kind === "plugin" &&
                route.pluginId === record.pluginId &&
                route.navigationId === navigation.id
                  ? "nav-item-active"
                  : ""
              }`}
              onClick={() =>
                navigate({
                  kind: "plugin",
                  pluginId: record.pluginId,
                  navigationId: navigation.id,
                  moduleId: navigation.moduleId,
                })
              }
            >
              {navigation.title}
            </button>
          ))
        ) : (
          <p className="plugin-state">No desktop views declared.</p>
        )
      ) : (
        <p
          className={`plugin-state plugin-state-${record.state}`}
          title={record.error ?? undefined}
        >
          {state}
        </p>
      )}
    </div>
  );
}

function PluginWorkspace({
  context,
  route,
  runtime,
}: {
  readonly context: FeatureContext;
  readonly route: Extract<AppRoute, { readonly kind: "plugin" }>;
  readonly runtime: PluginRuntime;
}): ReactNode {
  const snapshot = usePluginSnapshot(runtime);
  const container = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState<PluginMountStatus>({
    loading: true,
    error: null,
  });
  const [help, setHelp] = useState<string | null>(null);
  const [helpError, setHelpError] = useState(false);
  const record = runtime.record(route.pluginId);
  const module = record?.manifest?.modules.find(
    (item) => item.id === route.moduleId,
  );

  useEffect(() => {
    const element = container.current;
    if (!element) return;
    setStatus({ loading: true, error: null });
    void runtime.mount(route, element, context.navigate, setStatus);
    return () => void runtime.unmount();
  }, [context.navigate, route, runtime, snapshot.generation]);

  useEffect(() => {
    const controller = new AbortController();
    setHelp(null);
    setHelpError(false);
    void runtime.loadHelp(route.pluginId, controller.signal).then(
      (source) => {
        if (!controller.signal.aborted) setHelp(source);
      },
      () => {
        if (!controller.signal.aborted) setHelpError(true);
      },
    );
    return () => controller.abort();
  }, [route.pluginId, runtime, snapshot.generation]);

  const helpBlocks = useMemo(
    () => (help === null ? [] : parseInertMarkdown(help)),
    [help],
  );
  return (
    <section className="plugin-workspace" data-plugin-id={route.pluginId}>
      <header className="view-header">
        <div>
          <p className="eyebrow">Installed plugin</p>
          <h1 className="view-title">
            {module?.title ?? record?.manifest?.title ?? route.pluginId}
          </h1>
          {record?.manifest && (
            <p className="plugin-version">
              {record.manifest.title} {record.manifest.version}
            </p>
          )}
        </div>
        <button
          className="button button-secondary"
          disabled={snapshot.loading}
          onClick={() => void runtime.refresh()}
        >
          Reload plugin
        </button>
      </header>
      {status.loading && (
        <div className="notice notice-loading" role="status">
          Loading plugin module…
        </div>
      )}
      {status.error && (
        <div className="notice notice-error" role="alert">
          {status.error}
        </div>
      )}
      <div
        ref={container}
        className="plugin-module"
        data-plugin-module={route.moduleId}
        aria-busy={status.loading}
      />
      {(help !== null || helpError) && (
        <details className="plugin-help panel">
          <summary>Plugin help</summary>
          {helpError ? (
            <div className="notice notice-error" role="alert">
              Plugin help is unavailable.
            </div>
          ) : (
            <div className="plugin-help-content">
              {helpBlocks.map((block, index) => {
                if (block.kind === "heading")
                  return createElement(
                    `h${Math.min(block.level + 1, 6)}`,
                    { key: index },
                    block.text,
                  );
                if (block.kind === "code")
                  return <pre key={index}>{block.text}</pre>;
                if (block.kind === "list")
                  return <li key={index}>{block.text}</li>;
                return <p key={index}>{block.text}</p>;
              })}
            </div>
          )}
        </details>
      )}
    </section>
  );
}

function statePresentation(record: PluginRecord): string {
  if (record.error) return record.error;
  if (record.state === "terminal_only")
    return "Available in the terminal only.";
  if (record.state === "disabled") return "Disabled in configuration.";
  if (record.state === "incompatible")
    return "Incompatible with this desktop version.";
  if (record.state === "failed") return "Plugin failed to start.";
  if (record.state === "stopped") return "Plugin stopped.";
  return "Plugin is starting.";
}

export type {
  DesktopPluginModule,
  PluginCleanup,
  PluginNotificationSeverity,
  PluginUiApi,
} from "./types.js";
