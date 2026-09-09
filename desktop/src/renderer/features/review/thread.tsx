import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import type {
  DesktopRead,
  DiscussionDto,
  DiscussionsResult,
} from "../../../shared/bridge.js";
import type { DiscussionDiffTarget } from "../../core/navigation.js";
import {
  SafeMarkdown,
  safeMarkdownPresentationBytes,
} from "../../core/safe-markdown.js";
import { buffersFor, type InlineComposerController } from "./composer.js";

/**
 * The whole-review budget the discussion surfaces share. It lives here with
 * the allocator so the Discussions panel and the in-diff threads spend one
 * budget in one order, and the same discussion is allocated the same way on
 * both surfaces.
 */
const DISCUSSION_MARKDOWN_BUDGET_BYTES = 256 * 1024;

export interface DiscussionMarkdownAllocation {
  readonly root: boolean;
  readonly replies: readonly boolean[];
}

export function allocateDiscussionMarkdown(
  discussions: readonly DiscussionDto[],
): readonly DiscussionMarkdownAllocation[] {
  let remaining = DISCUSSION_MARKDOWN_BUDGET_BYTES;
  let exhausted = false;
  return Object.freeze(
    discussions.map((discussion) => {
      const root = allocate(discussion.root_comment.body);
      const replies = discussion.root_comment.replies.map((reply) =>
        allocate(reply.body),
      );
      return Object.freeze({ root, replies: Object.freeze(replies) });
    }),
  );

  function allocate(source: string): boolean {
    if (exhausted) return false;
    const bytes = safeMarkdownPresentationBytes(source);
    if (bytes > remaining) {
      exhausted = true;
      remaining = 0;
      return false;
    }
    remaining -= bytes;
    return true;
  }
}

export function DiscussionMarkdownBody({
  allocated,
  body,
  openExternal,
}: {
  readonly allocated: boolean;
  readonly body: string;
  readonly openExternal: (url: string) => Promise<boolean>;
}): ReactNode {
  return allocated ? (
    <SafeMarkdown openExternal={openExternal} source={body} />
  ) : (
    <p className="safe-markdown-aggregate-omission" role="status">
      Markdown omitted because the discussion display budget was exhausted. Open
      this review on the forge to read the complete discussion.
    </p>
  );
}

export function discussionDiffTarget(
  discussion: DiscussionDto,
): DiscussionDiffTarget | null {
  const comment = discussion.root_comment;
  if (!discussion.is_inline || !comment.file_path) return null;
  if (Number.isInteger(comment.new_line) && (comment.new_line ?? 0) > 0)
    return Object.freeze({
      discussionId: discussion.id,
      path: comment.file_path,
      side: "new",
      line: comment.new_line!,
    });
  if (Number.isInteger(comment.old_line) && (comment.old_line ?? 0) > 0)
    return Object.freeze({
      discussionId: discussion.id,
      path: comment.file_path,
      side: "old",
      line: comment.old_line!,
    });
  return null;
}

/**
 * A published discussion together with the diff line it is anchored to, and
 * its place among the threads that share that line and side. The place is what
 * lets two threads on one line carry names a reader can tell apart.
 */
export interface AnchoredThread {
  readonly discussion: DiscussionDto;
  readonly target: DiscussionDiffTarget;
  readonly ordinal: number;
  readonly total: number;
}

/** What a file header states about the threads anchored inside that file. */
export interface ThreadFileCounts {
  readonly threads: number;
  readonly unresolved: number;
}

const NO_THREADS: readonly AnchoredThread[] = Object.freeze([]);
const NO_COUNTS: ThreadFileCounts = Object.freeze({
  threads: 0,
  unresolved: 0,
});

/**
 * The key a thread is placed by. It is the same triple `DiscussionDiffTarget`
 * carries, so a row finds its threads with one map read instead of a scan over
 * every discussion, which is what keeps a 200-thread review windowed.
 */
export function threadKey(
  path: string,
  side: "old" | "new",
  line: number,
): string {
  return `${path}\u0000${side}\u0000${line}`;
}

/** How the collapsed row states what the thread holds. */
export function threadSummaryText(discussion: DiscussionDto): string {
  const replies = discussion.root_comment.replies;
  const count = replies.length === 1 ? "1 reply" : `${replies.length} replies`;
  const last = replies.at(-1) ?? discussion.root_comment;
  return `${count}, ${discussion.is_resolved ? "resolved" : "unresolved"}, last by @${last.author.username}`;
}

/** How the file header states its thread totals. */
export function threadFileCountText(counts: ThreadFileCounts): string {
  return `${counts.threads} ${counts.threads === 1 ? "thread" : "threads"}, ${counts.unresolved} unresolved`;
}

/** Where a thread sits, worded the way the composer words the same line. */
export function threadAnchorLabel(target: DiscussionDiffTarget): string {
  return `${target.side} line ${target.line}`;
}

/**
 * How a thread names itself. One thread on a line is named by the line alone,
 * as the composer names it; a line carrying several is named by its place in
 * them, because the line no longer identifies which thread is being answered.
 */
export function threadDescriptor(thread: AnchoredThread): string {
  const where = threadAnchorLabel(thread.target);
  return thread.total > 1
    ? `thread ${thread.ordinal} of ${thread.total} on ${where}`
    : `thread on ${where}`;
}

/** The same identity, worded for the controls that act on the thread. */
export function threadReference(thread: AnchoredThread): string {
  const where = threadAnchorLabel(thread.target);
  return thread.total > 1
    ? `discussion ${thread.ordinal} of ${thread.total} on ${where}`
    : `the discussion on ${where}`;
}

export interface DiscussionThreadBridge {
  listDiscussions(review: string): DesktopRead<DiscussionsResult>;
  cancelRead(requestToken: string): Promise<boolean>;
}

/**
 * Everything a diff row needs to place, expand and answer the published
 * threads of one review. Expansion lives here rather than inside a row so the
 * two split panes read one answer and their independent grids stay aligned.
 */
export interface DiscussionThreadSlot {
  readonly review: string;
  /** Why the published discussions are missing, or null when they are held. */
  readonly loadError: string | null;
  /**
   * The sentence one thread is showing, or null. It is kept per thread and
   * rendered on the thread's own row rather than inside the reply composer,
   * because the composer closes on a published reply and the sentence that
   * reports what happened afterwards has to outlive it.
   */
  readonly notice: (discussionId: string) => string | null;
  /** Whether a pending review is open, so the composer can say it publishes now. */
  readonly pendingReview: boolean;
  readonly byLine: ReadonlyMap<string, readonly AnchoredThread[]>;
  readonly byPath: ReadonlyMap<string, ThreadFileCounts>;
  readonly allocations: ReadonlyMap<string, DiscussionMarkdownAllocation>;
  readonly expanded: (discussionId: string) => boolean;
  readonly toggle: (discussionId: string) => void;
  readonly composing: (discussionId: string) => boolean;
  readonly openReply: (discussionId: string) => void;
  readonly closeReply: (discussionId: string) => void;
  /** Why this thread cannot take a reply now, or null when it can. */
  readonly replyReason: (discussionId: string) => string | null;
  /** Why this thread cannot change its resolution now, or null when it can. */
  readonly resolveReason: (discussion: DiscussionDto) => string | null;
  readonly reply: (
    thread: AnchoredThread,
    body: string,
    alsoResolve: boolean,
  ) => Promise<void>;
  readonly openExternal: (url: string) => Promise<boolean>;
}

export const THREAD_READ_REFUSAL =
  "The published discussions could not be read, so this diff shows no threads. Refresh the diff or open the review on the forge.";

/**
 * Said when the reread that follows a published reply fails. The thread is then
 * showing replies that are known to be out of date, and the same sentence
 * stands in for Reply on that thread, because pressing it again is how the
 * reader would publish the reply twice (S83).
 */
const THREAD_REFRESH_REFUSAL =
  "Rereading the discussions failed, so this thread is out of date. Refresh the diff before replying again.";

/** The prefix that keeps a post-reply refusal from reading as a failed reply. */
const REPLY_PUBLISHED = "The reply was published.";

const RESOLVE_UNSUPPORTED = "Resolution is unsupported for this discussion.";

/**
 * Said in the composer while a review is pending. A thread reply is published
 * at once rather than added to the review, which is the one place in the diff
 * where the primary action does not queue, so it is stated before typing
 * (design 2.1) rather than discovered afterwards.
 */
export const THREAD_REPLY_IMMEDIATE_NOTE =
  "This reply publishes immediately. It is not added to your pending review.";

/** The threads of a file, counted over the whole file rather than the window. */
export function threadFileCounts(
  slot: DiscussionThreadSlot,
  oldPath: string,
  newPath: string,
): ThreadFileCounts {
  const fromNew = slot.byPath.get(newPath) ?? NO_COUNTS;
  if (oldPath === newPath) return fromNew;
  const fromOld = slot.byPath.get(oldPath) ?? NO_COUNTS;
  return Object.freeze({
    threads: fromNew.threads + fromOld.threads,
    unresolved: fromNew.unresolved + fromOld.unresolved,
  });
}

/** The threads anchored to one line of one side, in stored order. */
export function threadsOnLine(
  slot: DiscussionThreadSlot,
  path: string,
  side: "old" | "new",
  line: number | null,
): readonly AnchoredThread[] {
  if (line === null) return NO_THREADS;
  return slot.byLine.get(threadKey(path, side, line)) ?? NO_THREADS;
}

/** A sentence, bound to the one thread that asked for the write it reports. */
interface ThreadNotice {
  readonly discussionId: string;
  readonly text: string;
}

export function useDiscussionThreads(
  bridge: DiscussionThreadBridge,
  review: string,
  composer: InlineComposerController,
  /**
   * Bumped whenever the diff itself is reloaded, so pressing Refresh rereads
   * the published discussions too. That is what makes "Refresh the diff" a
   * true instruction after a reread failed.
   */
  reloadToken: number,
): DiscussionThreadSlot {
  const [threads, setThreads] = useState<readonly DiscussionDto[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [expandedIds, setExpandedIds] = useState<ReadonlySet<string>>(
    () => new Set<string>(),
  );
  const [composingId, setComposingId] = useState<string | null>(null);
  const [notice, setNotice] = useState<ThreadNotice | null>(null);
  // Threads whose published reply could not be reread. They are known to be
  // out of date, so they refuse another reply rather than let one be written
  // twice against a state nobody has seen.
  const [staleIds, setStaleIds] = useState<ReadonlySet<string>>(
    () => new Set<string>(),
  );
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);
  useEffect(() => {
    let current = true;
    let token: string | null = null;
    setThreads([]);
    setLoadError(null);
    setExpandedIds(new Set<string>());
    setComposingId(null);
    setNotice(null);
    setStaleIds(new Set<string>());
    try {
      const read = bridge.listDiscussions(review);
      token = read.requestToken;
      void read.result.then(
        (result) => {
          if (!current) return;
          setThreads(result.discussions);
          setLoadError(null);
        },
        () => {
          if (current) setLoadError(THREAD_READ_REFUSAL);
        },
      );
    } catch {
      setLoadError(THREAD_READ_REFUSAL);
    }
    return () => {
      current = false;
      if (token === null) return;
      try {
        void bridge.cancelRead(token).catch(() => undefined);
      } catch {
        // A cancellation that cannot be delivered must not fail the unmount.
      }
    };
  }, [bridge, reloadToken, review]);

  const anchored = useMemo(() => {
    const grouped = new Map<
      string,
      { readonly discussion: DiscussionDto; readonly target: DiscussionDiffTarget }[]
    >();
    const byPath = new Map<string, ThreadFileCounts>();
    for (const discussion of threads) {
      const target = discussionDiffTarget(discussion);
      if (!target) continue;
      const key = threadKey(target.path, target.side, target.line);
      const held = grouped.get(key);
      if (held) held.push({ discussion, target });
      else grouped.set(key, [{ discussion, target }]);
      const counts = byPath.get(target.path) ?? NO_COUNTS;
      byPath.set(target.path, {
        threads: counts.threads + 1,
        unresolved: counts.unresolved + (discussion.is_resolved ? 0 : 1),
      });
    }
    // The place inside a group is only known once the group is complete, so it
    // is stamped on here rather than while the groups are being filled.
    const byLine = new Map<string, readonly AnchoredThread[]>();
    for (const [key, group] of grouped)
      byLine.set(
        key,
        Object.freeze(
          group.map((entry, index) =>
            Object.freeze({
              ...entry,
              ordinal: index + 1,
              total: group.length,
            }),
          ),
        ),
      );
    return Object.freeze({
      byLine: byLine as ReadonlyMap<string, readonly AnchoredThread[]>,
      byPath: byPath as ReadonlyMap<string, ThreadFileCounts>,
    });
  }, [threads]);

  // The allocation is computed over every discussion of the review, in stored
  // order, so a discussion that is displayed on both the Discussions panel and
  // the diff is allocated identically on the two surfaces.
  const allocations = useMemo(() => {
    const allocated = allocateDiscussionMarkdown(threads);
    const byId = new Map<string, DiscussionMarkdownAllocation>();
    threads.forEach((discussion, index) => {
      const allocation = allocated[index];
      if (allocation) byId.set(discussion.id, allocation);
    });
    return byId as ReadonlyMap<string, DiscussionMarkdownAllocation>;
  }, [threads]);

  const refresh = useCallback(async (): Promise<boolean> => {
    let token: string | null = null;
    try {
      const read = bridge.listDiscussions(review);
      token = read.requestToken;
      const result = await read.result;
      if (live.current) setThreads(result.discussions);
      return true;
    } catch {
      return false;
    } finally {
      if (!live.current && token !== null) {
        try {
          void bridge.cancelRead(token).catch(() => undefined);
        } catch {
          // Same as the mount read: a cancellation is best effort.
        }
      }
    }
  }, [bridge, review]);

  const capabilityReplyReason = composer.threadReason("reply");
  const replyReason = useCallback(
    (discussionId: string): string | null =>
      staleIds.has(discussionId) ? THREAD_REFRESH_REFUSAL : capabilityReplyReason,
    [capabilityReplyReason, staleIds],
  );
  const resolveReason = useCallback(
    (discussion: DiscussionDto): string | null => {
      if (!discussion.resolvable) return RESOLVE_UNSUPPORTED;
      return composer.threadReason("resolve");
    },
    [composer],
  );

  const toggle = useCallback((discussionId: string): void => {
    setExpandedIds((current) => {
      const next = new Set(current);
      if (!next.delete(discussionId)) next.add(discussionId);
      return next;
    });
  }, []);
  const openReply = useCallback((discussionId: string): void => {
    setNotice((current) =>
      current?.discussionId === discussionId ? null : current,
    );
    setExpandedIds((current) => new Set(current).add(discussionId));
    setComposingId(discussionId);
  }, []);
  const closeReply = useCallback((): void => setComposingId(null), []);

  /**
   * Publishes one reply, then the resolution the toggle asked for, then rereads
   * the discussions so the thread shows what it just published. The sentence
   * for whatever went wrong is set before the composer closes and lives on the
   * thread's own row, so a refused resolution and a failed reread are both
   * said rather than swallowed by the composer disappearing.
   */
  const reply = useCallback(
    async (
      thread: AnchoredThread,
      body: string,
      alsoResolve: boolean,
    ): Promise<void> => {
      const id = thread.discussion.id;
      const sent = await composer.replyToThread(id, body);
      if (!sent.ok) {
        if (live.current && sent.message !== null)
          setNotice({ discussionId: id, text: sent.message });
        return;
      }
      buffersFor(composer.review).replies.delete(id);
      const trouble: string[] = [];
      if (alsoResolve) {
        const resolved = !thread.discussion.is_resolved;
        const flipped = await composer.setThreadResolved(id, resolved);
        if (flipped.ok) {
          if (live.current)
            setThreads((current) =>
              current.map((item) =>
                item.id === id ? { ...item, is_resolved: resolved } : item,
              ),
            );
        } else trouble.push(flipped.message ?? RESOLVE_UNSUPPORTED);
      }
      const reread = await refresh();
      if (!reread) trouble.push(THREAD_REFRESH_REFUSAL);
      if (!live.current) return;
      // The sentence is published first, then the thread is marked out of date
      // if it is, and only then does the composer close.
      setNotice(
        trouble.length === 0
          ? null
          : { discussionId: id, text: `${REPLY_PUBLISHED} ${trouble.join(" ")}` },
      );
      if (!reread) setStaleIds((current) => new Set(current).add(id));
      setComposingId(null);
    },
    [composer, refresh],
  );

  return {
    review,
    loadError,
    notice: (discussionId) =>
      notice?.discussionId === discussionId ? notice.text : null,
    pendingReview: composer.pendingReview,
    byLine: anchored.byLine,
    byPath: anchored.byPath,
    allocations,
    expanded: (discussionId) => expandedIds.has(discussionId),
    toggle,
    composing: (discussionId) => composingId === discussionId,
    openReply,
    closeReply,
    replyReason,
    resolveReason,
    reply,
    openExternal: composer.openExternal,
  };
}

/**
 * One published thread as a row of the diff, under the line it is anchored to.
 * It is collapsed to a single summary line until the reader asks for it, so a
 * review with hundreds of threads costs the window one row each.
 */
export function DiffThread({
  thread,
  slot,
}: {
  readonly thread: AnchoredThread;
  readonly slot: DiscussionThreadSlot;
}): ReactNode {
  const { discussion } = thread;
  const reference = threadReference(thread);
  const expanded = slot.expanded(discussion.id);
  const composing = slot.composing(discussion.id);
  const allocation = slot.allocations.get(discussion.id);
  const replies = discussion.root_comment.replies;
  const notice = slot.notice(discussion.id);
  const replyReason = slot.replyReason(discussion.id);
  return (
    <article
      className={`diff-thread${discussion.is_resolved ? " diff-thread-resolved" : ""}`}
      aria-label={`Discussion ${threadDescriptor(thread)}`}
    >
      <header className="diff-thread-heading">
        <button
          className="diff-thread-summary"
          aria-expanded={expanded}
          aria-label={`Summary of ${reference}`}
          onClick={() => slot.toggle(discussion.id)}
        >
          {threadSummaryText(discussion)}
        </button>
        <button
          className="button button-secondary"
          aria-label={`Reply to ${reference}`}
          disabled={replyReason !== null}
          title={replyReason ?? undefined}
          onClick={() => slot.openReply(discussion.id)}
        >
          Reply
        </button>
      </header>
      {notice !== null && (
        <div className="notice notice-error diff-thread-notice" role="alert">
          {notice}
        </div>
      )}
      {expanded && (
        <>
          <p className="diff-thread-meta">
            {discussion.root_comment.author.display_name ||
              discussion.root_comment.author.username}
          </p>
          <div className="diff-thread-body">
            <DiscussionMarkdownBody
              allocated={allocation?.root === true}
              body={discussion.root_comment.body}
              openExternal={slot.openExternal}
            />
            {replies.map((item, index) => (
              <blockquote key={item.id}>
                <DiscussionMarkdownBody
                  allocated={allocation?.replies[index] === true}
                  body={item.body}
                  openExternal={slot.openExternal}
                />
              </blockquote>
            ))}
          </div>
        </>
      )}
      {composing && (
        <ThreadReplyComposer
          key={`reply:${discussion.id}`}
          thread={thread}
          slot={slot}
        />
      )}
    </article>
  );
}

/**
 * The reply shape the design puts on a thread: the composer plus one toggle
 * beside its button, which resolves or reopens the thread with the same press
 * that publishes the reply.
 */
function ThreadReplyComposer({
  thread,
  slot,
}: {
  readonly thread: AnchoredThread;
  readonly slot: DiscussionThreadSlot;
}): ReactNode {
  const { discussion } = thread;
  const reference = threadReference(thread);
  const replyReason = slot.replyReason(discussion.id);
  const [body, setBodyState] = useState(
    () => buffersFor(slot.review).replies.get(discussion.id) ?? "",
  );
  const [toggled, setToggled] = useState(false);
  const editor = useRef<HTMLTextAreaElement | null>(null);
  // Reply is pressed from the collapsed row, which leaves focus on that row,
  // so the composer takes it here: that is what makes Escape and Ctrl/Cmd+Enter
  // reach the composer that just opened instead of the row behind it.
  useEffect(() => {
    editor.current?.focus();
  }, []);
  const resolveReason = slot.resolveReason(discussion);
  const resolveLabel = discussion.is_resolved
    ? "Reopen thread"
    : "Resolve thread";
  const setBody = (value: string): void => {
    buffersFor(slot.review).replies.set(discussion.id, value);
    setBodyState(value);
  };
  const submit = (): void => {
    if (body.length === 0 || replyReason !== null) return;
    void slot.reply(thread, body, toggled && resolveReason === null);
  };
  return (
    <section
      className="diff-thread-composer"
      aria-label={`Reply composer for ${reference}`}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          slot.closeReply(discussion.id);
          return;
        }
        // The primary action belongs to the composer rather than to the
        // editor, and it overrides button activation, so one press is one
        // reply however the reader reaches it.
        if (!(event.ctrlKey || event.metaKey) || event.key !== "Enter") return;
        event.preventDefault();
        submit();
      }}
    >
      <textarea
        ref={editor}
        className="diff-thread-text"
        aria-label={`Reply body for ${reference}`}
        value={body}
        onChange={(event) => setBody(event.target.value)}
      />
      {slot.pendingReview && (
        <small className="diff-thread-immediate">
          {THREAD_REPLY_IMMEDIATE_NOTE}
        </small>
      )}
      <div className="diff-thread-actions">
        <label className="diff-thread-resolve">
          <input
            type="checkbox"
            checked={toggled && resolveReason === null}
            disabled={resolveReason !== null}
            title={resolveReason ?? undefined}
            onChange={(event) => setToggled(event.target.checked)}
          />
          {resolveLabel}
        </label>
        <button
          className="button button-secondary"
          onClick={() => slot.closeReply(discussion.id)}
        >
          Cancel
        </button>
        <button
          className="button"
          disabled={body.length === 0 || replyReason !== null}
          title={replyReason ?? undefined}
          onClick={submit}
        >
          Reply now
        </button>
      </div>
      {resolveReason !== null && <small>{resolveReason}</small>}
      {replyReason !== null && <small>{replyReason}</small>}
    </section>
  );
}

/**
 * The spacer the other split pane renders in place of a thread. Both panes
 * iterate the same thread list for the same row and read the same expansion,
 * so the two independent pane grids stay on the same rows.
 */
export function DiffThreadMirror({
  expanded,
}: {
  readonly expanded: boolean;
}): ReactNode {
  return (
    <div
      className={
        expanded
          ? "thread-row-mirror thread-row-mirror-expanded"
          : "thread-row-mirror"
      }
      role="presentation"
      aria-hidden="true"
    />
  );
}

/**
 * Whether a thread's row takes the taller of the two declared heights. A row
 * carrying a sentence takes it too, so a refusal is never reported into a
 * collapsed row that has no space to show it.
 */
export function threadRowExpanded(
  slot: DiscussionThreadSlot,
  thread: AnchoredThread,
): boolean {
  return (
    slot.expanded(thread.discussion.id) ||
    slot.notice(thread.discussion.id) !== null
  );
}

/** The class the row wrapping a thread carries, collapsed or expanded. */
export function threadRowClassName(
  slot: DiscussionThreadSlot,
  thread: AnchoredThread,
): string {
  return threadRowExpanded(slot, thread)
    ? "thread-row thread-row-expanded"
    : "thread-row";
}
