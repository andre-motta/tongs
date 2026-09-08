import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import type {
  DesktopBridge,
  DiffFileRow,
  DiffHunkRow,
  DiffLayout,
  DiffPage,
  DiffRow,
  SplitDiffCell,
  SplitDiffRow,
} from "../../../shared/bridge.js";
import type { AppRoute, FeatureContribution } from "../../core/navigation.js";
import { safeError } from "../../core/presentation.js";
import { QueryCoordinator } from "../../core/query.js";
import { WindowedList } from "../../core/windowed-list.js";
import { ReviewHeader } from "../review-detail/index.js";

export interface LoadedDiff {
  readonly layout: DiffLayout;
  readonly revision: DiffPage["revision"];
  readonly rows: readonly DiffRow[];
}

export function createDiffFeature(): FeatureContribution {
  return {
    id: "review.diff",
    order: 22,
    matches: (route) => route.kind === "review" && route.panel === "diff",
    render: (context, route) =>
      route.kind === "review" && route.panel === "diff" ? (
        <DiffView
          bridge={context.bridge}
          queries={context.queries}
          route={route}
          navigate={context.navigate}
        />
      ) : null,
  };
}

function DiffView({
  bridge,
  queries,
  route,
  navigate,
}: {
  readonly bridge: DesktopBridge;
  readonly queries: QueryCoordinator;
  readonly route: Extract<AppRoute, { kind: "review" }>;
  readonly navigate: (route: AppRoute) => void;
}): ReactNode {
  const [layout, setLayout] = useState<DiffLayout>("unified");
  const [loaded, setLoaded] = useState<LoadedDiff | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);
  useEffect(() => {
    let current = true;
    setLoading(true);
    setError(null);
    void loadAllPages(route.item.handle, layout, bridge, queries)
      .then((value) => {
        if (current) {
          setLoaded(value);
          setLoading(false);
        }
      })
      .catch((reason: unknown) => {
        if (current) {
          setError(reason);
          setLoading(false);
        }
      });
    return () => {
      current = false;
      void queries.cancel(`diff:${route.item.handle}`);
    };
  }, [bridge, layout, queries, reload, route.item.handle]);
  return (
    <>
      <ReviewHeader route={route} navigate={navigate} />
      <div className="diff-toolbar">
        <strong className="toolbar-title">Layout</strong>
        <button
          className={`button ${layout === "unified" ? "button-active" : "button-secondary"}`}
          onClick={() => setLayout("unified")}
        >
          Unified
        </button>
        <button
          className={`button ${layout === "split" ? "button-active" : "button-secondary"}`}
          onClick={() => setLayout("split")}
        >
          Split
        </button>
        <button
          className="button button-secondary"
          disabled={loading}
          onClick={() => setReload((value) => value + 1)}
        >
          Refresh
        </button>
        {loaded && (
          <code
            className="revision"
            data-head-sha={loaded.revision.head_sha}
            data-base-sha={loaded.revision.base_sha}
            data-start-sha={loaded.revision.start_sha ?? ""}
          >
            {loaded.revision.head_sha.slice(0, 12)}
          </code>
        )}
      </div>
      {loading && (
        <Notice kind="loading">
          {loaded ? "Refreshing diff…" : "Loading source diff…"}
        </Notice>
      )}
      {Boolean(error) && (
        <Notice kind="error">
          {loaded
            ? "Refresh failed. Showing the previous diff snapshot."
            : safeError(error)}
        </Notice>
      )}
      {loaded && <DiffWorkspace loaded={loaded} />}
    </>
  );
}

export async function loadAllPages(
  review: string,
  layout: DiffLayout,
  bridge: DesktopBridge,
  queries: QueryCoordinator,
): Promise<LoadedDiff> {
  const key = `diff:${review}`;
  const first = await queries.run(key, () =>
    bridge.openDiff({ review, layout, max_items: 1000 }),
  );
  const rows = [...first.entries];
  let page = first;
  while (page.next_cursor !== null) {
    const cursor = page.next_cursor;
    page = await queries.run(key, () =>
      bridge.pageDiff({
        snapshot: first.snapshot_id,
        resource: first.resource,
        cursor,
        max_items: 1000,
      }),
    );
    if (
      page.snapshot_id !== first.snapshot_id ||
      page.resource !== first.resource ||
      !sameRevision(page, first)
    )
      throw new Error("revision_changed");
    rows.push(...page.entries);
  }
  return Object.freeze({
    layout,
    revision: first.revision,
    rows: Object.freeze(rows),
  });
}

function DiffWorkspace({ loaded }: { readonly loaded: LoadedDiff }): ReactNode {
  const files = useMemo(
    () => loaded.rows.filter((row): row is DiffFileRow => row.kind === "file"),
    [loaded.rows],
  );
  const [selected, setSelected] = useState(files[0]?.file_index ?? 0);
  const [target, setTarget] = useState(0);
  useEffect(() => {
    setSelected(files[0]?.file_index ?? 0);
    setTarget(0);
  }, [files]);
  const visible = loaded.rows.filter((row) => row.file_index === selected);
  const moveFocus = (event: KeyboardEvent<HTMLElement>): void => {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    const buttons = [
      ...event.currentTarget.querySelectorAll<HTMLButtonElement>("button"),
    ];
    const current = buttons.indexOf(
      document.activeElement as HTMLButtonElement,
    );
    const direction = event.key === "ArrowDown" ? 1 : -1;
    const next =
      buttons[
        (current < 0 ? 0 : current + direction + buttons.length) %
          buttons.length
      ];
    if (next) {
      event.preventDefault();
      next.focus();
      next.click();
    }
  };
  return (
    <div className="diff-workspace">
      <nav
        className="file-list"
        aria-label="Changed files"
        onKeyDown={moveFocus}
      >
        {files.map((file) => (
          <FileNavigation
            key={file.file_index}
            file={file}
            rows={loaded.rows}
            selected={selected}
            select={(fileIndex, rowIndex) => {
              setSelected(fileIndex);
              setTarget(rowIndex);
            }}
          />
        ))}
        {files.length === 0 && (
          <Notice kind="empty">No file metadata was returned.</Notice>
        )}
      </nav>
      <section
        className={`diff-view diff-${loaded.layout}`}
        data-layout={loaded.layout}
      >
        <WindowedList
          items={visible}
          pageSize={200}
          targetIndex={target}
          renderItem={(row, index) => (
            <DiffRowView
              key={`${row.kind}:${row.file_index}:${"hunk_index" in row ? row.hunk_index : 0}:${index}`}
              row={row}
            />
          )}
        />
      </section>
    </div>
  );
}

function FileNavigation({
  file,
  rows,
  selected,
  select,
}: {
  readonly file: DiffFileRow;
  readonly rows: readonly DiffRow[];
  readonly selected: number;
  readonly select: (fileIndex: number, rowIndex: number) => void;
}): ReactNode {
  const visible = rows.filter((row) => row.file_index === file.file_index);
  const hunks = visible.filter(
    (row): row is DiffHunkRow => row.kind === "hunk",
  );
  return (
    <>
      <button
        className={`file-item ${selected === file.file_index ? "nav-item-active" : ""}`}
        onClick={() => select(file.file_index, 0)}
      >
        <span>{file.new_path || file.old_path}</span>
        <span className="file-stats">
          +{file.additions} −{file.deletions}
        </span>
      </button>
      {hunks.map((hunk) => (
        <button
          key={hunk.hunk_index}
          className="file-item hunk-item"
          onClick={() => select(file.file_index, visible.indexOf(hunk))}
        >
          {hunk.context_text || hunk.header}
        </button>
      ))}
    </>
  );
}

function DiffRowView({ row }: { readonly row: DiffRow }): ReactNode {
  if (row.kind === "file") {
    const path =
      row.old_path === row.new_path
        ? row.new_path
        : `${row.old_path} → ${row.new_path}`;
    return (
      <header className="diff-file" role="listitem">
        <strong className="file-path">{path}</strong>
        <span className="file-status">{row.status}</span>
        {[
          [row.is_binary, "Binary"],
          [row.is_truncated, "Truncated"],
          [row.is_empty, "Empty"],
          [row.is_mode_only, "Mode only"],
          [row.is_unavailable, "Unavailable"],
        ].map(
          ([condition, label]) =>
            condition && (
              <span key={String(label)} className="badge">
                {label}
              </span>
            ),
        )}
      </header>
    );
  }
  if (row.kind === "hunk")
    return (
      <div className="diff-hunk" role="listitem">
        {row.header}
      </div>
    );
  if (row.kind === "split") return <SplitRow row={row} />;
  return (
    <div className={`diff-line line-${row.line_type}`} role="listitem">
      <span className="line-number">{row.old_line}</span>
      <span className="line-number">{row.new_line}</span>
      <code className="line-content">
        {marker(row.line_type)}
        {row.content}
      </code>
    </div>
  );
}

function SplitRow({ row }: { readonly row: SplitDiffRow }): ReactNode {
  return (
    <div className="split-row" role="listitem">
      <SplitCell cell={row.old} side="old" />
      <SplitCell cell={row.new} side="new" />
    </div>
  );
}
function SplitCell({
  cell,
  side,
}: {
  readonly cell: SplitDiffCell | null;
  readonly side: "old" | "new";
}): ReactNode {
  return (
    <div
      className={`split-cell split-${side}${cell ? ` line-${cell.line_type}` : " split-empty"}`}
    >
      {cell && (
        <>
          <span className="line-number">
            {side === "old" ? cell.old_line : cell.new_line}
          </span>
          <code className="line-content">
            {marker(cell.line_type)}
            {cell.content}
          </code>
        </>
      )}
    </div>
  );
}
function marker(type: string): string {
  return type === "addition"
    ? "+"
    : type === "deletion"
      ? "−"
      : type === "no_newline"
        ? "\\ "
        : " ";
}
function sameRevision(left: DiffPage, right: DiffPage): boolean {
  return (
    left.revision.head_sha === right.revision.head_sha &&
    left.revision.base_sha === right.revision.base_sha &&
    left.revision.start_sha === right.revision.start_sha
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
