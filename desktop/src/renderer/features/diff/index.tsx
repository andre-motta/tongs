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
  DiscussionDiffTarget,
  FeatureContribution,
  InlineAnchorSelection,
  InlineSelectedLine,
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
  const [jumpError, setJumpError] = useState<string | null>(null);
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
  useEffect(() => {
    if (!loaded || !route.diffTarget) return;
    const target = resolveDiscussionTarget(
      route.item.handle,
      loaded,
      route.diffTarget,
    );
    if (target) {
      setJumpError(null);
      selectInlineAnchor(target);
    } else {
      setJumpError(
        "This discussion location is unavailable in the displayed revision. Refresh the diff or open the discussion on the forge.",
      );
    }
  }, [loaded, route.diffTarget, route.item.handle, selectInlineAnchor]);
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
        <button
          className="button button-secondary"
          disabled={
            inlineAnchor?.review !== route.item.handle ||
            inlineAnchor.side !== "new" ||
            inlineAnchor.contextComplete !== true ||
            !inlineAnchor.selectedLines?.length
          }
          title="Select one or more contiguous new-side lines. Use Shift+click or Shift+Enter to extend the range."
          onClick={() => navigate({ ...route, panel: "discussions" })}
        >
          Suggest replacement
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
      {jumpError && <Notice kind="warning">{jumpError}</Notice>}
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
  useEffect(() => {
    if (!selection || selection.review !== review) return;
    const matchingFile = files.find(
      (file) => file.file_index === selection.fileIndex,
    );
    if (!matchingFile) return;
    const fileRows = loaded.rows.filter(
      (row) => row.file_index === matchingFile.file_index,
    );
    const rowIndex = fileRows.findIndex((row) =>
      loaded.layout === "split"
        ? row.kind === "split" && row.row_index === selection.rowIndex
        : row.kind === "line" &&
          row.old_line === selection.oldLine &&
          row.new_line === selection.newLine &&
          row.line_type === selection.lineType,
    );
    setSelected(matchingFile.file_index);
    if (rowIndex >= 0) setTarget(rowIndex);
  }, [files, loaded, review, selection]);
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
  const choose = (side: "old" | "new", extend = false): void => {
    if (!selectable || !file) return;
    selectAnchor(
      selectionForUnifiedLine(
        review,
        loaded,
        file,
        row,
        side,
        selection,
        extend,
      ),
    );
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
        choose={(extend) => choose("old", extend)}
      />
      <LineNumberAnchor
        label="new"
        value={row.new_line}
        selected={
          selection !== null &&
          selectionMatchesLine(selection, loaded, file, row, "new")
        }
        selectable={selectable}
        choose={(extend) => choose("new", extend)}
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
        onClick={selectable ? (event) => choose(defaultSide, event.shiftKey) : undefined}
        onKeyDown={
          selectable
            ? (event) =>
                activateOnKeyboard(event, () =>
                  choose(defaultSide, event.shiftKey),
                )
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
  readonly choose: (extend: boolean) => void;
}): ReactNode {
  if (value === null || !selectable)
    return <span className="line-number">{value}</span>;
  return (
    <button
      className={`line-number line-anchor${selected ? " line-anchor-selected" : ""}`}
      aria-label={`Select ${label} line ${value}`}
      aria-pressed={selected}
      onClick={(event) => choose(event.shiftKey)}
      onKeyDown={(event) =>
        activateOnKeyboard(event, () => choose(event.shiftKey))
      }
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
  const choose = (extend = false): void => {
    if (!selectable || !cell || !file || !cell.anchor_side) return;
    selectAnchor(
      selectionForSplitCell(
        review,
        loaded,
        file,
        row,
        cell,
        selection,
        extend,
      ),
    );
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
      onClick={selectable ? (event) => choose(event.shiftKey) : undefined}
      onKeyDown={
        selectable
          ? (event) => activateOnKeyboard(event, () => choose(event.shiftKey))
          : undefined
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

export function selectionForUnifiedLine(
  review: string,
  loaded: LoadedDiff,
  file: DiffFileRow,
  row: Extract<DiffRow, { readonly kind: "line" }>,
  side: "old" | "new",
  existing: InlineAnchorSelection | null = null,
  extend = false,
): InlineAnchorSelection {
  const candidates = unifiedSideRows(loaded, file, row.hunk_index, side);
  const targetIndex = candidates.indexOf(row);
  const originIndex = extend
    ? rangeOriginIndex(existing, review, loaded, file, row.hunk_index, side, candidates)
    : -1;
  const selected =
    targetIndex >= 0 && originIndex >= 0
      ? candidates.slice(
          Math.min(targetIndex, originIndex),
          Math.max(targetIndex, originIndex) + 1,
        )
      : [row];
  const origin = originIndex >= 0 ? candidates[originIndex]! : row;
  const context = unifiedRangeContext(loaded, file, selected, side);
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
    contextLines: context.lines,
    contextComplete: context.complete,
    rangeOriginOldLine: origin.old_line,
    rangeOriginNewLine: origin.new_line,
    selectedLines: Object.freeze(selected.map(selectedLine)),
  });
}

export function selectionForSplitCell(
  review: string,
  loaded: LoadedDiff,
  file: DiffFileRow,
  row: SplitDiffRow,
  cell: SplitDiffCell,
  existing: InlineAnchorSelection | null = null,
  extend = false,
): InlineAnchorSelection {
  const side = cell.anchor_side;
  if (!side) throw new Error("Cannot select an unanchored split cell");
  const candidates = splitSideRows(loaded, file, row.hunk_index, side);
  const targetIndex = candidates.findIndex((item) => item.row === row);
  const cells = candidates.map((item) => item.cell);
  const originIndex = extend
    ? rangeOriginIndex(existing, review, loaded, file, row.hunk_index, side, cells)
    : -1;
  const selected =
    targetIndex >= 0 && originIndex >= 0
      ? candidates.slice(
          Math.min(targetIndex, originIndex),
          Math.max(targetIndex, originIndex) + 1,
        )
      : [{ row, cell }];
  const origin = originIndex >= 0 ? candidates[originIndex]!.cell : cell;
  const context = splitRangeContext(loaded, file, selected, side);
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
    contextLines: context.lines,
    contextComplete: context.complete,
    rangeOriginOldLine: origin.old_line,
    rangeOriginNewLine: origin.new_line,
    selectedLines: Object.freeze(selected.map((item) => selectedLine(item.cell))),
  });
}

type SourceRow = Extract<DiffRow, { readonly kind: "line" }>;

function selectedLine(line: {
  readonly old_line: number | null;
  readonly new_line: number | null;
  readonly line_type: string;
  readonly content: string;
}): InlineSelectedLine {
  return Object.freeze({
    oldLine: line.old_line,
    newLine: line.new_line,
    lineType: line.line_type,
    content: line.content,
  });
}

function unifiedSideRows(
  loaded: LoadedDiff,
  file: DiffFileRow,
  hunkIndex: number,
  side: "old" | "new",
): SourceRow[] {
  return loaded.rows.filter(
    (candidate): candidate is SourceRow =>
      candidate.kind === "line" &&
      candidate.file_index === file.file_index &&
      candidate.hunk_index === hunkIndex &&
      candidate.line_type !== "no_newline" &&
      (side === "old"
        ? candidate.old_line !== null
        : candidate.new_line !== null),
  );
}

function splitSideRows(
  loaded: LoadedDiff,
  file: DiffFileRow,
  hunkIndex: number,
  side: "old" | "new",
): { readonly row: SplitDiffRow; readonly cell: SplitDiffCell }[] {
  return loaded.rows
    .filter(
      (candidate): candidate is SplitDiffRow =>
        candidate.kind === "split" &&
        candidate.file_index === file.file_index &&
        candidate.hunk_index === hunkIndex,
    )
    .map((candidate) => ({ row: candidate, cell: candidate[side] }))
    .filter(
      (item): item is { readonly row: SplitDiffRow; readonly cell: SplitDiffCell } =>
        item.cell !== null &&
        item.cell.line_type !== "no_newline" &&
        (side === "old"
          ? item.cell.old_line !== null
          : item.cell.new_line !== null),
    );
}

function rangeOriginIndex<T extends {
  readonly old_line: number | null;
  readonly new_line: number | null;
}>(
  existing: InlineAnchorSelection | null,
  review: string,
  loaded: LoadedDiff,
  file: DiffFileRow,
  hunkIndex: number,
  side: "old" | "new",
  candidates: readonly T[],
): number {
  if (
    !existing ||
    existing.review !== review ||
    existing.snapshotId !== loaded.snapshotId ||
    existing.resource !== loaded.resource ||
    !sameRevisionValue(existing.revision, loaded.revision) ||
    existing.fileIndex !== file.file_index ||
    existing.hunkIndex !== hunkIndex ||
    existing.side !== side
  )
    return -1;
  const oldLine =
    existing.rangeOriginOldLine !== undefined
      ? existing.rangeOriginOldLine
      : existing.oldLine;
  const newLine =
    existing.rangeOriginNewLine !== undefined
      ? existing.rangeOriginNewLine
      : existing.newLine;
  return candidates.findIndex(
    (candidate) =>
      candidate.old_line === oldLine && candidate.new_line === newLine,
  );
}

function unifiedRangeContext(
  loaded: LoadedDiff,
  file: DiffFileRow,
  selected: readonly SourceRow[],
  side: "old" | "new",
): SourceContext {
  const first = selected[0];
  const last = selected.at(-1);
  if (!first || !last)
    return Object.freeze({ lines: Object.freeze([]), complete: false });
  const candidates = unifiedSideRows(loaded, file, first.hunk_index, side);
  const firstIndex = candidates.indexOf(first);
  const lastIndex = candidates.indexOf(last);
  if (firstIndex < 0 || lastIndex < 0)
    return Object.freeze({ lines: Object.freeze([]), complete: false });
  return Object.freeze({
    lines: Object.freeze(
      candidates
        .slice(Math.max(0, firstIndex - 2), lastIndex + 3)
        .map((candidate) => candidate.content),
    ),
    complete: contextIsComplete(loaded, file),
  });
}

function splitRangeContext(
  loaded: LoadedDiff,
  file: DiffFileRow,
  selected: readonly { readonly row: SplitDiffRow; readonly cell: SplitDiffCell }[],
  side: "old" | "new",
): SourceContext {
  const first = selected[0];
  const last = selected.at(-1);
  if (!first || !last)
    return Object.freeze({ lines: Object.freeze([]), complete: false });
  const candidates = splitSideRows(loaded, file, first.row.hunk_index, side);
  const firstIndex = candidates.findIndex((item) => item.row === first.row);
  const lastIndex = candidates.findIndex((item) => item.row === last.row);
  if (firstIndex < 0 || lastIndex < 0)
    return Object.freeze({ lines: Object.freeze([]), complete: false });
  return Object.freeze({
    lines: Object.freeze(
      candidates
        .slice(Math.max(0, firstIndex - 2), lastIndex + 3)
        .map((item) => item.cell.content),
    ),
    complete: contextIsComplete(loaded, file),
  });
}

export interface SourceContext {
  readonly lines: readonly string[];
  readonly complete: boolean;
}

export function unifiedSourceContext(
  loaded: LoadedDiff,
  file: DiffFileRow,
  target: Extract<DiffRow, { readonly kind: "line" }>,
  side: "old" | "new",
): SourceContext {
  const candidates = unifiedSideRows(loaded, file, target.hunk_index, side);
  const index = candidates.indexOf(target);
  if (index < 0) return Object.freeze({ lines: Object.freeze([]), complete: false });
  return Object.freeze({
    lines: Object.freeze(
      candidates
        .slice(Math.max(0, index - 2), index + 3)
        .map((row) => row.content),
    ),
    complete: contextIsComplete(loaded, file),
  });
}

export function splitSourceContext(
  loaded: LoadedDiff,
  file: DiffFileRow,
  target: SplitDiffRow,
  side: "old" | "new",
): SourceContext {
  const candidates = splitSideRows(loaded, file, target.hunk_index, side);
  const index = candidates.findIndex((item) => item.row === target);
  if (index < 0) return Object.freeze({ lines: Object.freeze([]), complete: false });
  return Object.freeze({
    lines: Object.freeze(
      candidates
        .slice(Math.max(0, index - 2), index + 3)
        .map((item) => item.cell.content),
    ),
    complete: contextIsComplete(loaded, file),
  });
}

function contextIsComplete(loaded: LoadedDiff, file: DiffFileRow): boolean {
  return (
    loaded.partialError === null &&
    !file.is_truncated &&
    !file.is_unavailable
  );
}

export function resolveDiscussionTarget(
  review: string,
  loaded: LoadedDiff,
  target: DiscussionDiffTarget,
): InlineAnchorSelection | null {
  if (!Number.isInteger(target.line) || target.line <= 0) return null;
  const file = loaded.rows.find(
    (row): row is DiffFileRow =>
      row.kind === "file" &&
      (target.side === "new"
        ? row.new_path === target.path
        : row.old_path === target.path),
  );
  if (
    !file ||
    file.is_binary ||
    file.is_truncated ||
    file.is_unavailable ||
    file.is_empty ||
    file.is_mode_only
  )
    return null;
  if (loaded.layout === "split") {
    for (const row of loaded.rows) {
      if (row.kind !== "split" || row.file_index !== file.file_index) continue;
      const cell = row[target.side];
      if (
        cell?.anchor_side === target.side &&
        (target.side === "new" ? cell.new_line : cell.old_line) === target.line
      )
        return selectionForSplitCell(review, loaded, file, row, cell);
    }
    return null;
  }
  const row = loaded.rows.find(
    (candidate): candidate is SourceRow =>
      candidate.kind === "line" &&
      candidate.file_index === file.file_index &&
      candidate.line_type !== "no_newline" &&
      (target.side === "new" ? candidate.new_line : candidate.old_line) ===
        target.line,
  );
  return row
    ? selectionForUnifiedLine(review, loaded, file, row, target.side)
    : null;
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
    const target = loaded.rows.find(
      (row): row is SplitDiffRow =>
        row.kind === "split" &&
        row.file_index === file.file_index &&
        [row.old, row.new].some(
          (cell) =>
            cell?.anchor_side === selection.side &&
            cell.old_line === selection.oldLine &&
            cell.new_line === selection.newLine &&
            cell.line_type === selection.lineType,
        ),
    );
    if (!target) return null;
    const cell = target[selection.side];
    if (!cell || cell.anchor_side !== selection.side) return null;
    const origin = splitSideRows(
      loaded,
      file,
      target.hunk_index,
      selection.side,
    ).find(
      (item) =>
        item.cell.old_line ===
          (selection.rangeOriginOldLine !== undefined
            ? selection.rangeOriginOldLine
            : selection.oldLine) &&
        item.cell.new_line ===
          (selection.rangeOriginNewLine !== undefined
            ? selection.rangeOriginNewLine
            : selection.newLine),
    );
    if (!origin) return null;
    const base = selectionForSplitCell(
      review,
      loaded,
      file,
      origin.row,
      origin.cell,
    );
    const rebound = selectionForSplitCell(
      review,
      loaded,
      file,
      target,
      cell,
      base,
      Boolean(selection.selectedLines && selection.selectedLines.length > 1),
    );
    return sameSelectedSource(selection, rebound) ? rebound : null;
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
  if (!row) return null;
  const origin = unifiedSideRows(
    loaded,
    file,
    row.hunk_index,
    selection.side,
  ).find(
    (candidate) =>
      candidate.old_line ===
        (selection.rangeOriginOldLine !== undefined
          ? selection.rangeOriginOldLine
          : selection.oldLine) &&
      candidate.new_line ===
        (selection.rangeOriginNewLine !== undefined
          ? selection.rangeOriginNewLine
          : selection.newLine),
  );
  if (!origin) return null;
  const base = selectionForUnifiedLine(
    review,
    loaded,
    file,
    origin,
    selection.side,
  );
  const rebound = selectionForUnifiedLine(
    review,
    loaded,
    file,
    row,
    selection.side,
    base,
    Boolean(selection.selectedLines && selection.selectedLines.length > 1),
  );
  return sameSelectedSource(selection, rebound) ? rebound : null;
}

function selectionMatchesLine(
  selection: InlineAnchorSelection,
  loaded: LoadedDiff,
  file: DiffFileRow | null,
  row: Extract<DiffRow, { readonly kind: "line" }>,
  side: "old" | "new",
): boolean {
  if (
    !file ||
    selection.side !== side ||
    !selectionBelongsToLoadedFile(selection, loaded, file)
  )
    return false;
  if (selection.selectedLines)
    return selection.selectedLines.some(
      (line) =>
        line.oldLine === row.old_line &&
        line.newLine === row.new_line &&
        line.lineType === row.line_type &&
        line.content === row.content,
    );
  return sameAnchorIdentity(
    selection,
    selectionForUnifiedLine(selection.review, loaded, file, row, side),
  );
}

function selectionMatchesSplitCell(
  selection: InlineAnchorSelection,
  loaded: LoadedDiff,
  file: DiffFileRow | null,
  row: SplitDiffRow,
  cell: SplitDiffCell,
): boolean {
  if (
    !file ||
    cell.anchor_side !== selection.side ||
    !selectionBelongsToLoadedFile(selection, loaded, file)
  )
    return false;
  if (selection.selectedLines)
    return selection.selectedLines.some(
      (line) =>
        line.oldLine === cell.old_line &&
        line.newLine === cell.new_line &&
        line.lineType === cell.line_type &&
        line.content === cell.content,
    );
  return sameAnchorIdentity(
    selection,
    selectionForSplitCell(selection.review, loaded, file, row, cell),
  );
}

function selectionBelongsToLoadedFile(
  selection: InlineAnchorSelection,
  loaded: LoadedDiff,
  file: DiffFileRow,
): boolean {
  return (
    selection.snapshotId === loaded.snapshotId &&
    selection.resource === loaded.resource &&
    sameRevisionValue(selection.revision, loaded.revision) &&
    selection.fileIndex === file.file_index &&
    selection.oldPath === file.old_path &&
    selection.newPath === file.new_path
  );
}

function sameSelectedSource(
  left: InlineAnchorSelection,
  right: InlineAnchorSelection,
): boolean {
  if (!left.selectedLines) return true;
  return Boolean(
    right.selectedLines &&
      left.selectedLines.length === right.selectedLines.length &&
      left.selectedLines.every((line, index) => {
        const candidate = right.selectedLines![index];
        return (
          candidate !== undefined &&
          line.oldLine === candidate.oldLine &&
          line.newLine === candidate.newLine &&
          line.lineType === candidate.lineType &&
          line.content === candidate.content
        );
      }),
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
    left.lineType === right.lineType &&
    left.contextComplete === right.contextComplete &&
    left.rangeOriginOldLine === right.rangeOriginOldLine &&
    left.rangeOriginNewLine === right.rangeOriginNewLine &&
    sameSelectedSource(left, right) &&
    left.contextLines.length === right.contextLines.length &&
    left.contextLines.every((line, index) => line === right.contextLines[index])
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
