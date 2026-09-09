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
  DiffFileRow,
  DiffHunkRow,
  DiffLayout,
  DiffPage,
  DiffRow,
  RepositoryDto,
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
import {
  RendererReadError,
  safeError,
  serviceErrorOf,
} from "../../core/presentation.js";
import { QueryCoordinator } from "../../core/query.js";
import {
  InlineComposer,
  anchorIdentity,
  useInlineReviewComposer,
  type InlineComposerController,
} from "../review/composer.js";
import {
  ReviewDrawerMount,
  peekPendingEdit,
  requestDrawerOpen,
  requestPendingEdit,
  pendingEntryTarget,
  subscribePendingEdit,
  takePendingEdit,
  useReviewDrawer,
} from "../review/drawer.js";
import {
  REVIEW_ROW_ATTRIBUTE,
  focusedReviewRow,
  pendingRowKey,
  pressComposerPrimary,
  reviewDrawerIsOpen,
  stepReviewRow,
  threadRowDiscussion,
  threadRowKey,
  useReviewKeyMap,
} from "../review/keys.js";
import {
  PendingCard,
  PendingCardMirror,
  type PendingDraftEntry,
} from "../review/pending-card.js";
import {
  DiffThread,
  DiffThreadMirror,
  threadFileCountText,
  threadFileCounts,
  threadRowClassName,
  threadRowExpanded,
  threadsOnLine,
  useDiscussionThreads,
  type AnchoredThread,
  type DiscussionThreadSlot,
} from "../review/thread.js";
import { ReviewHeader } from "../review-detail/index.js";

const MAX_DIFF_PAGES = 1000;
const MAX_DIFF_ROWS = 100_000;

/**
 * One in-diff composer is open at a time. It is bound to the anchor it was
 * opened with, so extending or moving the line selection never retargets the
 * composer that is already collecting text.
 */
export interface InlineComposerSlot {
  readonly controller: InlineComposerController;
  readonly anchor: InlineAnchorSelection | null;
  /**
   * The pending entry the open composer is editing, or null when it is
   * collecting a new comment. The card for that entry gives way to the
   * composer, so the entry is never shown twice.
   */
  readonly editing: PendingDraftEntry | null;
  /**
   * `keepSelection` leaves the app-owned line selection alone, which is what a
   * row inside an existing multi-line selection needs: card #188 composes on a
   * single line, and discarding the range the reader built with the shipped
   * shift-click affordance would be a regression while #189 is pending.
   */
  readonly open: (
    anchor: InlineAnchorSelection,
    keepSelection?: boolean,
    entry?: PendingDraftEntry | null,
  ) => void;
  readonly close: () => void;
}

/** The row a drag started on, or the row it is being extended to. */
export interface DragRow {
  readonly side: "old" | "new";
  readonly hunkIndex: number;
  readonly oldLine: number | null;
  readonly newLine: number | null;
}

/**
 * A press on a line number starts a drag that selects the contiguous range it
 * covers on that one side of that one hunk. The drag lives on the workspace so
 * a release anywhere ends it, and it remembers the row it last extended to so
 * that a pointer moving inside one row does not rebuild the selection on every
 * event.
 */
export interface LineRangeDrag {
  readonly begin: (row: DragRow) => void;
  readonly extendTo: (row: DragRow, extend: () => void) => void;
}

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
          repositories={context.repositories}
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
  repositories,
  inlineAnchor,
  selectInlineAnchor,
}: {
  readonly bridge: DesktopBridge;
  readonly queries: QueryCoordinator;
  readonly route: Extract<AppRoute, { kind: "review" }>;
  readonly navigate: (route: AppRoute) => void;
  readonly panels: FeatureContributionParameters["reviewPanels"];
  readonly repositories: readonly RepositoryDto[];
  readonly inlineAnchor: InlineAnchorSelection | null;
  readonly selectInlineAnchor: (
    selection: InlineAnchorSelection | null,
  ) => void;
}): ReactNode {
  // The suggestion block syntax is the forge's, so the composer needs to know
  // which forge this review lives on.
  const forge =
    repositories.find(
      (repository) => repository.handle === route.item.repository,
    )?.forge_type ?? null;
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
      <ReviewHeader
        route={route}
        navigate={navigate}
        panels={panels}
        drawer={
          loaded ? (
            <DiffReviewDrawer
              bridge={bridge}
              review={route.item.handle}
              forge={forge}
              revision={loaded.revision}
              jumpTo={(target) => navigate({ ...route, diffTarget: target })}
            />
          ) : null
        }
      />
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
      {jumpError && <Notice kind="warning">{jumpError}</Notice>}
      {loaded && (
        <DiffWorkspace
          bridge={bridge}
          review={route.item.handle}
          forge={forge}
          loaded={loaded}
          reloadToken={reload}
          selection={inlineAnchor}
          selectAnchor={selectInlineAnchor}
        />
      )}
    </>
  );
}

/**
 * The review drawer as the Changes tab mounts it. It is a component of its own
 * because the controller needs a revision, and the revision is only known once
 * the diff has loaded. Jump reuses the discussion jump path exactly: the
 * drawer hands over the stored anchor's own path, side and line, and
 * `resolveDiscussionTarget` turns them into a selection the same way it does
 * for a discussion, so no new coordinate rule is introduced here.
 */
function DiffReviewDrawer({
  bridge,
  review,
  forge,
  revision,
  jumpTo,
}: {
  readonly bridge: DesktopBridge;
  readonly review: string;
  readonly forge: RepositoryDto["forge_type"] | null;
  readonly revision: DiffPage["revision"];
  readonly jumpTo: (target: DiscussionDiffTarget) => void;
}): ReactNode {
  const controller = useReviewDrawer(bridge, review, revision, forge);
  const target = (entry: PendingDraftEntry): DiscussionDiffTarget | null => {
    const anchored = pendingEntryTarget(entry);
    return anchored
      ? { discussionId: `pending:${entry.id}`, ...anchored }
      : null;
  };
  return (
    <ReviewDrawerMount
      controller={controller}
      openExternal={(url) => bridge.openExternal(url)}
      jump={(entry) => {
        const to = target(entry);
        if (to) jumpTo(to);
      }}
      edit={(entry) => {
        requestPendingEdit(review, entry.id);
        const to = target(entry);
        if (to) jumpTo(to);
      }}
    />
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
  // A diff-owned code, so the diff reload advice stays on the diff surface.
  return new RendererReadError("invalid_diff_page", message, false);
}

function paginationLimit(message: string): RendererReadError {
  return new RendererReadError("pagination_limit", message, false);
}

function invalidatesSelection(error: unknown): boolean {
  const failure = serviceErrorOf(error);
  return (
    failure !== null &&
    (failure.code === "snapshot_expired" || failure.code === "revision_changed")
  );
}

type FeatureContributionParameters = Parameters<
  FeatureContribution["render"]
>[0];

function DiffWorkspace({
  bridge,
  review,
  forge,
  loaded,
  reloadToken,
  selection,
  selectAnchor,
}: {
  readonly bridge: DesktopBridge;
  readonly review: string;
  readonly forge: RepositoryDto["forge_type"] | null;
  readonly loaded: LoadedDiff;
  /** Bumped by the toolbar's Refresh, so the published threads are reread too. */
  readonly reloadToken: number;
  readonly selection: InlineAnchorSelection | null;
  readonly selectAnchor: (selection: InlineAnchorSelection | null) => void;
}): ReactNode {
  const files = useMemo(
    () => loaded.rows.filter((row): row is DiffFileRow => row.kind === "file"),
    [loaded.rows],
  );
  const [selected, setSelected] = useState(files[0]?.file_index ?? 0);
  const [target, setTarget] = useState(0);
  const [composerAnchor, setComposerAnchor] =
    useState<InlineAnchorSelection | null>(null);
  const [editingEntry, setEditingEntry] = useState<PendingDraftEntry | null>(
    null,
  );
  const composerController = useInlineReviewComposer(
    bridge,
    review,
    loaded.revision,
    forge,
  );
  const threads = useDiscussionThreads(
    bridge,
    review,
    composerController,
    reloadToken,
  );
  const [dragging, setDragging] = useState(false);
  const dragged = useRef<DragRow | null>(null);
  // A drag can end anywhere, including outside the diff, so the release is
  // watched on the document only while a drag is actually running.
  useEffect(() => {
    if (!dragging) return;
    const release = (): void => {
      dragged.current = null;
      setDragging(false);
    };
    document.addEventListener("mouseup", release);
    return () => document.removeEventListener("mouseup", release);
  }, [dragging]);
  // A reloaded diff can retire the row the composer is anchored to. Only an
  // anchor that no longer belongs to the loaded diff is dropped, and its typed
  // text stays in the per-anchor buffer either way.
  useEffect(() => {
    if (
      composerAnchor === null ||
      anchorBelongsToLoadedDiff(composerAnchor, review, loaded)
    )
      return;
    setComposerAnchor(null);
    setEditingEntry(null);
  }, [composerAnchor, loaded, review]);
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
  // The entry the composer is editing is re-read from the controller on every
  // render, so a save that rewrote the pending list cannot leave the composer
  // holding a body the store no longer has.
  const editing =
    editingEntry === null
      ? null
      : (composerController.pending.find(
          (entry) => entry.id === editingEntry.id,
        ) ?? null);
  // A pending entry that leaves the review while its editor is open takes the
  // editor with it, rather than turning into a composer for a new comment on
  // the same line.
  useEffect(() => {
    if (
      editingEntry === null ||
      composerController.pending.some((entry) => entry.id === editingEntry.id)
    )
      return;
    setComposerAnchor(null);
    setEditingEntry(null);
  }, [composerController.pending, editingEntry]);
  // An Edit pressed in the drawer names an entry, not a row: the drawer can be
  // open over any panel, and the row the entry sits on may not be loaded yet.
  // The request is claimed only once the entry is in the adopted draft and its
  // line resolves in the loaded diff, so an Edit pressed before the diff is
  // ready opens the editor when it becomes ready rather than doing nothing. A
  // request that names an entry this review no longer holds is dropped, so it
  // cannot reopen on some later diff.
  useEffect(() => {
    const claim = (): void => {
      const entryId = peekPendingEdit(review);
      if (entryId === null) return;
      const entry = composerController.pending.find(
        (item) => item.id === entryId,
      );
      if (!entry) {
        if (composerController.pending.length > 0) takePendingEdit(review);
        return;
      }
      const anchored = pendingEntryTarget(entry);
      const resolved = anchored
        ? resolveDiscussionTarget(review, loaded, {
            discussionId: `pending:${entry.id}`,
            ...anchored,
          })
        : null;
      if (!resolved) {
        takePendingEdit(review);
        return;
      }
      takePendingEdit(review);
      composerController.clearMessage();
      // The selection the reader built is left alone, exactly as the in-diff
      // card's own Edit leaves it, and the entry keeps the anchor it was
      // saved with.
      setComposerAnchor(resolved);
      setEditingEntry(entry);
    };
    claim();
    return subscribePendingEdit(claim);
  }, [composerController, loaded, review]);
  const inline: InlineComposerSlot = {
    controller: composerController,
    anchor: composerAnchor,
    editing,
    open: (anchor, keepSelection = false, entry = null) => {
      composerController.clearMessage();
      if (!keepSelection) selectAnchor(anchor);
      setComposerAnchor(anchor);
      setEditingEntry(entry);
    },
    close: () => {
      composerController.clearMessage();
      setComposerAnchor(null);
      setEditingEntry(null);
    },
  };
  const drag: LineRangeDrag = {
    begin: (row) => {
      dragged.current = row;
      setDragging(true);
    },
    extendTo: (row, extend) => {
      const from = dragged.current;
      // A range lives on one side of one hunk, because that is the only shape
      // the anchor can carry. An overshoot into the next hunk or the other
      // side stops the range where it is instead of collapsing it to the row
      // the pointer happens to be over.
      if (!from || from.side !== row.side || from.hunkIndex !== row.hunkIndex)
        return;
      if (from.oldLine === row.oldLine && from.newLine === row.newLine) return;
      dragged.current = row;
      extend();
    },
  };
  // The review keyboard map (design 2.8). The region is the routed content
  // around the workspace, not the workspace itself, so a key typed with the
  // focus on the layout toggle or the changed-file navigation is answered the
  // same way as one typed with nothing focused at all. The rows container is
  // held separately because it is what `n`, `p`, `r` and the composer's
  // primary action reach into.
  const [workspaceNode, setWorkspaceNode] = useState<HTMLElement | null>(null);
  const [rowsNode, setRowsNode] = useState<HTMLElement | null>(null);
  const rowCursor = useRef<string | null>(null);
  const stepFile = (direction: 1 | -1): boolean => {
    // A review with one file has no next file, so the key stays unclaimed
    // rather than scrolling the one file back to its top.
    if (files.length < 2) return false;
    const at = files.findIndex((file) => file.file_index === selected);
    const from = at < 0 ? 0 : at;
    const next = files[(from + direction + files.length) % files.length];
    if (!next) return false;
    setSelected(next.file_index);
    setTarget(0);
    // The changed-file navigation drops the composer and the selection when it
    // moves to another file, and never when it stays; the key does exactly the
    // same, so a review with one file is not reset by a press that goes
    // nowhere.
    if (next.file_index !== selected) {
      setComposerAnchor(null);
      selectAnchor(null);
    }
    return true;
  };
  const stepRow = (direction: 1 | -1): boolean => {
    const step = stepReviewRow(rowsNode, direction, rowCursor.current);
    rowCursor.current = step.key;
    return step.moved;
  };
  const composeOnSelection = (): boolean => {
    // A row that holds the focus answers `c` itself, with the row's own
    // handler. This is the same key from the body: the anchor is the selection
    // the reader built, and a range composes on the whole range exactly as the
    // gutter affordance does.
    if (!selection || selection.review !== review) return false;
    const file = files.find((row) => row.file_index === selection.fileIndex);
    if (!file) return false;
    const range =
      (selection.selectedLines?.length ?? 0) > 1
        ? rangeAnchorForSelection(selection, review, loaded, file)
        : null;
    inline.open(range ?? selection, true);
    return true;
  };
  const replyToFocusedThread = (): boolean => {
    const discussion = threadRowDiscussion(
      focusedReviewRow(rowsNode) ?? rowCursor.current,
    );
    if (discussion === null) return false;
    // The key carries the same refusals the Reply button carries, rather than
    // opening a composer the thread would not take.
    if (threads.replyReason(discussion) !== null) return false;
    threads.openReply(discussion);
    return true;
  };
  const closeComposer = (): boolean => {
    // Escape used to be bound to the non-focusable `.diff-view` section, so it
    // did nothing once the focus left the diff rows. The composer's own staged
    // Escape still answers first whenever the focus is inside it (an armed
    // discard, then the overflow, then the composer), because that handler
    // calls `preventDefault` and this map never answers a key twice. The text
    // stays either way: it lives in the per-anchor buffer, not in the composer.
    if (composerAnchor === null) return false;
    inline.close();
    return true;
  };
  useReviewKeyMap(
    workspaceNode?.parentElement ?? workspaceNode,
    [
      { key: "c", run: composeOnSelection },
      {
        key: "C",
        shift: true,
        run: () => requestDrawerOpen(review),
      },
      { key: "]", run: () => stepFile(1) },
      { key: "[", run: () => stepFile(-1) },
      { key: "n", run: () => stepRow(1) },
      { key: "p", run: () => stepRow(-1) },
      { key: "r", run: replyToFocusedThread },
      { key: "Enter", primary: true, run: () => pressComposerPrimary(rowsNode) },
      { key: "Escape", run: closeComposer },
    ],
    // The review drawer is a dialog over the diff. It is not `aria-modal`,
    // because the diff behind it stays readable, but it does own the keyboard
    // while it is open: `v` and `Esc` are its keys, and a `n` typed with the
    // focus in the diff behind it must not move the diff underneath. The
    // drawer's own map claims on this same predicate, so everything this one
    // stands down for is offered there.
    { standDown: reviewDrawerIsOpen },
  );
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
    <div className="diff-workspace" ref={setWorkspaceNode}>
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
              if (fileIndex !== selected) {
                setComposerAnchor(null);
                selectAnchor(null);
              }
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
        ref={setRowsNode}
      >
        {threads.loadError && (
          <Notice kind="warning">{threads.loadError}</Notice>
        )}
        {/*
          A refusal raised by an Edit or a Delete pressed on a pending card is
          the controller's, and the composer used to be the only thing that
          rendered it, so a press made with no composer open was refused in
          silence. The drawer made that ordinary: it writes the same draft, so
          a save in flight there refuses a press here. The composer still shows
          its own copy while it is open, so this stands in only when it is not.
        */}
        {composerAnchor === null && composerController.message !== null && (
          <Notice kind="error">
            <span>{composerController.message}</span>
            <button
              className="button button-secondary notice-action"
              onClick={composerController.clearMessage}
            >
              Dismiss
            </button>
          </Notice>
        )}
        <DiffRowsWindow
          review={review}
          loaded={loaded}
          items={visible}
          pageSize={200}
          targetIndex={target}
          file={selectedFile}
          selection={selection}
          selectAnchor={selectAnchor}
          inline={inline}
          drag={drag}
          pending={composerController.pending}
          threads={threads}
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
  inline,
  drag,
  pending,
  threads,
}: {
  readonly review: string;
  readonly loaded: LoadedDiff;
  readonly items: readonly DiffRow[];
  readonly pageSize: number;
  readonly targetIndex: number;
  readonly file: DiffFileRow | null;
  readonly selection: InlineAnchorSelection | null;
  readonly selectAnchor: (selection: InlineAnchorSelection | null) => void;
  readonly inline: InlineComposerSlot;
  readonly drag: LineRangeDrag;
  readonly pending: readonly PendingDraftEntry[];
  readonly threads: DiscussionThreadSlot;
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
  // The card that spells the refusal out is chosen from the cards this window
  // actually paints, in the order the reader meets them. Choosing by stored
  // order instead let the sentence vanish whenever the file's first entry was
  // paged out of the window or was the entry being edited.
  const explains = firstRenderedPendingEntry(
    pending,
    loaded,
    file,
    windowed,
    inline.editing,
  );
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
                    inline={inline}
                    drag={drag}
                    pending={pending}
                    explains={explains}
                    threads={threads}
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
              inline={inline}
              drag={drag}
              pending={pending}
              explains={explains}
              threads={threads}
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
  inline,
  drag,
  pending,
  explains,
  threads,
}: {
  readonly review: string;
  readonly loaded: LoadedDiff;
  readonly row: DiffRow;
  readonly file: DiffFileRow | null;
  readonly selection: InlineAnchorSelection | null;
  readonly selectAnchor: (selection: InlineAnchorSelection | null) => void;
  readonly inline: InlineComposerSlot;
  readonly drag: LineRangeDrag;
  readonly pending: readonly PendingDraftEntry[];
  readonly explains: string | null;
  readonly threads: DiscussionThreadSlot;
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
        <PendingFileCount pending={pending} file={row} />
        <ThreadFileCount threads={threads} file={row} />
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
  const compose = (side: "old" | "new"): void => {
    if (!selectable || !file) return;
    // A row inside a multi-line selection composes on the whole range: the
    // composer claims it, and the app-owned selection is left as the reader
    // built it.
    const range =
      selection !== null &&
      (selection.selectedLines?.length ?? 0) > 1 &&
      selectionMatchesLine(selection, loaded, file, row, side)
        ? rangeAnchorForSelection(selection, review, loaded, file)
        : null;
    inline.open(
      range ?? selectionForUnifiedLine(review, loaded, file, row, side),
      range !== null,
    );
  };
  const selected =
    selection !== null &&
    selectionMatchesLine(selection, loaded, file, row, defaultSide);
  const composerOpen =
    inline.anchor !== null &&
    composerOnUnifiedRow(inline.anchor, loaded, file, row);
  const entries = pendingOnUnifiedRow(pending, file, row);
  const anchoredThreads = threadsOnUnifiedRow(threads, file, row);
  const editEntry = (entry: PendingDraftEntry): void => {
    if (!file || !entry.anchor) return;
    // The composer takes the row the card sits on, which is the line the
    // stored anchor names. Nothing is recaptured: the entry keeps the anchor
    // it was saved with, and the selection the reader built is left alone.
    inline.open(
      selectionForUnifiedLine(review, loaded, file, row, entry.anchor.side),
      true,
      entry,
    );
  };
  return (
    <>
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
          drag={drag}
          hunkIndex={row.hunk_index}
          oldLine={row.old_line}
          newLine={row.new_line}
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
          drag={drag}
          hunkIndex={row.hunk_index}
          oldLine={row.old_line}
          newLine={row.new_line}
        />
        <GutterComment
          selectable={selectable}
          side={defaultSide}
          line={defaultSide === "old" ? row.old_line : row.new_line}
          compose={() => compose(defaultSide)}
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
              ? (event) => {
                  if (isCommentKey(event)) {
                    event.preventDefault();
                    compose(defaultSide);
                    return;
                  }
                  activateOnKeyboard(event, () =>
                    choose(defaultSide, event.shiftKey),
                  );
                }
              : undefined
          }
        >
          {marker(row.line_type)}
          {row.content}
        </code>
      </div>
      {composerOpen && inline.anchor && (
        <div className="inline-composer-row" role="listitem">
          <InlineComposer
            key={composerKey(inline)}
            anchor={inline.anchor}
            controller={inline.controller}
            entry={inline.editing}
            close={inline.close}
          />
        </div>
      )}
      {anchoredThreads.map((thread) => (
        <div
          className={threadRowClassName(threads, thread)}
          role="listitem"
          key={thread.discussion.id}
          {...{ [REVIEW_ROW_ATTRIBUTE]: threadRowKey(thread.discussion.id) }}
        >
          <DiffThread thread={thread} slot={threads} />
        </div>
      ))}
      {entries.map((entry) =>
        inline.editing?.id === entry.id ? null : (
          <div
            className="pending-card-row"
            role="listitem"
            key={entry.id}
            {...{ [REVIEW_ROW_ATTRIBUTE]: pendingRowKey(entry.id) }}
          >
            <PendingCard
              entry={entry}
              reason={inline.controller.entryReason}
              explain={entry.id === explains}
              busy={inline.controller.busy}
              openExternal={inline.controller.openExternal}
              edit={() => editEntry(entry)}
              remove={() => void inline.controller.removeEntry(entry.id)}
            />
          </div>
        ),
      )}
    </>
  );
}

/**
 * The composer keeps the text it is collecting in its own state, so it has to
 * be remounted whenever what it is composing changes. A new comment and one or
 * more pending cards share a row, and Edit swaps the composer's shape without
 * moving it, so without this key React reconciles the two and one comment's
 * unsent text is saved into another.
 */
function composerKey(slot: InlineComposerSlot): string {
  return slot.editing
    ? `edit:${slot.editing.id}`
    : `new:${anchorIdentity(slot.anchor)}`;
}

/**
 * "N pending" in the file header, counting the entries anchored to this file.
 * The composer chip counts the whole review, so the header says which scope it
 * is naming.
 */
function PendingFileCount({
  pending,
  file,
}: {
  readonly pending: readonly PendingDraftEntry[];
  readonly file: DiffFileRow;
}): ReactNode {
  const count = pendingOnFile(pending, file).length;
  if (count === 0) return null;
  return (
    <span
      className="file-pending"
      aria-label={`${count} pending review comments on this file`}
    >
      {count} pending
    </span>
  );
}

/**
 * "N threads, M unresolved" in the file header, over the threads anchored
 * anywhere in this file rather than over the ones the window paints. The
 * header is what tells the reader there is more to page to.
 */
function ThreadFileCount({
  threads,
  file,
}: {
  readonly threads: DiscussionThreadSlot;
  readonly file: DiffFileRow;
}): ReactNode {
  const counts = threadFileCounts(threads, file.old_path, file.new_path);
  if (counts.threads === 0) return null;
  const text = threadFileCountText(counts);
  return (
    <span className="file-threads" aria-label={`${text} on this file`}>
      {text}
    </span>
  );
}

/**
 * The published threads that belong under one unified row. A unified row can
 * carry both sides of the diff, so both sides are read, new side first, which
 * is the order the file's own columns are in.
 */
function threadsOnUnifiedRow(
  threads: DiscussionThreadSlot,
  file: DiffFileRow | null,
  row: Extract<DiffRow, { readonly kind: "line" }>,
): readonly AnchoredThread[] {
  if (!file) return EMPTY_THREADS;
  const onNew = threadsOnLine(threads, file.new_path, "new", row.new_line);
  const onOld = threadsOnLine(threads, file.old_path, "old", row.old_line);
  if (onOld.length === 0) return onNew;
  if (onNew.length === 0) return onOld;
  return [...onNew, ...onOld];
}

/** The same match on a split row, read through the cell each side owns. */
function threadsOnSplitRow(
  threads: DiscussionThreadSlot,
  file: DiffFileRow | null,
  row: SplitDiffRow,
): readonly AnchoredThread[] {
  if (!file) return EMPTY_THREADS;
  const onNew =
    row.new?.anchor_side === "new"
      ? threadsOnLine(threads, file.new_path, "new", row.new.new_line)
      : EMPTY_THREADS;
  const onOld =
    row.old?.anchor_side === "old"
      ? threadsOnLine(threads, file.old_path, "old", row.old.old_line)
      : EMPTY_THREADS;
  if (onOld.length === 0) return onNew;
  if (onNew.length === 0) return onOld;
  return [...onNew, ...onOld];
}

const EMPTY_THREADS: readonly AnchoredThread[] = Object.freeze([]);

/**
 * The pending entry that carries the once-per-file refusal sentence: the first
 * one this window paints, skipping the entry whose card has given way to the
 * composer that is editing it. A sentence chosen by stored order disappeared
 * whenever that entry was outside the window or under edit, which is the S44
 * refusal going silent on a read-only review.
 */
function firstRenderedPendingEntry(
  pending: readonly PendingDraftEntry[],
  loaded: LoadedDiff,
  file: DiffFileRow | null,
  windowed: readonly DiffRow[],
  editing: PendingDraftEntry | null,
): string | null {
  if (!file || pending.length === 0) return null;
  for (const row of windowed) {
    const entries =
      loaded.layout === "split"
        ? row.kind === "split"
          ? pendingOnSplitRow(pending, file, row)
          : []
        : row.kind === "line"
          ? pendingOnUnifiedRow(pending, file, row)
          : [];
    for (const entry of entries)
      if (entry.id !== editing?.id) return entry.id;
  }
  return null;
}

/** The pending entries whose stored anchor names this file. */
function pendingOnFile(
  pending: readonly PendingDraftEntry[],
  file: DiffFileRow,
): readonly PendingDraftEntry[] {
  return pending.filter(
    (entry) =>
      entry.anchor !== null &&
      entry.anchor.old_path === file.old_path &&
      entry.anchor.new_path === file.new_path,
  );
}

/**
 * The entries that belong under one unified row. A range entry is anchored on
 * the line that closes it, so its card lands under the end of the range, which
 * is where the composer that wrote it stood.
 */
function pendingOnUnifiedRow(
  pending: readonly PendingDraftEntry[],
  file: DiffFileRow | null,
  row: Extract<DiffRow, { readonly kind: "line" }>,
): readonly PendingDraftEntry[] {
  if (!file) return [];
  return pendingOnFile(pending, file).filter((entry) => {
    const anchor = entry.anchor!;
    return anchor.side === "old"
      ? row.old_line !== null && row.old_line === anchor.old_line
      : row.new_line !== null && row.new_line === anchor.new_line;
  });
}

/** The same match on a split row, read through the cell the anchor names. */
function pendingOnSplitRow(
  pending: readonly PendingDraftEntry[],
  file: DiffFileRow | null,
  row: SplitDiffRow,
): readonly PendingDraftEntry[] {
  if (!file) return [];
  return pendingOnFile(pending, file).filter((entry) => {
    const anchor = entry.anchor!;
    const cell = row[anchor.side];
    if (!cell || cell.anchor_side !== anchor.side) return false;
    return anchor.side === "old"
      ? cell.old_line !== null && cell.old_line === anchor.old_line
      : cell.new_line !== null && cell.new_line === anchor.new_line;
  });
}

/**
 * The gutter affordance the design puts on every source row. It stays in the
 * grid when a row cannot be commented on, so the code column never shifts.
 */
function GutterComment({
  selectable,
  side,
  line,
  compose,
}: {
  readonly selectable: boolean;
  readonly side: "old" | "new";
  readonly line: number | null;
  readonly compose: () => void;
}): ReactNode {
  if (!selectable || line === null)
    return <span className="gutter-comment gutter-comment-empty" />;
  return (
    <button
      className="gutter-comment"
      aria-label={`Comment on ${side} line ${line}`}
      title="Comment on this line"
      onClick={(event) => {
        // The split cell is itself a select control, so the press has to stop
        // here: letting it bubble would re-select this one row and collapse
        // the range the composer just claimed.
        event.stopPropagation();
        compose();
      }}
      onKeyDown={(event) => {
        // The same containment for the keyboard path. Enter and Space on this
        // button used to reach the split cell's own handler, which calls
        // `preventDefault` and re-selects the one row: the range collapsed,
        // and the suppressed default meant the activation click was never
        // synthesised either, so the composer did not open at all. The press
        // is carried out here instead of being left to that synthesis, which
        // is how the row and the cell already answer their own keys.
        if (event.key !== "Enter" && event.key !== " ") return;
        if (event.ctrlKey || event.metaKey || event.altKey) return;
        event.preventDefault();
        event.stopPropagation();
        compose();
      }}
    >
      +
    </button>
  );
}

/** The design's keyboard equivalent of the gutter affordance (section 2.8). */
function isCommentKey(event: KeyboardEvent<HTMLElement>): boolean {
  return (
    event.key === "c" &&
    !event.ctrlKey &&
    !event.metaKey &&
    !event.altKey &&
    !event.shiftKey
  );
}

function LineNumberAnchor({
  label,
  value,
  selected,
  selectable,
  choose,
  drag,
  hunkIndex,
  oldLine,
  newLine,
}: {
  readonly label: "old" | "new";
  readonly value: number | null;
  readonly selected: boolean;
  readonly selectable: boolean;
  readonly choose: (extend: boolean) => void;
  readonly drag: LineRangeDrag;
  readonly hunkIndex: number;
  readonly oldLine: number | null;
  readonly newLine: number | null;
}): ReactNode {
  const dragRow: DragRow = { side: label, hunkIndex, oldLine, newLine };
  if (value === null || !selectable)
    return <span className="line-number">{value}</span>;
  return (
    <button
      className={`line-number line-anchor${selected ? " line-anchor-selected" : ""}`}
      aria-label={`Select ${label} line ${value}`}
      aria-pressed={selected}
      onMouseDown={(event) => {
        if (event.button !== 0) return;
        // Suppresses the native text selection the drag would otherwise paint
        // over the diff, and takes back the focus that suppression drops. A
        // Shift press keeps the range origin it extends from.
        event.preventDefault();
        event.currentTarget.focus();
        choose(event.shiftKey);
        drag.begin(dragRow);
      }}
      onMouseOver={(event) => {
        // Only a held primary button extends. A release the document listener
        // never saw cannot leave a plain hover rebuilding the selection.
        if ((event.buttons & 1) !== 1) return;
        drag.extendTo(dragRow, () => choose(true));
      }}
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
  inline,
  drag,
  pending,
  explains,
  threads,
}: {
  readonly review: string;
  readonly loaded: LoadedDiff;
  readonly row: DiffRow;
  readonly file: DiffFileRow | null;
  readonly side: "old" | "new";
  readonly selection: InlineAnchorSelection | null;
  readonly selectAnchor: (selection: InlineAnchorSelection | null) => void;
  readonly inline: InlineComposerSlot;
  readonly drag: LineRangeDrag;
  readonly pending: readonly PendingDraftEntry[];
  readonly explains: string | null;
  readonly threads: DiscussionThreadSlot;
}): ReactNode {
  if (row.kind === "file")
    return (
      <header className="diff-file split-metadata" role="listitem">
        <strong className="file-path">
          {side === "old" ? row.old_path : row.new_path}
        </strong>
        <span className="file-status">{row.status}</span>
        <FileBadges file={row} />
        {side === "new" && <PendingFileCount pending={pending} file={row} />}
        {side === "new" && <ThreadFileCount threads={threads} file={row} />}
      </header>
    );
  if (row.kind === "hunk")
    return (
      <div className="diff-hunk split-metadata" role="listitem">
        {row.header}
      </div>
    );
  if (row.kind !== "split") return null;
  const composerOpen =
    inline.anchor !== null &&
    composerOnSplitRow(inline.anchor, loaded, file, row);
  // Both panes read the same entry list for the same row, so the pane that
  // does not own the anchor side renders one spacer per card and the two
  // independent pane grids stay on the same rows.
  const entries = pendingOnSplitRow(pending, file, row);
  // Both panes read the same thread list for the same row, and the pane that
  // does not own the anchored side renders one spacer per thread, so the two
  // grids stay on the same rows however many threads a line carries.
  const anchoredThreads = threadsOnSplitRow(threads, file, row);
  const editEntry = (entry: PendingDraftEntry): void => {
    const cell = entry.anchor ? row[entry.anchor.side] : null;
    if (!file || !entry.anchor || !cell) return;
    inline.open(
      selectionForSplitCell(review, loaded, file, row, cell),
      true,
      entry,
    );
  };
  return (
    <>
      <SplitCell
        review={review}
        loaded={loaded}
        row={row}
        file={file}
        cell={row[side]}
        visualSide={side}
        selection={selection}
        selectAnchor={selectAnchor}
        inline={inline}
        drag={drag}
      />
      {composerOpen &&
        inline.anchor &&
        (inline.anchor.side === side ? (
          <div className="inline-composer-row" role="listitem">
            <InlineComposer
              key={composerKey(inline)}
              anchor={inline.anchor}
              controller={inline.controller}
              entry={inline.editing}
              close={inline.close}
            />
          </div>
        ) : (
          // The panes are two independent columns, so the pane that does not
          // hold the composer keeps its rows aligned with a spacer of the same
          // fixed height.
          <div
            className="inline-composer-mirror"
            role="presentation"
            aria-hidden="true"
          />
        ))}
      {anchoredThreads.map((thread) =>
        thread.target.side === side ? (
          <div
            className={threadRowClassName(threads, thread)}
            role="listitem"
            key={thread.discussion.id}
            {...{ [REVIEW_ROW_ATTRIBUTE]: threadRowKey(thread.discussion.id) }}
          >
            <DiffThread thread={thread} slot={threads} />
          </div>
        ) : (
          <DiffThreadMirror
            key={thread.discussion.id}
            expanded={threadRowExpanded(threads, thread)}
          />
        ),
      )}
      {entries.map((entry) =>
        inline.editing?.id === entry.id ? null : entry.anchor?.side === side ? (
          <div
            className="pending-card-row"
            role="listitem"
            key={entry.id}
            {...{ [REVIEW_ROW_ATTRIBUTE]: pendingRowKey(entry.id) }}
          >
            <PendingCard
              entry={entry}
              reason={inline.controller.entryReason}
              explain={entry.id === explains}
              busy={inline.controller.busy}
              openExternal={inline.controller.openExternal}
              edit={() => editEntry(entry)}
              remove={() => void inline.controller.removeEntry(entry.id)}
            />
          </div>
        ) : (
          <PendingCardMirror key={entry.id} />
        ),
      )}
    </>
  );
}

// The sidecar derives every shape the forge payload determines, so each badge
// states a fact about the file.  The last entry is the honest residue: GitHub's
// files endpoint withholds the patch for binary content and for a mode-only
// change without distinguishing them, so the badge names the forge as the limit
// instead of implying the client failed to load the file.
const FILE_BADGES: ReadonlyArray<
  readonly [(file: DiffFileRow) => boolean, string, string?]
> = [
  [(file) => file.is_binary, "Binary"],
  [(file) => file.is_truncated, "Truncated"],
  [(file) => file.is_empty, "Empty"],
  [(file) => file.is_mode_only, "Mode only"],
  [(file) => file.is_rename_only, "Rename only"],
  [
    (file) => file.is_unavailable,
    "Not exposed by forge",
    "The forge sent no diff content for this file and did not report whether it is a binary or a mode-only change.",
  ],
];

function FileBadges({ file }: { readonly file: DiffFileRow }): ReactNode {
  return FILE_BADGES.map(
    ([matches, label, hint]) =>
      matches(file) && (
        <span key={label} className="badge" title={hint}>
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
  inline,
  drag,
}: {
  readonly review: string;
  readonly loaded: LoadedDiff;
  readonly row: SplitDiffRow;
  readonly file: DiffFileRow | null;
  readonly cell: SplitDiffCell | null;
  readonly visualSide: "old" | "new";
  readonly selection: InlineAnchorSelection | null;
  readonly selectAnchor: (selection: InlineAnchorSelection | null) => void;
  readonly inline: InlineComposerSlot;
  readonly drag: LineRangeDrag;
}): ReactNode {
  const cellNode = useRef<HTMLDivElement | null>(null);
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
  const draggable = selectable && cell !== null && cell.anchor_side !== null;
  const dragRow: DragRow | null = draggable
    ? {
        side: cell.anchor_side,
        hunkIndex: row.hunk_index,
        oldLine: cell.old_line,
        newLine: cell.new_line,
      }
    : null;
  const compose = (): void => {
    if (!selectable || !cell || !file || !cell.anchor_side) return;
    const range =
      selection !== null &&
      (selection.selectedLines?.length ?? 0) > 1 &&
      selectionMatchesSplitCell(selection, loaded, file, row, cell)
        ? rangeAnchorForSelection(selection, review, loaded, file)
        : null;
    inline.open(
      range ?? selectionForSplitCell(review, loaded, file, row, cell),
      range !== null,
    );
  };
  return (
    <div
      ref={cellNode}
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
          ? (event) => {
              if (isCommentKey(event)) {
                event.preventDefault();
                compose();
                return;
              }
              activateOnKeyboard(event, () => choose(event.shiftKey));
            }
          : undefined
      }
    >
      {cell && (
        <>
          <span
            className="line-number"
            // The drag affordance is the number column on both layouts
            // (design 2.1). Binding it to the whole cell would suppress the
            // native text selection over the code column, which is how a
            // reader copies a line out of the diff.
            onMouseDown={
              draggable
                ? (event) => {
                    if (event.button !== 0) return;
                    event.preventDefault();
                    cellNode.current?.focus();
                    choose(event.shiftKey);
                    drag.begin(dragRow!);
                  }
                : undefined
            }
            onMouseOver={
              draggable
                ? (event) => {
                    if ((event.buttons & 1) !== 1) return;
                    drag.extendTo(dragRow!, () => choose(true));
                  }
                : undefined
            }
          >
            {visualSide === "old" ? cell.old_line : cell.new_line}
          </span>
          <GutterComment
            selectable={selectable && cell.anchor_side === visualSide}
            side={visualSide}
            line={visualSide === "old" ? cell.old_line : cell.new_line}
            compose={compose}
          />
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
  // Shift extends the range, so it stays part of a plain activation. Ctrl or
  // Cmd does not: that combination is the composer's primary action in the
  // review keyboard map, and a row that answered it would select itself and
  // swallow the write.
  if (event.ctrlKey || event.metaKey || event.altKey) return;
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

/**
 * The selection's span, re-anchored on its last line with its origin on the
 * first, whichever way the reader built it. Every layer under the composer
 * reads the anchored line as the end of a range and `start_line` as its
 * opening, so a drag upward must not hand them a reversed pair, and the
 * composer row belongs under the bottom of the span either way.
 */
function rangeAnchorForSelection(
  selection: InlineAnchorSelection,
  review: string,
  loaded: LoadedDiff,
  file: DiffFileRow,
): InlineAnchorSelection | null {
  // `selectedLines` is always a slice of the side's rows in document order
  // (`selectionForUnifiedLine`, `selectionForSplitCell`), so the first entry
  // opens the span and the last one closes it. `rangeEndpoints` in the
  // composer orders by line number and agrees with this for the same reason.
  const lines = selection.selectedLines ?? [];
  const opening = lines[0];
  const closing = lines.at(-1);
  if (lines.length < 2 || !opening || !closing) return null;
  if (loaded.layout === "split") {
    const cells = splitSideRows(
      loaded,
      file,
      selection.hunkIndex,
      selection.side,
    );
    const first = cells.find((item) => sameSelectedLine(item.cell, opening));
    const last = cells.find((item) => sameSelectedLine(item.cell, closing));
    if (!first || !last) return null;
    const base = selectionForSplitCell(
      review,
      loaded,
      file,
      first.row,
      first.cell,
    );
    return selectionForSplitCell(
      review,
      loaded,
      file,
      last.row,
      last.cell,
      base,
      true,
    );
  }
  const rows = unifiedSideRows(
    loaded,
    file,
    selection.hunkIndex,
    selection.side,
  );
  const first = rows.find((row) => sameSelectedLine(row, opening));
  const last = rows.find((row) => sameSelectedLine(row, closing));
  if (!first || !last) return null;
  const base = selectionForUnifiedLine(
    review,
    loaded,
    file,
    first,
    selection.side,
  );
  return selectionForUnifiedLine(
    review,
    loaded,
    file,
    last,
    selection.side,
    base,
    true,
  );
}

function sameSelectedLine(
  row: {
    readonly old_line: number | null;
    readonly new_line: number | null;
    readonly line_type: string;
    readonly content: string;
  },
  line: InlineSelectedLine,
): boolean {
  return (
    row.old_line === line.oldLine &&
    row.new_line === line.newLine &&
    row.line_type === line.lineType &&
    row.content === line.content
  );
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
    file.is_mode_only ||
    // A rename-only file projects no source rows, so no discussion line can
    // resolve into it. It is deliberately absent from `contextIsComplete`:
    // that flag means the held content is partial, which a rename is not.
    file.is_rename_only
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

function anchorBelongsToLoadedDiff(
  anchor: InlineAnchorSelection,
  review: string,
  loaded: LoadedDiff,
): boolean {
  return (
    anchor.review === review &&
    anchor.snapshotId === loaded.snapshotId &&
    anchor.resource === loaded.resource &&
    sameRevisionValue(anchor.revision, loaded.revision)
  );
}

function composerOnUnifiedRow(
  anchor: InlineAnchorSelection,
  loaded: LoadedDiff,
  file: DiffFileRow | null,
  row: Extract<DiffRow, { readonly kind: "line" }>,
): boolean {
  return (
    file !== null &&
    anchor.rowIndex === null &&
    selectionBelongsToLoadedFile(anchor, loaded, file) &&
    anchor.hunkIndex === row.hunk_index &&
    anchor.oldLine === row.old_line &&
    anchor.newLine === row.new_line &&
    anchor.lineType === row.line_type
  );
}

function composerOnSplitRow(
  anchor: InlineAnchorSelection,
  loaded: LoadedDiff,
  file: DiffFileRow | null,
  row: SplitDiffRow,
): boolean {
  return (
    file !== null &&
    anchor.rowIndex === row.row_index &&
    selectionBelongsToLoadedFile(anchor, loaded, file) &&
    anchor.hunkIndex === row.hunk_index
  );
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
