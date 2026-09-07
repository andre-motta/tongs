import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import { getAvailableBridge, invoke, isBrowserDemoEnabled, waitForBridge } from "./bridge";
import type {
  Bridge,
  Diff,
  DiffLine,
  PluginApi,
  PluginMount,
  PluginModule,
  PluginRecord,
  Review,
} from "./types";
import { DIFF_ROW_HEIGHT, getDiffWindow } from "./virtualDiff";

type View = "inbox" | "plugins";
type DiffMode = "unified" | "split";
type PluginModuleExports = { mount?: PluginMount };
export type PluginLoader = (entryUrl: string) => Promise<PluginModuleExports>;

export const loadPluginEntry: PluginLoader = async (entryUrl) => (
  await import(/* @vite-ignore */ entryUrl)
) as PluginModuleExports;

export async function mountPluginModule(
  entryUrl: string,
  container: HTMLElement,
  api: PluginApi,
  loader: PluginLoader = loadPluginEntry,
): Promise<void | (() => void)> {
  const loaded = await loader(entryUrl);
  if (typeof loaded.mount !== "function") {
    throw new Error("Plugin module does not export mount(container, api)");
  }
  return loaded.mount(container, api);
}

export function App(): JSX.Element {
  const [bridge, setBridge] = useState<Bridge | undefined>(() => getAvailableBridge());
  const [connection, setConnection] = useState("connecting");
  const [reviews, setReviews] = useState<Review[]>([]);
  const [plugins, setPlugins] = useState<PluginRecord[]>([]);
  const [selectedReviewId, setSelectedReviewId] = useState("normal");
  const [selectedRepo, setSelectedRepo] = useState<string | undefined>();
  const [selectedPluginId, setSelectedPluginId] = useState<string | undefined>();
  const [view, setView] = useState<View>("inbox");
  const [theme, setTheme] = useState<"dark" | "light">("dark");
  const [health, setHealth] = useState<{ fixture: boolean; protocol: string }>();
  const [diff, setDiff] = useState<Diff>();
  const [diffLoading, setDiffLoading] = useState(false);
  const [error, setError] = useState<string>();

  useEffect(() => {
    let active = true;
    const load = async (nextBridge?: Bridge) => {
      setConnection(nextBridge ? "connected" : "browser demo");
      try {
        const [healthResult, reviewResult, pluginResult] = await Promise.all([
          invoke("health", undefined, nextBridge),
          invoke("list_reviews", undefined, nextBridge),
          invoke("list_plugins", undefined, nextBridge),
        ]);
        if (!active) return;
        setHealth(healthResult as { fixture: boolean; protocol: string });
        setReviews(reviewResult as Review[]);
        setPlugins(pluginResult as PluginRecord[]);
        setError(undefined);
      } catch (reason) {
        if (active) setError(reason instanceof Error ? reason.message : "Unable to load fixture");
      }
    };

    const cleanup = waitForBridge((ready) => {
      setBridge(ready);
      void load(ready);
    });
    const initialBridge = getAvailableBridge();
    if (initialBridge || isBrowserDemoEnabled()) void load(initialBridge);
    else setError("Waiting for the desktop bridge. Start a shell or use ?demo=1 for the browser demo.");
    return () => {
      active = false;
      cleanup();
    };
  }, []);

  const visibleReviews = useMemo(
    () => (selectedRepo ? reviews.filter((review) => review.repo === selectedRepo) : reviews),
    [reviews, selectedRepo],
  );
  const selectedReview =
    visibleReviews.find((review) => review.id === selectedReviewId) ?? visibleReviews[0] ?? reviews[0];
  const repositories = useMemo(
    () => Array.from(new Map(reviews.map((review) => [review.repo, review])).values()).map((review) => ({
      name: review.repo,
      forge: review.forge,
      count: reviews.filter((candidate) => candidate.repo === review.repo).length,
      color: review.forge === "GitHub" ? "violet" : "orange",
    })),
    [reviews],
  );

  useEffect(() => {
    if (!selectedReview) {
      setDiff(undefined);
      return;
    }
    let active = true;
    setDiffLoading(true);
    void invoke("get_diff", { id: selectedReview.id }, bridge)
      .then((result) => {
        if (active) setDiff(result as Diff);
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : "Unable to load diff");
      })
      .finally(() => {
        if (active) setDiffLoading(false);
      });
    return () => {
      active = false;
    };
  }, [bridge, selectedReview]);

  const selectReview = (review: Review) => {
    setSelectedReviewId(review.id);
    setView("inbox");
  };

  const selectRepo = (repo?: string) => {
    setSelectedRepo(repo);
    const first = repo ? reviews.find((review) => review.repo === repo) : reviews[0];
    if (first) setSelectedReviewId(first.id);
  };

  return (
    <div className="app-shell" data-theme={theme}>
      <header className="topbar">
        <div className="brand-lockup" aria-label="tongs desktop fixture">
          <div className="brand-mark">t</div>
          <div>
            <div className="brand-name">tongs <span>desktop</span></div>
            <div className="eyebrow">review workspace</div>
          </div>
        </div>
        <div className="topbar-context">
          <span className="fixture-pill"><span className="pulse-dot" /> PROTOTYPE FIXTURE</span>
          <span className="protocol-label">{health?.protocol ?? "prototype-1"}</span>
        </div>
        <div className="topbar-actions">
          <button className="icon-button" aria-label="Toggle light and dark theme" onClick={() => setTheme((current) => current === "dark" ? "light" : "dark")}>
            {theme === "dark" ? "☼" : "☾"}
          </button>
          <button className="avatar-button" aria-label="Profile menu not included in prototype" disabled>AL</button>
        </div>
      </header>

      <div className="workspace-grid">
        <aside className="sidebar" aria-label="Repository and navigation">
          <div className="sidebar-section nav-section">
            <div className="section-label">Workspace</div>
            <button className={`nav-item ${view === "inbox" && !selectedRepo ? "active" : ""}`} onClick={() => { setView("inbox"); selectRepo(); }}>
              <span className="nav-icon">◈</span><span>Review inbox</span><span className="nav-count">{reviews.length}</span>
            </button>
            <button className={`nav-item ${view === "plugins" ? "active" : ""}`} onClick={() => setView("plugins")}>
              <span className="nav-icon">✦</span><span>Plugins</span><span className="nav-count">{plugins.filter((plugin) => plugin.status === "ready").length}</span>
            </button>
          </div>
          <div className="sidebar-section repo-section">
            <div className="section-heading"><span className="section-label">Repositories</span><button className="quiet-button" aria-label="Add repository not included in prototype" disabled>＋</button></div>
            {repositories.map((repo) => (
              <button key={repo.name} className={`repo-item ${selectedRepo === repo.name ? "active" : ""}`} onClick={() => { selectRepo(repo.name); setView("inbox"); }}>
                <span className={`repo-glyph ${repo.color}`}>{repo.forge === "GitHub" ? "◉" : "◆"}</span>
                <span className="repo-copy"><strong>{repo.name}</strong><small>{repo.forge}</small></span>
                <span className="repo-count">{repo.count}</span>
              </button>
            ))}
          </div>
          <div className="sidebar-footer">
            <div className="connection-row"><span className={`connection-dot ${connection === "connected" ? "ready" : "fallback"}`} /><span>{connection}</span></div>
            <div className="sidebar-note">Synthetic data only<br />No forge calls are made.</div>
          </div>
        </aside>

        <main className="main-column">
          {error && <div className="error-banner" role="alert"><span>Bridge error</span> {error}<button onClick={() => setError(undefined)} aria-label="Dismiss error">×</button></div>}
          {view === "inbox" ? (
            <InboxView
              reviews={visibleReviews}
              selected={selectedReview}
              selectedRepo={selectedRepo}
              diff={diff}
              diffLoading={diffLoading}
              onSelect={selectReview}
            />
          ) : (
            <PluginsView
              bridge={bridge}
              plugins={plugins}
              selectedPluginId={selectedPluginId}
              onSelectPlugin={setSelectedPluginId}
            />
          )}
        </main>
      </div>
    </div>
  );
}

function InboxView({
  reviews,
  selected,
  selectedRepo,
  diff,
  diffLoading,
  onSelect,
}: {
  reviews: Review[];
  selected?: Review;
  selectedRepo?: string;
  diff?: Diff;
  diffLoading: boolean;
  onSelect: (review: Review) => void;
}): JSX.Element {
  return (
    <div className="inbox-layout">
      <section className="inbox-panel" aria-label="Review inbox">
        <div className="page-heading">
          <div><div className="eyebrow">{selectedRepo ? "Repository scope" : "Your workspace"}</div><h1>{selectedRepo ?? "Review inbox"}</h1></div>
          <button className="secondary-button" disabled title="Command palette is not included in this prototype">⌘ K <span>Commands</span></button>
        </div>
        <div className="filter-tabs" role="tablist" aria-label="Review filters">
          <button className="filter-tab active" role="tab" aria-selected="true">My reviews <span>{reviews.length}</span></button>
          <button className="filter-tab" role="tab" aria-selected="false" disabled>Assigned <span>0</span></button>
          <button className="filter-tab" role="tab" aria-selected="false" disabled>All open <span>2</span></button>
        </div>
        <div className="inbox-summary"><span>{reviews.length} review{reviews.length === 1 ? "" : "s"}</span><button className="sort-button" disabled title="Sorting is not included in this prototype">Updated <span>↓</span></button></div>
        <div className="review-list">
          {reviews.map((review) => <ReviewCard key={review.id} review={review} selected={review.id === selected?.id} onClick={() => onSelect(review)} />)}
          {!reviews.length && <div className="empty-state">No reviews in this repository scope.</div>}
        </div>
        <div className="inbox-hint"><span className="keycap">Tab</span> focus review <span className="keycap">↵</span> open review</div>
      </section>
      <section className="detail-panel" aria-label="Review detail">
        {selected ? <ReviewDetail review={selected} diff={diff} diffLoading={diffLoading} /> : <div className="empty-detail">Select a review to start</div>}
      </section>
    </div>
  );
}

function ReviewCard({ review, selected, onClick }: { review: Review; selected: boolean; onClick: () => void }): JSX.Element {
  return (
    <button className={`review-card ${selected ? "selected" : ""}`} onClick={onClick} aria-pressed={selected}>
      <div className="review-card-top"><span className={`forge-label ${review.forge === "GitHub" ? "github" : "gitlab"}`}>{review.forge === "GitHub" ? "GH" : "GL"}</span><span className={`status-label ${review.status.toLowerCase()}`}><span />{review.status}</span></div>
      <div className="review-title">{review.title}</div>
      <div className="review-card-meta"><span>#{review.number}</span><span className="meta-separator">·</span><span>{review.repo}</span></div>
      <div className="review-card-bottom"><span className="author-avatar">{review.author.slice(0, 1).toUpperCase()}</span><span>{review.author}</span><span className="updated">2h ago</span></div>
    </button>
  );
}

function ReviewDetail({ review, diff, diffLoading }: { review: Review; diff?: Diff; diffLoading: boolean }): JSX.Element {
  const [mode, setMode] = useState<DiffMode>("unified");
  const [tab, setTab] = useState("Diff");
  return (
    <div className="review-detail">
      <div className="detail-heading">
        <div className="detail-title-wrap"><div className="eyebrow">{review.forge} pull request <span className="title-slash">/</span> #{review.number}</div><h2>{review.title}</h2><div className="detail-subline"><span className="author-avatar small">{review.author.slice(0, 1).toUpperCase()}</span> <strong>{review.author}</strong> opened this review in <strong>{review.repo}</strong></div></div>
        <button className="approve-button" disabled title="Review mutations are not included in this prototype">✓ Approve review</button>
      </div>
      <div className="detail-tabs" role="tablist">
        {["Overview", "Diff", "Discussion", "CI"].map((item) => <button key={item} className={tab === item ? "active" : ""} role="tab" aria-selected={tab === item} onClick={() => setTab(item)} disabled={item !== "Diff"} title={item === "Diff" ? "" : `${item} is not included in this prototype`}>{item}{item === "Discussion" && <span className="tab-badge">3</span>}</button>)}
      </div>
      {tab === "Diff" ? (
        <DiffViewer diff={diff} loading={diffLoading} mode={mode} onModeChange={setMode} />
      ) : (
        <div className="placeholder-panel"><div className="placeholder-icon">{tab === "Overview" ? "◌" : tab === "Discussion" ? "◍" : "◒"}</div><h3>{tab} is ready for the next prototype slice</h3><p>This comparison fixture focuses on the review inbox and a responsive diff workspace.</p></div>
      )}
    </div>
  );
}

function DiffViewer({ diff, loading, mode, onModeChange }: { diff?: Diff; loading: boolean; mode: DiffMode; onModeChange: (mode: DiffMode) => void }): JSX.Element {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewportHeight, setViewportHeight] = useState(460);
  useLayoutEffect(() => {
    const node = scrollRef.current;
    if (!node) return;
    const measure = () => setViewportHeight(node.clientHeight || 460);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    setScrollTop(0);
    scrollRef.current?.scrollTo({ top: 0 });
  }, [diff?.path, diff?.lines.length, loading]);

  const window = getDiffWindow(diff?.lines.length ?? 0, scrollTop, viewportHeight);
  const renderedLines = diff?.lines.slice(window.start, window.end) ?? [];
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "j" && event.key !== "k") return;
    event.preventDefault();
    const amount = event.key === "j" ? DIFF_ROW_HEIGHT : -DIFF_ROW_HEIGHT;
    scrollRef.current?.scrollBy({ top: amount, behavior: "smooth" });
  };
  return (
    <div className="diff-workspace">
      <div className="diff-toolbar"><div className="file-breadcrumb"><span className="file-icon">◫</span><strong>{diff?.path ?? "Loading workspace.py"}</strong><span className="file-status">M</span></div><div className="diff-toolbar-right"><span className="diff-stat">{diff ? `${diff.lines.length.toLocaleString()} lines` : "Loading..."} <span className="green-text">+{diff ? diff.lines.filter((line) => line.kind === "addition").length : 0}</span> <span className="red-text">-{diff ? diff.lines.filter((line) => line.kind === "deletion").length : 0}</span></span><div className="view-toggle" role="group" aria-label="Diff layout"><button className={mode === "unified" ? "active" : ""} onClick={() => onModeChange("unified")} aria-label="Unified diff view">▤ Unified</button><button className={mode === "split" ? "active" : ""} onClick={() => onModeChange("split")} aria-label="Side-by-side diff view">▥ Split</button></div></div></div>
      <div className="diff-info"><span className="diff-info-dot" /> Virtualized diff <span className="info-divider" /> {renderedLines.length} rows rendered of {diff?.lines.length.toLocaleString() ?? "0"} <span className="info-divider" /> Press <kbd>j</kbd>/<kbd>k</kbd> to navigate</div>
      <div className={`diff-scroll ${mode === "split" ? "split-mode" : ""}`} ref={scrollRef} onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)} onKeyDown={onKeyDown} tabIndex={0} aria-label="Code diff, keyboard navigable">
        {loading ? <div className="diff-loading"><span className="spinner" /> Loading diff fixture...</div> : diff ? <div className="diff-canvas" style={{ height: window.height }}><div className="diff-window" style={{ transform: `translateY(${window.top}px)` }}>{renderedLines.map((line, index) => <DiffRow key={`${window.start + index}-${line.old_line}-${line.new_line}`} line={line} mode={mode} />)}</div></div> : <div className="diff-loading">No diff loaded.</div>}
      </div>
    </div>
  );
}

function DiffRow({ line, mode }: { line: DiffLine; mode: DiffMode }): JSX.Element {
  const marker = line.kind === "addition" ? "+" : line.kind === "deletion" ? "−" : "·";
  const classes = `diff-row ${line.kind}`;
  if (mode === "split") {
    return <div className={`${classes} split-row`} style={{ height: DIFF_ROW_HEIGHT }}><DiffCell lineNumber={line.old_line} marker={line.kind === "addition" ? "" : line.kind === "deletion" ? "−" : "·"} text={line.kind === "addition" ? "" : line.text} /><DiffCell lineNumber={line.new_line} marker={line.kind === "deletion" ? "" : line.kind === "addition" ? "+" : "·"} text={line.kind === "deletion" ? "" : line.text} /></div>;
  }
  return <div className={classes} style={{ height: DIFF_ROW_HEIGHT }}><span className="diff-number">{line.old_line ?? ""}</span><span className="diff-number">{line.new_line ?? ""}</span><span className="diff-marker">{marker}</span><code>{line.text}</code></div>;
}

function DiffCell({ lineNumber, marker, text }: { lineNumber: number | null; marker: string; text: string }): JSX.Element {
  return <div className="diff-cell"><span className="diff-number">{lineNumber ?? ""}</span><span className="diff-marker">{marker}</span><code>{text}</code></div>;
}

function PluginsView({ bridge, plugins, selectedPluginId, onSelectPlugin }: { bridge?: Bridge; plugins: PluginRecord[]; selectedPluginId?: string; onSelectPlugin: (id: string) => void }): JSX.Element {
  const selected = plugins.find((plugin) => plugin.id === selectedPluginId) ?? plugins.find((plugin) => plugin.status === "ready");
  const [module, setModule] = useState<PluginModule>();
  useEffect(() => {
    setModule(selected?.modules[0]);
    if (selected && selected.id !== selectedPluginId) onSelectPlugin(selected.id);
  }, [selected, selectedPluginId, onSelectPlugin]);
  return (
    <div className="plugins-layout">
      <div className="page-heading plugin-heading"><div><div className="eyebrow">Extensible workspace</div><h1>Plugins</h1><p className="page-subtitle">Installed contributions are discovered through the backend contract.</p></div><span className="fixture-tag">FIXTURE PLUGINS</span></div>
      <div className="plugins-grid">
        <section className="plugin-list-panel"><div className="panel-label">Installed modules <span>{plugins.length}</span></div>{plugins.length ? plugins.map((plugin) => <PluginCard key={plugin.id} plugin={plugin} selected={plugin.id === selected?.id} onClick={() => onSelectPlugin(plugin.id)} />) : <div className="empty-plugins"><div className="empty-plugin-icon">✦</div><strong>No desktop modules detected</strong><p>Install an opted-in plugin to see its module here. Terminal-only plugins remain available to the TUI.</p></div>}</section>
        <section className="plugin-detail-panel">{selected && module ? <PluginModuleView key={`${selected.id}:${module.id}`} bridge={bridge} plugin={selected} module={module} /> : <div className="plugin-welcome"><div className="placeholder-icon">✦</div><h2>Plugin surface</h2><p>Desktop modules load from the installed plugin metadata at runtime. Nothing in core is coupled to a plugin package.</p><div className="plugin-contract-note"><span>API</span> dynamic module + scoped backend invoke</div></div>}</section>
      </div>
    </div>
  );
}

function PluginCard({ plugin, selected, onClick }: { plugin: PluginRecord; selected: boolean; onClick: () => void }): JSX.Element {
  const statusText = plugin.status === "terminal_only" ? "TUI only" : plugin.status;
  return <button className={`plugin-card ${selected ? "selected" : ""}`} onClick={onClick}><div className="plugin-card-icon">✦</div><div className="plugin-card-copy"><strong>{plugin.title}</strong><small>{plugin.modules.length ? `${plugin.modules.length} desktop module${plugin.modules.length === 1 ? "" : "s"}` : "No desktop modules"}</small></div><span className={`plugin-status ${plugin.status}`}><span />{statusText}</span></button>;
}

function PluginModuleView({ bridge, plugin, module }: { bridge?: Bridge; plugin: PluginRecord; module: PluginModule }): JSX.Element {
  const containerRef = useRef<HTMLDivElement>(null);
  const [mountStatus, setMountStatus] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string>();
  const [help, setHelp] = useState<string>();
  useEffect(() => {
    let active = true;
    let cleanup: void | (() => void);
    const mount = async () => {
      setMountStatus("loading");
      setError(undefined);
      try {
        const container = containerRef.current;
        if (!container) throw new Error("Plugin module container is unavailable");
        const api: PluginApi = { invoke: (method, params = {}) => invoke("plugin_invoke", { plugin: plugin.id, method, params }, bridge) };
        const mountedCleanup = await mountPluginModule(module.entry_url, container, api);
        if (!active) {
          mountedCleanup?.();
          return;
        }
        cleanup = mountedCleanup;
        setMountStatus("ready");
      } catch (reason) {
        if (active) {
          setMountStatus("error");
          setError(reason instanceof Error ? reason.message : "Unable to mount plugin module");
        }
      }
    };
    void mount();
    return () => {
      active = false;
      cleanup?.();
    };
  }, [bridge, module.entry_url, plugin.id]);

  const loadHelp = async () => {
    try {
      const result = await invoke("plugin_help", { plugin: plugin.id, module: module.id }, bridge);
      setHelp(String(result));
    } catch (reason) {
      setHelp(reason instanceof Error ? reason.message : "Help unavailable");
    }
  };
  const statusMessage = mountStatus === "ready" ? "Loaded dynamically from installed assets" : mountStatus === "loading" ? "Loading installed module" : "Module failed to mount";
  return <div className="module-workspace"><div className="module-heading"><div><div className="eyebrow">Desktop module</div><h2>{module.title}</h2><p>{plugin.title} <span className="title-slash">/</span> {module.id}</p></div><button className="secondary-button" onClick={() => void loadHelp()}>? Help</button></div><div className="module-runtime-note"><span className={`module-state ${mountStatus}`}>● {mountStatus}</span><span>{statusMessage}</span><span className="info-divider" /><span>Scoped API bridge</span></div><div className="module-surface">{mountStatus === "loading" && <div className="module-loading"><span className="spinner" /> Loading module...</div>}<div ref={containerRef} className="plugin-mount" />{error && <div className="module-error" role="alert">{error}</div>}</div>{help && <div className="help-drawer"><div className="help-title"><strong>Module help</strong><button onClick={() => setHelp(undefined)} aria-label="Close module help">×</button></div><pre>{help}</pre></div>}</div>;
}
