import {
  Component,
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ErrorInfo,
  type ReactNode,
} from "react";
import { createRoot } from "react-dom/client";
import type { RepositoryDto } from "../shared/bridge.js";
import {
  FeatureRegistry,
  Navigator,
  type AppRoute,
  type FeatureContext,
  type InlineAnchorSelection,
} from "./core/navigation.js";
import { QueryCoordinator } from "./core/query.js";
import { createDiffFeature } from "./features/diff/index.js";
import { createInboxFeature } from "./features/inbox/index.js";
import { createPipelinesFeature } from "./features/pipelines/index.js";
import { createReviewFeature } from "./features/review/index.js";
import { RepositoryNavigation } from "./features/repositories/index.js";
import {
  createCommitsFeature,
  createReviewOverviewFeature,
} from "./features/review-detail/index.js";

const bridge = window.tongs;
const navigator = new Navigator();
const queries = new QueryCoordinator(bridge);
const shellQueries = new QueryCoordinator(bridge);
const registry = new FeatureRegistry();
registry.register(createInboxFeature());
registry.register(createReviewOverviewFeature());
registry.register(createCommitsFeature());
registry.register(createDiffFeature());
registry.register(createPipelinesFeature());
registry.register(createReviewFeature(bridge));

function App(): ReactNode {
  const [route, setRoute] = useState<AppRoute>(navigator.route);
  const [repositories, setRepositories] = useState<readonly RepositoryDto[]>(
    [],
  );
  const [repositoriesReady, setRepositoriesReady] = useState(false);
  const [repositoryGeneration, setRepositoryGeneration] = useState(0);
  const [inlineAnchor, setInlineAnchor] =
    useState<InlineAnchorSelection | null>(null);
  const [serviceStatus, setServiceStatus] = useState(
    "Connecting to the local service…",
  );
  const [serviceClass, setServiceClass] = useState("service-status");
  useEffect(() => navigator.subscribe(setRoute), []);
  useEffect(
    () =>
      bridge.onEvent((event) => {
        if (event.name === "service.changed" && isRepositoryChange(event.data))
          return;
        if (
          event.name === "protocol.resync_required" ||
          event.name === "service.changed"
        ) {
          setServiceStatus("Updates are available. Refresh the current view.");
          setServiceClass("service-status service-status-warning");
        }
      }),
    [],
  );
  useEffect(
    () => () => {
      void queries.cancelAll();
      void shellQueries.cancelAll();
    },
    [],
  );
  useEffect(() => {
    void publishNativeProbe(setServiceStatus, setServiceClass);
  }, []);
  const navigation = useCallback((next: AppRoute) => {
    setInlineAnchor((current) =>
      next.kind === "review" && current?.review === next.item.handle
        ? current
        : null,
    );
    navigator.navigate(next);
  }, []);
  const onDiscovery = useCallback((next: readonly RepositoryDto[]) => {
    setRepositories(next);
    setRepositoriesReady(true);
    setRepositoryGeneration((current) => current + 1);
  }, []);
  const feature = useMemo(() => registry.find(route), [route]);
  const selected = route.kind === "inbox" ? route.repository : null;
  const featureContext: FeatureContext = {
    bridge,
    queries,
    repositories,
    repositoriesReady,
    repositoryGeneration,
    reviewPanels: registry.reviewPanels(),
    inlineAnchor,
    selectInlineAnchor: setInlineAnchor,
    navigate: navigation,
  };
  const commands = registry.commands(featureContext, route);
  return (
    <>
      <header className="app-bar">
        <div className="brand">
          <img src="/icon.png" alt="" width="32" height="32" />
          <strong>Tongs</strong>
        </div>
        <div className="app-actions">
          {commands.map((command) => {
            const disabledReason = command.disabledReason(
              featureContext,
              route,
            );
            return (
              <button
                key={command.id}
                className="button button-secondary"
                disabled={disabledReason !== null}
                title={disabledReason ?? undefined}
                onClick={() => void command.run(featureContext, route)}
              >
                {command.label}
              </button>
            );
          })}
          <p id="service-status" className={serviceClass}>
            {serviceStatus}
          </p>
        </div>
      </header>
      <div className="app-layout">
        <RepositoryNavigation
          bridge={bridge}
          queries={shellQueries}
          selected={selected}
          navigate={navigation}
          onDiscovery={onDiscovery}
        />
        <main id="content" className="content" tabIndex={-1}>
          <ErrorBoundary
            key={`${route.kind}:${route.kind === "review" ? `${route.item.handle}:${route.panel}` : (route.repository?.handle ?? "all")}`}
          >
            {feature.render(featureContext, route)}
          </ErrorBoundary>
        </main>
      </div>
    </>
  );
}

function isRepositoryChange(value: unknown): boolean {
  return (
    value !== null &&
    typeof value === "object" &&
    "kind" in value &&
    value.kind === "repositories_changed"
  );
}

class ErrorBoundary extends Component<
  { readonly children: ReactNode },
  { readonly failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError(): { readonly failed: boolean } {
    return { failed: true };
  }
  componentDidCatch(_error: Error, _info: ErrorInfo): void {}
  render(): ReactNode {
    return this.state.failed ? (
      <>
        <h1 className="view-title">This view could not be displayed</h1>
        <div className="notice notice-error" role="alert">
          The local view encountered an error. Choose another repository or
          review to continue.
        </div>
      </>
    ) : (
      this.props.children
    );
  }
}

async function publishNativeProbe(
  setStatus: (value: string) => void,
  setClass: (value: string) => void,
): Promise<void> {
  try {
    const assets = await bridge.listAssets().result;
    const canvas = document.createElement("canvas");
    const gl = canvas.getContext("webgl");
    const debug = gl?.getExtension("WEBGL_debug_renderer_info");
    const probe = {
      assets: assets.length,
      processGlobal: typeof globalThis.process,
      requireGlobal: typeof globalThis.require,
      webglVendor:
        gl && debug ? gl.getParameter(debug.UNMASKED_VENDOR_WEBGL) : null,
      webglRenderer:
        gl && debug ? gl.getParameter(debug.UNMASKED_RENDERER_WEBGL) : null,
    };
    setStatus("Local service connected");
    setClass("service-status service-status-ready");
    await new Promise((resolve) =>
      requestAnimationFrame(() => requestAnimationFrame(resolve)),
    );
    document.documentElement.dataset.tongsProbe = JSON.stringify(probe);
  } catch {
    setStatus("Local service unavailable");
    setClass("service-status service-status-error");
  }
}

const root = document.querySelector<HTMLElement>("#app");
if (!root) throw new Error("Missing React application root");
createRoot(root).render(<App />);
