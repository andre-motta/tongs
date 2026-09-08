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
  ReviewRevisionDto,
  SplitDiffCell,
  SplitDiffRow,
} from "../../../shared/bridge.js";
import type {
  AppRoute,
  FeatureContribution,
  InlineAnchorSelection,
} from "../../core/navigation.js";
import { RendererReadError, safeError } from "../../core/presentation.js";
import { QueryCoordinator } from "../../core/query.js";
import { ReviewHeader } from "../review-detail/index.js";

const MAX_DIFF_PAGES = 1000;
const MAX_DIFF_ROWS = 100_000;

export interface LoadedDiff {
  readonly layout: DiffLayout;
  readonly snapshotId: string;
  readonly resource: string;
  readonly revision: DiffPage["revision"];
  readonly rows: readonly DiffRow[];
  readonly partialError: RendererReadError | null;
}

export function createDiffFeature(): FeatureContribution {
  return {
    id: "review.diff",
    order: 22,
    reviewPanel: { id: "diff", label: "Files changed", order: 20 },
    matches: (route) => route.kind === "review" && route.panel === "diff",
    render: (context, route) =>
      route.kind === "review" && route.panel === "diff" ? (
        <DiffView
          bridge={context.bridge}
          queries={context.queries}
          route={route}
          navigate={context.navigate}
          panels={context.reviewPanels}
          inlineAnchor={context.inlineAnchor}
          selectInlineAnchor={context.selectInlineAnchor}
        />
      ) : null,
  };
}

function DiffView({
  bridge,
  queries,
  route,
  navigate,
  panels,
  inlineAnchor,
  selectInlineAnchor,
}: {
  readonly bridge: DesktopBridge;
  readonly queries: QueryCoordinator;
  readonly route: Extract<AppRoute, { kind: "review" }>;
  readonly navigate: (route: AppRoute) => void;
  readonly panels: FeatureContributionParameters["reviewPanels"];
  readonly inlineAnchor: InlineAnchorSelection | null;
  readonly selectInlineAnchor: (
    selection: InlineAnchorSelection | null,
  ) => void;
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
          setError(value.partialError);
          setLoading(false);
        }
      })
      .catch((reason: unknown) => {
        if (current) {
          if (invalidatesSelection(reason)) selectInlineAnchor(null);
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
      <ReviewHeader route={route} navigate={navigate} panels={panels} />
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
          {safeError(error)}
          {loaded && loaded.partialError !== error
            ? " Showing the previous diff snapshot."
            : ""}
        </Notice>
      )}
      {loaded && (
        <DiffWorkspace
          review={route.item.handle}
          loaded={loaded}
          selection={inlineAnchor}
          selectAnchor={selectInlineAnchor}
        />
      )}
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
  if (first.cursor !== 0)
    throw invalidPage("The first diff page did not start at cursor zero.");
  assertNextCursor(first);
  const rows = [...first.entries];
  if (rows.length > MAX_DIFF_ROWS)
    return loadedDiff(
      layout,
      first,
      rows.slice(0, MAX_DIFF_ROWS),
      paginationLimit("The first diff page exceeded the renderer row limit."),
    );
  let page = first;
  let pageCount = 1;
  while (page.next_cursor !== null) {
    if (pageCount >= MAX_DIFF_PAGES || rows.length >= MAX_DIFF_ROWS)
      return loadedDiff(
        layout,
        first,
        rows.slice(0, MAX_DIFF_ROWS),
        paginationLimit("The diff exceeded the bounded renderer paging limit."),
      );
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
      throw new RendererReadError(
        "revision_changed",
        "The review revision changed during diff pagination.",
        false,
      );
    if (page.cursor !== cursor)
      throw invalidPage("The diff page cursor did not match the request.");
    assertNextCursor(page);
    if (rows.length + page.entries.length > MAX_DIFF_ROWS)
      return loadedDiff(
        layout,
        first,
        [...rows, ...page.entries].slice(0, MAX_DIFF_ROWS),
        paginationLimit("The diff exceeded the bounded renderer row limit."),
      );
    rows.push(...page.entries);
    pageCount += 1;
  }
  return loadedDiff(layout, first, rows, null);
}

function loadedDiff(
  layout: DiffLayout,
  first: DiffPage,
  rows: readonly DiffRow[],
  partialError: RendererReadError | null,
): LoadedDiff {
  return Object.freeze({
    layout,
    snapshotId: first.snapshot_id,
    resource: first.resource,
    revision: first.revision,
    rows: Object.freeze(rows),
    partialError,
  });
}

function assertNextCursor(page: DiffPage): void {
  if (page.next_cursor !== null && page.next_cursor <= page.cursor)
    throw invalidPage("The diff page cursor did not advance.");
}

function invalidPage(message: string): RendererReadError {
  return new RendererReadError("invalid_response", message, false);
}

function paginationLimit(message: string): RendererReadError {
  return new RendererReadError("pagination_limit", message, false);
}

function invalidatesSelection(error: unknown): boolean {
  return (
    error !== null &&
    typeof error === "object" &&
    "code" in error &&
    (error.code === "snapshot_expired" || error.code === "revision_changed")
  );
}

type FeatureContributionParameters = Parameters<
  FeatureContribution["render"]
>[0];

function DiffWorkspace({
  review,
  loaded,
  selection,
  selectAnchor,
}: {
  readonly review: string;
  readonly loaded: LoadedDiff;
  readonly selection: InlineAnchorSelection | null;
  readonly selectAnchor: (selection: InlineAnchorSelection | null) => void;
}): ReactNode {
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
  useEffect(() => {
    if (!selection) return;
    const rebound = rebindInlineAnchor(selection, review, loaded);
    if (!rebound) selectAnchor(null);
    else if (!sameAnchorIdentity(selection, rebound)) selectAnchor(rebound);
  }, [loaded, review, selectAnchor, selection]);
  const visible = loaded.rows.filter((row) => row.file_index === selected);
  const selectedFile =
    files.find((file) => file.file_index === selected) ?? null;
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
              if (fileIndex !== selected) selectAnchor(null);
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
        <DiffRowsWindow
          review={review}
          loaded={loaded}
          items={visible}
          pageSize={200}
          targetIndex={target}
          file={selectedFile}
          selection={selection}
          selectAnchor={selectAnchor}
        />
      </section>
    </div>
  );
}

function DiffRowsWindow({
  review,
  loaded,
  items,
  pageSize,
  targetIndex,
  file,
  selection,
  selectAnchor,
}: {
  readonly review: string;
  readonly loaded: LoadedDiff;
  readonly items: readonly DiffRow[];
  readonly pageSize: number;
  readonly targetIndex: number;
  readonly file: DiffFileRow | null;
  readonly selection: InlineAnchorSelection | null;
  readonly selectAnchor: (selection: InlineAnchorSelection | null) => void;
}): ReactNode {
  const [start, setStart] = useState(
    Math.max(0, Math.floor(targetIndex / pageSize) * pageSize),
  );
  useEffect(
    () => setStart(Math.max(0, Math.floor(targetIndex / pageSize) * pageSize)),
    [pageSize, targetIndex],
  );
  const boundedStart = Math.min(start, Math.max(0, items.length - 1));
  const end = Math.min(items.length, boundedStart + pageSize);
  const windowed = items.slice(boundedStart, end);
  return (
    <>
      {loaded.layout === "split" ? (
        <div className="split-panes">
          {(["old", "new"] as const).map((side) => (
            <div className={`split-pane split-pane-${side}`} key={side}>
              <div className="split-pane-content" role="list">
                {windowed.map((row, offset) => (
                  <SplitPaneRow
                    key={`${side}:${row.kind}:${row.file_index}:${"hunk_index" in row ? row.hunk_index : 0}:${boundedStart + offset}`}
                    review={review}
                    loaded={loaded}
                    row={row}
                    file={file}
                    side={side}
                    selection={selection}
                    selectAnchor={selectAnchor}
                  />
                ))}
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="windowed-items" role="list">
          {windowed.map((row, offset) => (
            <DiffRowView
              key={`${row.kind}:${row.file_index}:${"hunk_index" in row ? row.hunk_index : 0}:${boundedStart + offset}`}
              review={review}
              loaded={loaded}
              row={row}
              file={file}
              selection={selection}
              selectAnchor={selectAnchor}
            />
          ))}
        </div>
      )}
      {items.length > pageSize && (
        <nav className="window-controls" aria-label="Diff row windows">
          <button
            className="button button-secondary"
            disabled={boundedStart === 0}
            onClick={() => setStart(Math.max(0, boundedStart - pageSize))}
          >
            Previous rows
          </button>
          <span className="window-position">
            {boundedStart + 1}-{end} of {items.length}
          </span>
          <button
            className="button button-secondary"
            disabled={end === items.length}
            onClick={() => setStart(end)}
          >
            Next rows
          </button>
        </nav>
      )}
    </>
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

function DiffRowView({
  review,
  loaded,
  row,
  file,
  selection,
  selectAnchor,
}: {
  readonly review: string;
  readonly loaded: LoadedDiff;
  readonly row: DiffRow;
  readonly file: DiffFileRow | null;
  readonly selection: InlineAnchorSelection | null;
  readonly selectAnchor: (selection: InlineAnchorSelection | null) => void;
}): ReactNode {
  if (row.kind === "file") {
    const path =
      row.old_path === row.new_path
        ? row.new_path
        : `${row.old_path} → ${row.new_path}`;
    return (
      <header className="diff-file" role="listitem">
        <strong className="file-path">{path}</strong>
        <span className="file-status">{row.status}</span>
        <FileBadges file={row} />
      </header>
    );
  }
  if (row.kind === "hunk")
    return (
      <div className="diff-hunk" role="listitem">
        {row.header}
      </div>
    );
  if (row.kind === "split") return null;
  const selectable = row.line_type !== "no_newline" && file !== null;
  const defaultSide =
    row.line_type === "deletion"
      ? "old"
      : row.new_line !== null
        ? "new"
        : "old";
  const choose = (side: "old" | "new"): void => {
    if (!selectable || !file) return;
    selectAnchor(anchorForLine(review, loaded, file, row, side));
  };
  const selected =
    selection !== null &&
    selectionMatchesLine(selection, loaded, file, row, defaultSide);
  return (
    <div
      className={`diff-line line-${row.line_type}${selected ? " line-selected" : ""}`}
      role="listitem"
      data-selected-side={selected ? selection.side : undefined}
    >
      <LineNumberAnchor
        label="old"
        value={row.old_line}
        selected={
          selection !== null &&
          selectionMatchesLine(selection, loaded, file, row, "old")
        }
        selectable={selectable}
        choose={() => choose("old")}
      />
      <LineNumberAnchor
        label="new"
        value={row.new_line}
        selected={
          selection !== null &&
          selectionMatchesLine(selection, loaded, file, row, "new")
        }
        selectable={selectable}
        choose={() => choose("new")}
      />
      <code
        className={`line-content${selectable ? " selectable-line" : ""}`}
        role={selectable ? "button" : undefined}
        tabIndex={selectable ? 0 : undefined}
        aria-label={
          selectable
            ? `Select ${defaultSide} line ${defaultSide === "old" ? row.old_line : row.new_line}`
            : undefined
        }
        onClick={selectable ? () => choose(defaultSide) : undefined}
        onKeyDown={
          selectable
            ? (event) => activateOnKeyboard(event, () => choose(defaultSide))
            : undefined
        }
      >
        {marker(row.line_type)}
        {row.content}
      </code>
    </div>
  );
}

function LineNumberAnchor({
  label,
  value,
  selected,
  selectable,
  choose,
}: {
  readonly label: "old" | "new";
  readonly value: number | null;
  readonly selected: boolean;
  readonly selectable: boolean;
  readonly choose: () => void;
}): ReactNode {
  if (value === null || !selectable)
    return <span className="line-number">{value}</span>;
  return (
    <button
      className={`line-number line-anchor${selected ? " line-anchor-selected" : ""}`}
      aria-label={`Select ${label} line ${value}`}
      aria-pressed={selected}
      onClick={choose}
    >
      {value}
    </button>
  );
}

function SplitPaneRow({
  review,
  loaded,
  row,
  file,
  side,
  selection,
  selectAnchor,
}: {
  readonly review: string;
  readonly loaded: LoadedDiff;
  readonly row: DiffRow;
  readonly file: DiffFileRow | null;
  readonly side: "old" | "new";
  readonly selection: InlineAnchorSelection | null;
  readonly selectAnchor: (selection: InlineAnchorSelection | null) => void;
}): ReactNode {
  if (row.kind === "file")
    return (
      <header className="diff-file split-metadata" role="listitem">
        <strong className="file-path">
          {side === "old" ? row.old_path : row.new_path}
        </strong>
        <span className="file-status">{row.status}</span>
        <FileBadges file={row} />
      </header>
    );
  if (row.kind === "hunk")
    return (
      <div className="diff-hunk split-metadata" role="listitem">
        {row.header}
      </div>
    );
  if (row.kind !== "split") return null;
  return (
    <SplitCell
      review={review}
      loaded={loaded}
      row={row}
      file={file}
      cell={row[side]}
      visualSide={side}
      selection={selection}
      selectAnchor={selectAnchor}
    />
  );
}

function FileBadges({ file }: { readonly file: DiffFileRow }): ReactNode {
  return [
    [file.is_binary, "Binary"],
    [file.is_truncated, "Truncated"],
    [file.is_empty, "Empty"],
    [file.is_mode_only, "Mode only"],
    [file.is_unavailable, "Unavailable"],
  ].map(
    ([condition, label]) =>
      condition && (
        <span key={String(label)} className="badge">
          {label}
        </span>
      ),
  );
}

function SplitCell({
  review,
  loaded,
  row,
  file,
  cell,
  visualSide,
  selection,
  selectAnchor,
}: {
  readonly review: string;
  readonly loaded: LoadedDiff;
  readonly row: SplitDiffRow;
  readonly file: DiffFileRow | null;
  readonly cell: SplitDiffCell | null;
  readonly visualSide: "old" | "new";
  readonly selection: InlineAnchorSelection | null;
  readonly selectAnchor: (selection: InlineAnchorSelection | null) => void;
}): ReactNode {
  const selectable =
    cell?.anchor_side !== null && cell !== null && file !== null;
  const selected =
    selectable &&
    selection !== null &&
    selectionMatchesSplitCell(selection, loaded, file, row, cell);
  const choose = (): void => {
    if (!selectable || !cell || !file || !cell.anchor_side) return;
    selectAnchor(anchorForSplitCell(review, loaded, file, row, cell));
  };
  return (
    <div
      className={`split-cell split-${visualSide}${cell ? ` line-${cell.line_type}` : " split-empty"}${selectable ? " selectable-line" : ""}${selected ? " line-selected" : ""}`}
      role={selectable ? "button" : "listitem"}
      tabIndex={selectable ? 0 : undefined}
      aria-label={
        selectable && cell?.anchor_side
          ? `Select ${cell.anchor_side} line ${cell.anchor_side === "old" ? cell.old_line : cell.new_line}`
          : undefined
      }
      aria-pressed={selectable ? selected : undefined}
      data-anchor-side={cell?.anchor_side ?? undefined}
      data-old-line={cell?.old_line ?? undefined}
      data-new-line={cell?.new_line ?? undefined}
      onClick={selectable ? choose : undefined}
      onKeyDown={
        selectable ? (event) => activateOnKeyboard(event, choose) : undefined
      }
    >
      {cell && (
        <>
          <span className="line-number">
            {visualSide === "old" ? cell.old_line : cell.new_line}
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

function activateOnKeyboard(
  event: KeyboardEvent<HTMLElement>,
  choose: () => void,
): void {
  if (event.key !== "Enter" && event.key !== " ") return;
  event.preventDefault();
  choose();
}

function anchorForLine(
  review: string,
  loaded: LoadedDiff,
  file: DiffFileRow,
  row: Extract<DiffRow, { readonly kind: "line" }>,
  side: "old" | "new",
): InlineAnchorSelection {
  return Object.freeze({
    review,
    snapshotId: loaded.snapshotId,
    resource: loaded.resource,
    revision: loaded.revision,
    fileIndex: file.file_index,
    hunkIndex: row.hunk_index,
    rowIndex: null,
    oldPath: file.old_path,
    newPath: file.new_path,
    side,
    oldLine: row.old_line,
    newLine: row.new_line,
    lineType: row.line_type,
  });
}

function anchorForSplitCell(
  review: string,
  loaded: LoadedDiff,
  file: DiffFileRow,
  row: SplitDiffRow,
  cell: SplitDiffCell,
): InlineAnchorSelection {
  const side = cell.anchor_side;
  if (!side) throw new Error("Cannot select an unanchored split cell");
  return Object.freeze({
    review,
    snapshotId: loaded.snapshotId,
    resource: loaded.resource,
    revision: loaded.revision,
    fileIndex: file.file_index,
    hunkIndex: row.hunk_index,
    rowIndex: row.row_index,
    oldPath: file.old_path,
    newPath: file.new_path,
    side,
    oldLine: cell.old_line,
    newLine: cell.new_line,
    lineType: cell.line_type,
  });
}

export function rebindInlineAnchor(
  selection: InlineAnchorSelection,
  review: string,
  loaded: LoadedDiff,
): InlineAnchorSelection | null {
  if (
    selection.review !== review ||
    !sameRevisionValue(selection.revision, loaded.revision)
  )
    return null;
  const file = loaded.rows.find(
    (row): row is DiffFileRow =>
      row.kind === "file" &&
      row.old_path === selection.oldPath &&
      row.new_path === selection.newPath,
  );
  if (!file) return null;
  if (loaded.layout === "split") {
    for (const row of loaded.rows) {
      if (row.kind !== "split" || row.file_index !== file.file_index) continue;
      for (const cell of [row.old, row.new]) {
        if (
          cell?.anchor_side === selection.side &&
          cell.old_line === selection.oldLine &&
          cell.new_line === selection.newLine &&
          cell.line_type === selection.lineType
        )
          return anchorForSplitCell(review, loaded, file, row, cell);
      }
    }
    return null;
  }
  const row = loaded.rows.find(
    (candidate): candidate is Extract<DiffRow, { readonly kind: "line" }> =>
      candidate.kind === "line" &&
      candidate.file_index === file.file_index &&
      candidate.old_line === selection.oldLine &&
      candidate.new_line === selection.newLine &&
      candidate.line_type === selection.lineType &&
      candidate.line_type !== "no_newline" &&
      (selection.side === "old"
        ? candidate.old_line !== null
        : candidate.new_line !== null),
  );
  return row ? anchorForLine(review, loaded, file, row, selection.side) : null;
}

function selectionMatchesLine(
  selection: InlineAnchorSelection,
  loaded: LoadedDiff,
  file: DiffFileRow | null,
  row: Extract<DiffRow, { readonly kind: "line" }>,
  side: "old" | "new",
): boolean {
  return Boolean(
    file &&
      sameAnchorIdentity(
        selection,
        anchorForLine(selection.review, loaded, file, row, side),
      ),
  );
}

function selectionMatchesSplitCell(
  selection: InlineAnchorSelection,
  loaded: LoadedDiff,
  file: DiffFileRow | null,
  row: SplitDiffRow,
  cell: SplitDiffCell,
): boolean {
  return Boolean(
    file &&
      cell.anchor_side &&
      sameAnchorIdentity(
        selection,
        anchorForSplitCell(selection.review, loaded, file, row, cell),
      ),
  );
}

function sameAnchorIdentity(
  left: InlineAnchorSelection,
  right: InlineAnchorSelection,
): boolean {
  return (
    left.review === right.review &&
    left.snapshotId === right.snapshotId &&
    left.resource === right.resource &&
    sameRevisionValue(left.revision, right.revision) &&
    left.fileIndex === right.fileIndex &&
    left.hunkIndex === right.hunkIndex &&
    left.rowIndex === right.rowIndex &&
    left.oldPath === right.oldPath &&
    left.newPath === right.newPath &&
    left.side === right.side &&
    left.oldLine === right.oldLine &&
    left.newLine === right.newLine &&
    left.lineType === right.lineType
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
  return sameRevisionValue(left.revision, right.revision);
}
function sameRevisionValue(
  left: ReviewRevisionDto,
  right: ReviewRevisionDto,
): boolean {
  return (
    left.head_sha === right.head_sha &&
    left.base_sha === right.base_sha &&
    left.start_sha === right.start_sha
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
