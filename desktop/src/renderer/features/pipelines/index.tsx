import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import type {
  DesktopBridge,
  JobItemDto,
  LogPage,
  PipelineItemDto,
} from "../../../shared/bridge.js";
import type {
  CICapabilityFlags,
  CIMutationAction,
  CIMutationReceipt,
  CIReceiptParams,
  JobMutationParams,
  PipelineMutationParams,
} from "../../../shared/ci.js";
import type {
  AppRoute,
  FeatureContribution,
  ReviewPanelContribution,
} from "../../core/navigation.js";
import {
  formatDate,
  RendererReadError,
  safeError,
  serviceErrorOf,
} from "../../core/presentation.js";
import { QueryCoordinator, StaleQueryError } from "../../core/query.js";
import { useRetainedRead } from "../../core/use-read.js";
import { ReviewHeader } from "../review-detail/index.js";

const LOG_PAGE_SIZE = 1000;
const LOG_WINDOW_SIZE = 300;
const MAX_LOG_PAGES = 512;
const MAX_LOG_ROWS = 50_000;
const MAX_LOG_CHARACTERS = 8 * 1024 * 1024;

interface LoadedLog {
  readonly snapshotId: string;
  readonly resource: string;
  readonly revision: LogPage["revision"];
  readonly lines: readonly string[];
  readonly partialError: RendererReadError | null;
}

interface MutationIntent {
  readonly operationId: string;
  readonly action: CIMutationAction;
  readonly pipeline: PipelineItemDto;
  readonly job: JobItemDto | null;
}

type MutationState =
  | { readonly kind: "idle" }
  | { readonly kind: "confirm"; readonly intent: MutationIntent }
  | { readonly kind: "submitting"; readonly intent: MutationIntent }
  | {
      readonly kind: "receipt";
      readonly intent: MutationIntent;
      readonly receipt: CIMutationReceipt;
    }
  | {
      readonly kind: "failed";
      readonly intent: MutationIntent;
      readonly error: unknown;
      readonly uncertain: boolean;
      readonly absentReceipt: boolean;
    };

export function createPipelinesFeature(): FeatureContribution {
  return {
    id: "review.pipelines",
    order: 23,
    reviewPanel: { id: "pipelines", label: "Pipelines", order: 40 },
    matches: (route) => route.kind === "review" && route.panel === "pipelines",
    render: (context, route) =>
      route.kind === "review" && route.panel === "pipelines" ? (
        <PipelinesView
          bridge={context.bridge}
          queries={context.queries}
          route={route}
          navigate={context.navigate}
          panels={context.reviewPanels}
        />
      ) : null,
  };
}

function PipelinesView({
  bridge,
  queries,
  route,
  navigate,
  panels,
}: {
  readonly bridge: DesktopBridge;
  readonly queries: QueryCoordinator;
  readonly route: Extract<AppRoute, { kind: "review" }>;
  readonly navigate: (route: AppRoute) => void;
  readonly panels: readonly ReviewPanelContribution[];
}): ReactNode {
  const pipelinesBegin = useCallback(
    () => bridge.listReviewPipelines({ review: route.item.handle, per_page: 100 }),
    [bridge, route.item.handle],
  );
  const pipelines = useRetainedRead(
    queries,
    `ci-pipelines:${route.item.handle}`,
    pipelinesBegin,
    [route.item.handle],
  );
  const capabilitiesBegin = useCallback(
    () => bridge.getCICapabilities(route.item.repository),
    [bridge, route.item.repository],
  );
  const capabilities = useRetainedRead(
    queries,
    `ci-capabilities:${route.item.repository}`,
    capabilitiesBegin,
    [route.item.repository],
  );
  const [selectedPipeline, setSelectedPipeline] = useState<string | null>(null);
  const [selectedJob, setSelectedJob] = useState<JobItemDto | null>(null);
  const [refreshEpoch, setRefreshEpoch] = useState(0);
  const [eventNotice, setEventNotice] = useState<string | null>(null);
  const [forgeNotice, setForgeNotice] = useState<{
    readonly kind: "status" | "error";
    readonly message: string;
  } | null>(null);
  const [mutation, setMutation] = useState<MutationState>({ kind: "idle" });
  const inFlightOperation = useRef<string | null>(null);
  const pipelineItems = pipelines.value?.pipelines ?? [];
  const selected =
    pipelineItems.find((item) => item.handle === selectedPipeline) ?? null;

  useEffect(() => {
    setSelectedPipeline((current) =>
      pipelineItems.some((item) => item.handle === current)
        ? current
        : (pipelineItems[0]?.handle ?? null),
    );
  }, [pipelineItems]);

  const refresh = useCallback(() => {
    pipelines.refresh();
    capabilities.refresh();
    setRefreshEpoch((current) => current + 1);
  }, [capabilities.refresh, pipelines.refresh]);

  useEffect(
    () =>
      bridge.onEvent((event) => {
        if (!isPipelineRefreshEvent(event.name, event.data, selectedPipeline))
          return;
        setEventNotice(
          event.name === "protocol.resync_required" ||
            isServiceResync(event.name, event.data)
            ? "Shared cached data changed. Pipeline data was reloaded."
            : "Pipeline status changed. The current view was reloaded.",
        );
        refresh();
      }),
    [bridge, refresh, selectedPipeline],
  );

  useEffect(() => {
    if (mutation.kind !== "confirm") return;
    if (
      mutation.intent.pipeline.handle !== selectedPipeline ||
      (mutation.intent.job !== null &&
        mutation.intent.job.handle !== selectedJob?.handle)
    ) {
      setMutation({ kind: "idle" });
    }
  }, [mutation, selectedJob?.handle, selectedPipeline]);

  const beginMutation = useCallback(
    (
      action: CIMutationAction,
      pipeline: PipelineItemDto,
      job: JobItemDto | null,
    ) => {
      setMutation({
        kind: "confirm",
        intent: {
          operationId: createOperationId(action),
          action,
          pipeline,
          job,
        },
      });
    },
    [],
  );

  const submitMutation = useCallback(
    async (intent: MutationIntent): Promise<void> => {
      if (inFlightOperation.current !== null) return;
      inFlightOperation.current = intent.operationId;
      setMutation({ kind: "submitting", intent });
      try {
        const receipt = await executeMutation(bridge, intent);
        requireReceiptBinding(receipt, intent);
        setMutation({ kind: "receipt", intent, receipt });
        refresh();
      } catch (error) {
        if (error instanceof StaleQueryError) return;
        setMutation({
          kind: "failed",
          intent,
          error,
          uncertain: classifyMutationFailure(error) === "unknown",
          absentReceipt: false,
        });
      } finally {
        if (inFlightOperation.current === intent.operationId)
          inFlightOperation.current = null;
      }
    },
    [bridge, refresh],
  );

  const reconcile = useCallback(
    async (intent: MutationIntent): Promise<void> => {
      if (inFlightOperation.current !== null) return;
      inFlightOperation.current = intent.operationId;
      setMutation({ kind: "submitting", intent });
      try {
        const result = await queries.run(`ci-receipt:${intent.operationId}`, () =>
          bridge.getCIReceipt(receiptParams(intent)),
        );
        if (result.receipt === null) {
          setMutation({
            kind: "failed",
            intent,
            error: null,
            uncertain: true,
            absentReceipt: true,
          });
          return;
        }
        requireReceiptBinding(result.receipt, intent);
        setMutation({ kind: "receipt", intent, receipt: result.receipt });
        refresh();
      } catch (error) {
        if (error instanceof StaleQueryError) return;
        setMutation({
          kind: "failed",
          intent,
          error,
          uncertain: true,
          absentReceipt: false,
        });
      } finally {
        if (inFlightOperation.current === intent.operationId)
          inFlightOperation.current = null;
      }
    },
    [bridge, queries, refresh],
  );

  const actionsLocked =
    mutation.kind === "submitting" ||
    (mutation.kind === "receipt" &&
      (mutation.receipt.outcome === "unknown" ||
        mutation.receipt.resync_required)) ||
    (mutation.kind === "failed" && mutation.uncertain);
  const capabilitiesMismatch =
    capabilities.value !== null &&
    capabilities.value.repository !== route.item.repository;
  const safeCapabilities =
    capabilities.error || capabilitiesMismatch
      ? null
      : (capabilities.value?.capabilities ?? null);
  const openOnForge = useCallback(
    async (url: string, label: string): Promise<void> => {
      try {
        if (!(await bridge.openExternal(url))) throw new Error("Open rejected");
        setForgeNotice({
          kind: "status",
          message: `${label} link sent to your browser.`,
        });
      } catch {
        setForgeNotice({
          kind: "error",
          message: `${label} could not be opened. Check the forge URL and retry.`,
        });
      }
    },
    [bridge],
  );

  return (
    <section
      className="ci-page"
      onKeyDown={(event) => focusLogSearch(event)}
    >
      <ReviewHeader route={route} navigate={navigate} panels={panels} />
      <div className="ci-toolbar">
        <div>
          <h2 className="section-title">Continuous integration</h2>
          <p className="ci-help">
            Inspect pipelines, jobs, and bounded text logs for this review.
          </p>
        </div>
        <button
          className="button button-secondary"
          disabled={pipelines.loading}
          onClick={refresh}
        >
          {pipelines.loading ? "Refreshing…" : "Refresh CI"}
        </button>
      </div>
      {eventNotice && <Notice kind="loading">{eventNotice}</Notice>}
      {forgeNotice && <Notice kind={forgeNotice.kind}>{forgeNotice.message}</Notice>}
      {capabilities.loading && !capabilities.value && (
        <Notice kind="loading">Checking CI action support…</Notice>
      )}
      {Boolean(capabilities.error) && (
        <Notice kind="error">
          CI action support is unavailable. Pipeline and log reads remain available.
        </Notice>
      )}
      {capabilitiesMismatch && (
        <Notice kind="error">
          CI action support did not match this repository. Actions remain disabled.
        </Notice>
      )}
      {pipelines.loading && !pipelines.value && (
        <Notice kind="loading">Loading pipelines…</Notice>
      )}
      {Boolean(pipelines.error) && (
        <Notice kind="error">
          {pipelines.value
            ? "Refresh failed. Showing the previous pipeline list."
            : safeError(pipelines.error)}
        </Notice>
      )}
      {pipelines.value !== null &&
        pipelineItems.length === 0 &&
        !pipelines.loading && (
          <Notice kind="empty">
            No pipelines are available for this review.
          </Notice>
        )}
      {pipelineItems.length > 0 && (
        <div className="ci-workspace">
          <PipelineList
            items={pipelineItems}
            selected={selectedPipeline}
            select={(handle) => {
              setSelectedPipeline(handle);
              setSelectedJob(null);
            }}
          />
          <div className="ci-detail">
            {selected && (
              <>
                <PipelineSummary
                  pipeline={selected}
                  capabilities={safeCapabilities}
                  actionsLocked={actionsLocked}
                  beginMutation={beginMutation}
                  openOnForge={openOnForge}
                />
                <JobsPanel
                  key={selected.handle}
                  bridge={bridge}
                  queries={queries}
                  pipeline={selected}
                  capabilities={safeCapabilities}
                  actionsLocked={actionsLocked}
                  refreshEpoch={refreshEpoch}
                  selectedJob={selectedJob}
                  selectJob={setSelectedJob}
                  beginMutation={beginMutation}
                  openOnForge={openOnForge}
                />
              </>
            )}
          </div>
        </div>
      )}
      <MutationStatus
        state={mutation}
        submit={submitMutation}
        reconcile={reconcile}
        refresh={refresh}
        clear={() => setMutation({ kind: "idle" })}
      />
    </section>
  );
}

function PipelineList({
  items,
  selected,
  select,
}: {
  readonly items: readonly PipelineItemDto[];
  readonly selected: string | null;
  readonly select: (handle: string) => void;
}): ReactNode {
  return (
    <nav
      className="ci-pipeline-list"
      aria-label="Review pipelines"
      onKeyDown={moveButtonFocus}
    >
      {items.map((item) => (
        <button
          key={item.handle}
          className={`ci-pipeline ${selected === item.handle ? "ci-selected" : ""}`}
          aria-current={selected === item.handle ? "true" : undefined}
          onClick={() => select(item.handle)}
        >
          <span className={`state state-${item.value.status}`}>
            {item.value.status}
          </span>
          <strong>Pipeline #{item.value.id}</strong>
          <span>{item.value.ref}</span>
          <code>{shortSha(item.value.sha)}</code>
        </button>
      ))}
    </nav>
  );
}

function PipelineSummary({
  pipeline,
  capabilities,
  actionsLocked,
  beginMutation,
  openOnForge,
}: {
  readonly pipeline: PipelineItemDto;
  readonly capabilities: CICapabilityFlags | null;
  readonly actionsLocked: boolean;
  readonly beginMutation: (
    action: CIMutationAction,
    pipeline: PipelineItemDto,
    job: JobItemDto | null,
  ) => void;
  readonly openOnForge: (url: string, label: string) => Promise<void>;
}): ReactNode {
  return (
    <section className="ci-summary" aria-label="Selected pipeline">
      <div>
        <p className="eyebrow">Selected pipeline</p>
        <h3>Pipeline #{pipeline.value.id}</h3>
        <p className="ci-meta">
          {pipeline.value.ref} · {pipeline.value.source} ·{" "}
          {formatDate(pipeline.value.created_at)}
        </p>
      </div>
      <div className="ci-actions">
        <button
          className="button button-secondary"
          onClick={() => void openOnForge(pipeline.value.web_url, "Pipeline")}
        >
          Open pipeline on forge
        </button>
        <ActionButton
          label="Retry pipeline"
          supported={capabilities?.retry_pipeline === true}
          unavailable={capabilities === null}
          locked={actionsLocked}
          onClick={() => beginMutation("retry_pipeline", pipeline, null)}
        />
        <ActionButton
          label="Cancel pipeline"
          supported={capabilities?.cancel_pipeline === true}
          unavailable={capabilities === null}
          locked={actionsLocked}
          onClick={() => beginMutation("cancel_pipeline", pipeline, null)}
        />
      </div>
    </section>
  );
}

function JobsPanel({
  bridge,
  queries,
  pipeline,
  capabilities,
  actionsLocked,
  refreshEpoch,
  selectedJob,
  selectJob,
  beginMutation,
  openOnForge,
}: {
  readonly bridge: DesktopBridge;
  readonly queries: QueryCoordinator;
  readonly pipeline: PipelineItemDto;
  readonly capabilities: CICapabilityFlags | null;
  readonly actionsLocked: boolean;
  readonly refreshEpoch: number;
  readonly selectedJob: JobItemDto | null;
  readonly selectJob: (job: JobItemDto | null) => void;
  readonly beginMutation: (
    action: CIMutationAction,
    pipeline: PipelineItemDto,
    job: JobItemDto | null,
  ) => void;
  readonly openOnForge: (url: string, label: string) => Promise<void>;
}): ReactNode {
  const begin = useCallback(
    () => bridge.listJobs(pipeline.handle),
    [bridge, pipeline.handle],
  );
  const jobs = useRetainedRead(
    queries,
    `ci-jobs:${pipeline.handle}`,
    begin,
    [pipeline.handle, refreshEpoch],
  );
  const items = jobs.value?.jobs ?? [];
  const selected =
    items.find((item) => item.handle === selectedJob?.handle) ?? null;

  useEffect(() => {
    const next = items.some((item) => item.handle === selectedJob?.handle)
      ? (selectedJob ?? null)
      : (items[0] ?? null);
    if (next?.handle !== selectedJob?.handle) selectJob(next);
  }, [items, selectJob, selectedJob]);

  return (
    <section className="ci-jobs" aria-label="Pipeline jobs">
      <div className="ci-section-heading">
        <h3>Jobs</h3>
        <button
          className="button button-secondary"
          disabled={jobs.loading}
          onClick={jobs.refresh}
        >
          Refresh jobs
        </button>
      </div>
      {jobs.loading && !jobs.value && (
        <Notice kind="loading">Loading jobs…</Notice>
      )}
      {Boolean(jobs.error) && (
        <Notice kind="error">
          {jobs.value
            ? "Refresh failed. Showing previous jobs."
            : safeError(jobs.error)}
        </Notice>
      )}
      {jobs.value !== null &&
        items.length === 0 &&
        !jobs.loading && (
          <Notice kind="empty">This pipeline has no jobs.</Notice>
        )}
      {items.length > 0 && (
        <div className="ci-job-layout">
          <div
            className="ci-job-list"
            role="list"
            aria-label="Jobs"
            onKeyDown={moveButtonFocus}
          >
            {items.map((item) => (
              <button
                key={item.handle}
                className={`ci-job ${selected?.handle === item.handle ? "ci-selected" : ""}`}
                role="listitem"
                onClick={() => selectJob(item)}
              >
                <span className={`state state-${item.value.status}`}>
                  {item.value.status}
                </span>
                <strong>{item.value.name}</strong>
                <span>{item.value.stage}</span>
              </button>
            ))}
          </div>
          {selected && (
            <div className="ci-job-detail">
              <div className="ci-actions">
                <button
                  className="button button-secondary"
                  onClick={() => void openOnForge(selected.value.web_url, "Job")}
                >
                  Open job on forge
                </button>
                <ActionButton
                  label="Retry job"
                  supported={capabilities?.retry_job === true}
                  unavailable={capabilities === null}
                  locked={actionsLocked}
                  onClick={() => beginMutation("retry_job", pipeline, selected)}
                />
                <ActionButton
                  label="Cancel job"
                  supported={capabilities?.cancel_job === true}
                  unavailable={capabilities === null}
                  locked={actionsLocked}
                  onClick={() => beginMutation("cancel_job", pipeline, selected)}
                />
              </div>
              <LogPanel
                key={selected.handle}
                bridge={bridge}
                queries={queries}
                job={selected}
                refreshEpoch={refreshEpoch}
              />
            </div>
          )}
        </div>
      )}
    </section>
  );
}

function LogPanel({
  bridge,
  queries,
  job,
  refreshEpoch,
}: {
  readonly bridge: DesktopBridge;
  readonly queries: QueryCoordinator;
  readonly job: JobItemDto;
  readonly refreshEpoch: number;
}): ReactNode {
  const [loaded, setLoaded] = useState<LoadedLog | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);
  const [editorOpening, setEditorOpening] = useState(false);
  const editorInFlight = useRef(false);
  const [editorNotice, setEditorNotice] = useState<{
    readonly kind: "status" | "error";
    readonly message: string;
  } | null>(null);
  useEffect(() => {
    let current = true;
    setLoading(true);
    setError(null);
    void loadLogPages(job.handle, bridge, queries)
      .then((value) => {
        if (!current) return;
        setLoaded(value);
        setError(value.partialError);
        setLoading(false);
      })
      .catch((reason: unknown) => {
        if (current && !(reason instanceof StaleQueryError)) {
          setError(reason);
          setLoading(false);
        }
      });
    return () => {
      current = false;
      void queries.cancel(`ci-log:${job.handle}`);
    };
  }, [bridge, job.handle, queries, refreshEpoch, reload]);
  const openInEditor = useCallback(async (): Promise<void> => {
    if (editorInFlight.current || loaded === null) return;
    editorInFlight.current = true;
    setEditorOpening(true);
    setEditorNotice(null);
    try {
      const result = await bridge.openJobLogInEditor(job.handle);
      setEditorNotice({
        kind: result.outcome === "started" ? "status" : "error",
        message: result.message,
      });
    } catch {
      setEditorNotice({
        kind: "error",
        message: "The editor could not be started. Check the configured command and retry.",
      });
    } finally {
      editorInFlight.current = false;
      setEditorOpening(false);
    }
  }, [bridge, job.handle, loaded]);

  return (
    <section
      className="ci-log"
      aria-label={`Log for ${job.value.name}`}
      onKeyDown={(event) => {
        if (event.key !== "F2" || loaded === null || editorOpening) return;
        event.preventDefault();
        void openInEditor();
      }}
    >
      <div className="ci-section-heading">
        <h3>Log · {job.value.name}</h3>
        <div className="ci-actions">
          <button
            className="button button-secondary"
            disabled={loaded === null || editorOpening}
            title="Starts the configured graphical editor with a private log export (F2)"
            onClick={() => void openInEditor()}
          >
            {editorOpening ? "Starting editor…" : "Open log in editor"}
          </button>
          <button
            className="button button-secondary"
            disabled={loading}
            onClick={() => setReload((current) => current + 1)}
          >
            Refresh log
          </button>
        </div>
      </div>
      {editorNotice && (
        <Notice kind={editorNotice.kind}>{editorNotice.message}</Notice>
      )}
      {loading && !loaded && <Notice kind="loading">Loading job log…</Notice>}
      {Boolean(error) && (
        <Notice kind="error">
          {loaded
            ? "The log refresh or paging stopped. A bounded previous result is shown."
            : logError(error)}
        </Notice>
      )}
      {loaded && <LogViewer loaded={loaded} />}
    </section>
  );
}

function LogViewer({ loaded }: { readonly loaded: LoadedLog }): ReactNode {
  const [query, setQuery] = useState("");
  const [selectedMatch, setSelectedMatch] = useState(0);
  const search = query.toLocaleLowerCase();
  const matches = useMemo(
    () =>
      search
        ? loaded.lines.flatMap((line, index) =>
            line.toLocaleLowerCase().includes(search) ? [index] : [],
          )
        : [],
    [loaded.lines, search],
  );
  useEffect(() => setSelectedMatch(0), [query, loaded.snapshotId]);
  const target = matches[selectedMatch] ?? 0;
  return (
    <>
      <div className="ci-log-toolbar">
        <label>
          <span>Search log</span>
          <input
            className="ci-log-search"
            type="search"
            value={query}
            placeholder="Press / to search"
            onChange={(event) => setQuery(event.currentTarget.value)}
          />
        </label>
        <span className="ci-match-count" role="status">
          {query
            ? matches.length
              ? `${selectedMatch + 1} of ${matches.length} matches`
              : "No matches"
            : `${loaded.lines.length} lines`}
        </span>
        <button
          className="button button-secondary"
          disabled={matches.length === 0}
          onClick={() =>
            setSelectedMatch((current) =>
              (current - 1 + matches.length) % matches.length,
            )
          }
        >
          Previous match
        </button>
        <button
          className="button button-secondary"
          disabled={matches.length === 0}
          onClick={() =>
            setSelectedMatch((current) => (current + 1) % matches.length)
          }
        >
          Next match
        </button>
      </div>
      {loaded.lines.length === 0 ? (
        <Notice kind="empty">This job has no log output.</Notice>
      ) : (
        <LogWindow
          lines={loaded.lines}
          targetIndex={target}
          matches={new Set(matches)}
          selectedMatch={matches[selectedMatch] ?? null}
        />
      )}
    </>
  );
}

function LogWindow({
  lines,
  targetIndex,
  matches,
  selectedMatch,
}: {
  readonly lines: readonly string[];
  readonly targetIndex: number;
  readonly matches: ReadonlySet<number>;
  readonly selectedMatch: number | null;
}): ReactNode {
  const [start, setStart] = useState(
    Math.floor(targetIndex / LOG_WINDOW_SIZE) * LOG_WINDOW_SIZE,
  );
  useEffect(
    () => setStart(Math.floor(targetIndex / LOG_WINDOW_SIZE) * LOG_WINDOW_SIZE),
    [targetIndex],
  );
  const boundedStart = Math.min(start, Math.max(0, lines.length - 1));
  const end = Math.min(lines.length, boundedStart + LOG_WINDOW_SIZE);
  return (
    <div className="ci-log-window">
      <pre className="ci-log-lines" aria-label="Job log">
        {lines.slice(boundedStart, end).map((line, offset) => {
          const index = boundedStart + offset;
          return (
            <code
              key={index}
              className={`${matches.has(index) ? "ci-log-match" : ""}${selectedMatch === index ? " ci-log-match-selected" : ""}`}
              data-line={index + 1}
            >
              <span className="ci-log-number">{index + 1}</span>
              <span>{line}</span>{"\n"}
            </code>
          );
        })}
      </pre>
      {lines.length > LOG_WINDOW_SIZE && (
        <nav className="ci-log-windows" aria-label="Log row windows">
          <button
            className="button button-secondary"
            disabled={boundedStart === 0}
            onClick={() => setStart(Math.max(0, boundedStart - LOG_WINDOW_SIZE))}
          >
            Previous rows
          </button>
          <span>
            {boundedStart + 1}-{end} of {lines.length}
          </span>
          <button
            className="button button-secondary"
            disabled={end === lines.length}
            onClick={() => setStart(end)}
          >
            Next rows
          </button>
        </nav>
      )}
    </div>
  );
}

function ActionButton({
  label,
  supported,
  unavailable,
  locked,
  onClick,
}: {
  readonly label: string;
  readonly supported: boolean;
  readonly unavailable: boolean;
  readonly locked: boolean;
  readonly onClick: () => void;
}): ReactNode {
  const reason = unavailable
    ? "CI action support is unavailable."
    : !supported
      ? `${label} is not supported by this forge.`
      : locked
        ? "Resolve the current CI action before starting another."
        : null;
  return (
    <button
      className="button button-secondary ci-action"
      disabled={reason !== null}
      title={reason ?? undefined}
      onClick={onClick}
    >
      {label}
    </button>
  );
}

function MutationStatus({
  state,
  submit,
  reconcile,
  refresh,
  clear,
}: {
  readonly state: MutationState;
  readonly submit: (intent: MutationIntent) => Promise<void>;
  readonly reconcile: (intent: MutationIntent) => Promise<void>;
  readonly refresh: () => void;
  readonly clear: () => void;
}): ReactNode {
  const confirm = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (state.kind === "confirm") confirm.current?.focus();
  }, [state.kind]);
  if (state.kind === "idle") return null;
  const target = targetLabel(state.intent);
  if (state.kind === "confirm") {
    return (
      <section className="ci-confirmation" role="alertdialog" aria-modal="true">
        <div>
          <strong>Confirm {actionLabel(state.intent.action)}</strong>
          <p>
            {target}. This sends one forge mutation with operation ID{" "}
            <code>{state.intent.operationId}</code>.
          </p>
        </div>
        <div className="ci-actions">
          <button className="button button-quiet" onClick={clear}>
            Cancel
          </button>
          <button
            ref={confirm}
            className="button ci-confirm"
            onClick={() => void submit(state.intent)}
          >
            Confirm {actionLabel(state.intent.action)}
          </button>
        </div>
      </section>
    );
  }
  if (state.kind === "submitting") {
    return (
      <Notice kind="loading">
        Sending {actionLabel(state.intent.action)} for {target}…
      </Notice>
    );
  }
  if (state.kind === "receipt") {
    const unknown = state.receipt.outcome === "unknown";
    return (
      <section
        className={`ci-mutation-result ${unknown ? "ci-outcome-unknown" : "ci-outcome-known"}`}
        role={unknown ? "alert" : "status"}
      >
        <strong>
          {unknown ? "Remote outcome unknown" : "CI action accepted"}
        </strong>
        <p>
          {actionLabel(state.intent.action)} for {target}. Operation{" "}
          <code>{state.intent.operationId}</code>.
        </p>
        {state.receipt.error && <p>{mutationErrorText(state.receipt.error)}</p>}
        {state.receipt.resync_required && (
          <p>Refresh pipeline and job status before acting again.</p>
        )}
        <div className="ci-actions">
          <button className="button button-secondary" onClick={refresh}>
            Refresh status
          </button>
          {unknown ? (
            <>
              <button
                className="button button-secondary"
                onClick={() => void reconcile(state.intent)}
              >
                Check retained receipt
              </button>
              <button className="button button-secondary" onClick={clear}>
                Acknowledge after review
              </button>
            </>
          ) : (
            <button className="button button-secondary" onClick={clear}>
              {state.receipt.resync_required
                ? "Acknowledge after refresh"
                : "Dismiss"}
            </button>
          )}
        </div>
      </section>
    );
  }
  return (
    <section className="ci-mutation-result ci-outcome-unknown" role="alert">
      <strong>
        {state.uncertain ? "CI action needs reconciliation" : "CI action rejected"}
      </strong>
      <p>
        {actionLabel(state.intent.action)} for {target}. Operation{" "}
        <code>{state.intent.operationId}</code>.
      </p>
      <p>
        {state.absentReceipt
          ? "No retained receipt is available in this session. This can mean absent or still pending and does not permit automatic replay."
          : mutationFailureText(state.error, state.uncertain)}
      </p>
      <div className="ci-actions">
        <button className="button button-secondary" onClick={refresh}>
          Refresh status
        </button>
        {state.uncertain && (
          <button
            className="button button-secondary"
            onClick={() => void reconcile(state.intent)}
          >
            Check retained receipt
          </button>
        )}
        <button className="button button-secondary" onClick={clear}>
          {state.uncertain ? "Acknowledge after review" : "Dismiss"}
        </button>
      </div>
    </section>
  );
}

export async function loadLogPages(
  job: string,
  bridge: Pick<DesktopBridge, "openLog" | "pageLog" | "cancelRead">,
  queries: QueryCoordinator,
): Promise<LoadedLog> {
  const key = `ci-log:${job}`;
  const first = await queries.run(key, () =>
    bridge.openLog({ job, max_items: LOG_PAGE_SIZE }),
  );
  if (first.cursor !== 0 || first.resource !== job)
    throw invalidLogPage("The first log page has an invalid identity.");
  assertLogCursor(first);
  const chunks: string[] = [];
  let characters = 0;
  let page = first;
  let pageCount = 1;
  while (true) {
    for (const row of page.entries) {
      const remaining = MAX_LOG_CHARACTERS - characters;
      if (row.text.length > remaining) {
        if (remaining > 0) chunks.push(row.text.slice(0, remaining));
        return loadedLog(
          first,
          logLines(chunks.join(""), MAX_LOG_ROWS).lines,
          logLimit("The job log exceeded the bounded renderer limit."),
        );
      }
      chunks.push(row.text);
      characters += row.text.length;
    }
    if (page.next_cursor === null) break;
    if (pageCount >= MAX_LOG_PAGES)
      return loadedLog(
        first,
        logLines(chunks.join(""), MAX_LOG_ROWS).lines,
        logLimit("The job log exceeded the bounded page limit."),
      );
    const cursor = page.next_cursor;
    page = await queries.run(key, () =>
      bridge.pageLog({
        snapshot: first.snapshot_id,
        resource: first.resource,
        cursor,
        max_items: LOG_PAGE_SIZE,
      }),
    );
    if (
      page.snapshot_id !== first.snapshot_id ||
      page.resource !== first.resource ||
      page.revision.sha256 !== first.revision.sha256 ||
      page.revision.byte_count !== first.revision.byte_count
    ) {
      throw new RendererReadError(
        "revision_changed",
        "The job log changed during pagination.",
        false,
      );
    }
    if (page.cursor !== cursor)
      throw invalidLogPage("The log page cursor did not match the request.");
    assertLogCursor(page);
    pageCount += 1;
  }
  const reconstructed = logLines(chunks.join(""), MAX_LOG_ROWS);
  return loadedLog(
    first,
    reconstructed.lines,
    reconstructed.truncated
      ? logLimit("The job log exceeded the bounded line limit.")
      : null,
  );
}

function logLines(
  text: string,
  limit: number,
): { readonly lines: readonly string[]; readonly truncated: boolean } {
  if (text.length === 0) return { lines: [], truncated: false };
  const rawLines = text.split("\n");
  if (text.endsWith("\n")) rawLines.pop();
  const truncated = rawLines.length > limit;
  return {
    lines: rawLines
      .slice(0, limit)
      .map((line) => sanitizeLogText(line.endsWith("\r") ? line.slice(0, -1) : line)),
    truncated,
  };
}

export function sanitizeLogText(value: string): string {
  let result = "";
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (code === 0x1b) {
      const next = value.charCodeAt(index + 1);
      if (next === 0x5b) {
        index += 2;
        while (index < value.length) {
          const current = value.charCodeAt(index);
          if (current >= 0x40 && current <= 0x7e) break;
          index += 1;
        }
      } else if (next === 0x5d) {
        index += 2;
        while (index < value.length) {
          const current = value.charCodeAt(index);
          if (current === 0x07) break;
          if (current === 0x1b && value.charCodeAt(index + 1) === 0x5c) {
            index += 1;
            break;
          }
          index += 1;
        }
      }
      continue;
    }
    if ((code < 32 && code !== 9) || code === 127) {
      result += "�";
      continue;
    }
    result += value[index];
  }
  return result;
}

export function classifyMutationFailure(
  error: unknown,
): "known" | "unknown" {
  const code = errorCode(error);
  return new Set([
    "authentication_failed",
    "closed",
    "conflict",
    "invalid_handle",
    "invalid_input",
    "invalid_params",
    "not_found",
    "not_running",
    "permission_denied",
    "rate_limited",
    "request_cancelled",
    "request_id_exhausted",
    "request_too_large",
    "resource_not_issued",
    "too_many_requests",
    "wrong_handle_kind",
  ]).has(code)
    ? "known"
    : "unknown";
}

function loadedLog(
  first: LogPage,
  lines: readonly string[],
  partialError: RendererReadError | null,
): LoadedLog {
  return Object.freeze({
    snapshotId: first.snapshot_id,
    resource: first.resource,
    revision: first.revision,
    lines: Object.freeze([...lines]),
    partialError,
  });
}

function assertLogCursor(page: LogPage): void {
  if (page.next_cursor !== null && page.next_cursor <= page.cursor)
    throw invalidLogPage("The log page cursor did not advance.");
}

function invalidLogPage(message: string): RendererReadError {
  return new RendererReadError("invalid_response", message, false);
}

function logLimit(message: string): RendererReadError {
  return new RendererReadError("pagination_limit", message, false);
}

function logError(error: unknown): string {
  const code = errorCode(error);
  if (code === "snapshot_expired")
    return "This log snapshot expired. Reload the job log to continue.";
  if (code === "revision_changed")
    return "The job log changed while loading. Reload the current log.";
  if (code === "invalid_response")
    return "The local service returned inconsistent log pages. Reload the log.";
  return "The local service could not load this job log.";
}

function createOperationId(action: CIMutationAction): string {
  return `desktop:${action}:${crypto.randomUUID()}`;
}

async function executeMutation(
  bridge: DesktopBridge,
  intent: MutationIntent,
): Promise<CIMutationReceipt> {
  const pipeline: PipelineMutationParams = {
    operation_id: intent.operationId,
    pipeline: intent.pipeline.handle,
  };
  if (intent.action === "retry_pipeline") return bridge.retryPipeline(pipeline);
  if (intent.action === "cancel_pipeline") return bridge.cancelPipeline(pipeline);
  if (intent.job === null)
    throw new RendererReadError(
      "invalid_response",
      "A job action lost its selected job.",
      false,
    );
  const job: JobMutationParams = { ...pipeline, job: intent.job.handle };
  if (intent.action === "retry_job") return bridge.retryJob(job);
  return bridge.cancelJob(job);
}

function receiptParams(intent: MutationIntent): CIReceiptParams {
  if (intent.action === "retry_pipeline" || intent.action === "cancel_pipeline") {
    return {
      operation_id: intent.operationId,
      action: intent.action,
      pipeline: intent.pipeline.handle,
    };
  }
  if (intent.job === null)
    throw new RendererReadError(
      "invalid_response",
      "A job receipt lost its selected job.",
      false,
    );
  return {
    operation_id: intent.operationId,
    action: intent.action,
    pipeline: intent.pipeline.handle,
    job: intent.job.handle,
  };
}

function requireReceiptBinding(
  receipt: CIMutationReceipt,
  intent: MutationIntent,
): void {
  if (
    receipt.operation_id !== intent.operationId ||
    receipt.action !== intent.action
  ) {
    throw new RendererReadError(
      "invalid_response",
      "The CI receipt does not match the confirmed action.",
      false,
    );
  }
}

function targetLabel(intent: MutationIntent): string {
  return intent.job
    ? `job ${intent.job.value.name} (#${intent.job.value.id}) in pipeline #${intent.pipeline.value.id}`
    : `pipeline #${intent.pipeline.value.id} on ${intent.pipeline.value.ref}`;
}

function actionLabel(action: CIMutationAction): string {
  return action.replace("_", " ");
}

function mutationErrorText(error: { readonly code: string }): string {
  return error.code === "network_unavailable"
    ? "The forge response was interrupted, so remote state must be refreshed."
    : "The remote result could not be confirmed. Refresh current state.";
}

function mutationFailureText(error: unknown, uncertain: boolean): string {
  if (uncertain)
    return "The response ended after the action may have been dispatched. It will not be replayed automatically.";
  const code = errorCode(error);
  if (code === "conflict")
    return "That operation ID is already bound to another action or target.";
  if (code === "permission_denied" || code === "authentication_failed")
    return "The forge rejected this action for the current account.";
  if (code === "resource_not_issued" || code === "invalid_handle")
    return "The selected pipeline or job is stale. Refresh before trying again.";
  if (code === "not_found")
    return "The forge rejected this action because the selected target no longer exists.";
  return "The action was rejected with a known result. Refresh current state before trying again.";
}

function errorCode(error: unknown): string {
  if (error !== null && typeof error === "object" && "code" in error)
    return String(error.code);
  // A read rejected in the main process keeps no properties, only its message,
  // so the job log can only see snapshot_expired or revision_changed once the
  // failure is recovered from that message.  Mutation failures still arrive as
  // objects and take the branch above unchanged.
  return serviceErrorOf(error)?.code ?? "";
}

function shortSha(value: string): string {
  return value.slice(0, 8);
}

function moveButtonFocus(event: KeyboardEvent<HTMLElement>): void {
  if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
  const buttons = [
    ...event.currentTarget.querySelectorAll<HTMLButtonElement>("button"),
  ];
  const current = buttons.indexOf(document.activeElement as HTMLButtonElement);
  const direction = event.key === "ArrowDown" ? 1 : -1;
  const next =
    buttons[
      (current < 0 ? 0 : current + direction + buttons.length) % buttons.length
    ];
  if (!next) return;
  event.preventDefault();
  next.focus();
  next.click();
}

function focusLogSearch(event: KeyboardEvent<HTMLElement>): void {
  if (
    event.key !== "/" ||
    event.ctrlKey ||
    event.metaKey ||
    event.altKey ||
    event.target instanceof HTMLInputElement
  ) {
    return;
  }
  const input = event.currentTarget.querySelector<HTMLInputElement>(
    ".ci-log-search",
  );
  if (input) {
    event.preventDefault();
    input.focus();
  }
}

function isPipelineRefreshEvent(
  name: string,
  data: unknown,
  selectedPipeline: string | null,
): boolean {
  if (name === "protocol.resync_required") return true;
  if (name !== "service.changed" || data === null || typeof data !== "object")
    return false;
  if ("kind" in data && data.kind === "resync_required") return true;
  if (!("kind" in data) || data.kind !== "pipeline_changed") return false;
  return (
    !("resource" in data) ||
    data.resource === null ||
    data.resource === selectedPipeline
  );
}

function isServiceResync(name: string, data: unknown): boolean {
  return (
    name === "service.changed" &&
    data !== null &&
    typeof data === "object" &&
    "kind" in data &&
    data.kind === "resync_required"
  );
}

function Notice({
  kind,
  children,
}: {
  readonly kind: string;
  readonly children: ReactNode;
}): ReactNode {
  return (
    <div
      className={`notice notice-${kind}`}
      role={kind === "error" ? "alert" : "status"}
    >
      {children}
    </div>
  );
}
