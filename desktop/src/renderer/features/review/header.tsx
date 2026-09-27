import { useCallback, useEffect, useState, type ReactNode } from "react";

import type {
  DesktopBridge,
  ReviewRevisionDto,
} from "../../../shared/bridge.js";
import type {
  ReviewAction,
  ReviewActionCapabilitiesDto,
} from "../../../shared/review.js";
import type {
  AppRoute,
  DiscussionDiffTarget,
} from "../../core/navigation.js";
import { safeError } from "../../core/presentation.js";
import {
  isUncertainError,
  newOperationId,
  reviewMutationError,
  useSharedReviewWorkflow,
} from "./composer.js";
import {
  ReviewDrawerMount,
  pendingEntryTarget,
  requestPendingEdit,
  useReviewDrawer,
} from "./drawer.js";
import type { PendingDraftEntry } from "./pending-card.js";
import {
  acknowledgeQuickUncertainty,
  beginQuickIntent,
  markQuickIntentUncertain,
  recoverQuickIntent,
  rejectQuickIntent,
  settleQuickIntent,
} from "./state.js";
import type { SuggestionForge } from "./suggestion.js";

/** The lifecycle actions, in the order the header offers them. */
const REVIEW_ACTIONS: readonly ReviewAction[] = Object.freeze([
  "merge",
  "close",
  "reopen",
  "unapprove",
]);

/** Said on every action while the capability read is still in flight. */
export const ACTIONS_LOADING =
  "Review action support for this review is still loading.";

/** Said on every action while an earlier mutation's result is not settled. */
export const ACTIONS_BLOCKED =
  "Resolve or acknowledge the previous action before another mutation.";

/**
 * The review page's own controls, the same on every review tab: the lifecycle
 * actions (Merge, Close, Reopen, Remove approval) and the "Your review" button
 * with its drawer. Every tab that knows the review's revision mounts exactly
 * this, so what a reader can do to the review itself never depends on which
 * tab they happen to be reading.
 *
 * Jump and Edit in the drawer send the reader to the Changes tab over the
 * existing discussion jump route, so the coordinates a pending entry resolves
 * to are the ones that route already resolves and nothing new decides where a
 * line is. On the Changes tab itself the navigation keeps the panel and only
 * changes the target, which is the jump it always made there.
 */
export function ReviewHeaderControls({
  bridge,
  route,
  navigate,
  forge,
  revision,
}: {
  readonly bridge: DesktopBridge;
  readonly route: Extract<AppRoute, { kind: "review" }>;
  readonly navigate: (route: AppRoute) => void;
  readonly forge: SuggestionForge | null;
  readonly revision: ReviewRevisionDto;
}): ReactNode {
  const review = route.item.handle;
  const openDiffAt = (target: DiscussionDiffTarget): void =>
    navigate({ ...route, panel: "diff", diffTarget: target });
  return (
    <>
      <ReviewLifecycleActions
        bridge={bridge}
        review={review}
        sourceBranch={route.item.summary.source_branch}
        revision={revision}
      />
      <HeaderReviewDrawer
        bridge={bridge}
        review={review}
        forge={forge}
        revision={revision}
        openDiffAt={openDiffAt}
      />
    </>
  );
}

function HeaderReviewDrawer({
  bridge,
  review,
  forge,
  revision,
  openDiffAt,
}: {
  readonly bridge: DesktopBridge;
  readonly review: string;
  readonly forge: SuggestionForge | null;
  readonly revision: ReviewRevisionDto;
  readonly openDiffAt: (target: DiscussionDiffTarget) => void;
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
        if (to) openDiffAt(to);
      }}
      edit={(entry) => {
        requestPendingEdit(review, entry.id);
        const to = target(entry);
        if (to) openDiffAt(to);
      }}
    />
  );
}

/**
 * What the capability read behind the actions has established. A capability
 * that has not been answered for is not an unsupported one, so a read still in
 * flight and a read that failed each keep their own words on the buttons.
 */
type CapabilityStanding =
  | { readonly kind: "loading" }
  | { readonly kind: "failed"; readonly message: string }
  | { readonly kind: "known"; readonly capabilities: ReviewActionCapabilitiesDto };

/**
 * The lifecycle actions on the review itself. Each needs a second press on its
 * own "Confirm" label before anything is sent, is enabled only where the forge
 * says the reader may take it, and is refused while an earlier mutation's
 * result is still sending or unknown. The outcome is written to the shared
 * review workflow state, so every surface of the review sees the same standing
 * and a tab change never loses an unknown result.
 *
 * Only the outcome of a lifecycle action is reported here. A verdict or a
 * general comment is reported by the Overview composer that sent it, so no
 * sentence is ever said twice on one page.
 */
function ReviewLifecycleActions({
  bridge,
  review,
  sourceBranch,
  revision,
}: {
  readonly bridge: DesktopBridge;
  readonly review: string;
  readonly sourceBranch: string;
  readonly revision: ReviewRevisionDto;
}): ReactNode {
  const { workflow, held, apply } = useSharedReviewWorkflow(
    bridge,
    review,
    revision,
  );
  const [standing, setStanding] = useState<CapabilityStanding>({
    kind: "loading",
  });
  const [confirmation, setConfirmation] = useState<ReviewAction | null>(null);
  const [mergeOptions, setMergeOptions] = useState({
    squash: false,
    cleanup: false,
  });
  const [recoveryError, setRecoveryError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    let token: string | null = null;
    let settled = false;
    setStanding({ kind: "loading" });
    try {
      const read = bridge.getReviewActionCapabilities(review);
      token = read.requestToken;
      void read.result.then(
        (result) => {
          settled = true;
          if (live)
            setStanding({ kind: "known", capabilities: result.capabilities });
        },
        (reason: unknown) => {
          settled = true;
          if (live) setStanding({ kind: "failed", message: safeError(reason) });
        },
      );
    } catch (reason) {
      settled = true;
      setStanding({ kind: "failed", message: safeError(reason) });
    }
    return () => {
      live = false;
      if (token === null || settled) return;
      try {
        void bridge.cancelRead(token).catch(() => undefined);
      } catch {
        // A cancel that cannot be dispatched leaves nothing to clean up here.
      }
    };
  }, [bridge, review]);

  const quick = workflow.quick;
  const blocked = quick?.status === "sending" || quick?.status === "unknown";
  const capabilities = standing.kind === "known" ? standing.capabilities : null;
  const actionQuick = quick && isActionCommand(quick.command) ? quick : null;

  const run = async (action: ReviewAction): Promise<void> => {
    if (confirmation !== action) {
      setConfirmation(action);
      return;
    }
    const operationId = newOperationId(action);
    const params = {
      operation_id: operationId,
      review,
      revision: held.current.displayed.revision,
    };
    setConfirmation(null);
    setRecoveryError(null);
    try {
      apply((current) =>
        beginQuickIntent(current, operationId, { action, ...params }),
      );
    } catch {
      // Another mutation claimed the review between the render and the press;
      // the buttons are already refusing on its account.
      return;
    }
    try {
      const receipt =
        action === "merge"
          ? await bridge.mergeReview({
              ...params,
              squash: mergeOptions.squash,
              source_cleanup: mergeOptions.cleanup
                ? { branch: sourceBranch }
                : null,
            })
          : action === "close"
            ? await bridge.closeReview(params)
            : action === "reopen"
              ? await bridge.reopenReview(params)
              : await bridge.unapproveReview(params);
      apply((current) => settleQuickIntent(current, operationId, receipt));
    } catch (reason) {
      apply((current) =>
        isUncertainError(reason)
          ? markQuickIntentUncertain(current, operationId)
          : rejectQuickIntent(current, operationId, reviewMutationError(reason)),
      );
    }
  };

  const recover = useCallback(async (): Promise<void> => {
    const current = held.current.quick;
    if (
      !current ||
      current.status !== "unknown" ||
      !isActionCommand(current.command)
    )
      return;
    try {
      const read = bridge.getReviewActionReceipt({
        operation_id: current.operationId,
        review,
        revision: current.command.revision,
        action: current.command.action,
      });
      const result = await read.result;
      if (result.receipt) {
        const receipt = result.receipt;
        apply((state) =>
          recoverQuickIntent(state, current.operationId, receipt),
        );
        setRecoveryError(null);
      } else
        setRecoveryError(
          "No retained action receipt is available. Inspect the forge before acknowledging uncertainty.",
        );
    } catch (reason) {
      setRecoveryError(safeError(reason));
    }
  }, [apply, bridge, held, review]);

  const titleFor = (action: ReviewAction): string | undefined =>
    standing.kind === "loading"
      ? ACTIONS_LOADING
      : standing.kind === "failed"
        ? `Review action support could not be read: ${standing.message}`
        : capabilities?.[action] !== true
          ? `${actionLabel(action)} is unsupported for this review.`
          : blocked
            ? ACTIONS_BLOCKED
            : undefined;

  const showDetails =
    capabilities?.merge === true ||
    recoveryError !== null ||
    Boolean(actionQuick?.message);

  return (
    <>
      <div
        className="review-header-actions"
        role="group"
        aria-label="Review lifecycle actions"
      >
        {REVIEW_ACTIONS.map((action) => (
          <button
            key={action}
            className="button button-secondary"
            data-review-action={action}
            disabled={capabilities?.[action] !== true || blocked}
            title={titleFor(action)}
            onClick={() => void run(action)}
          >
            {confirmation === action
              ? `Confirm ${actionLabel(action)}`
              : actionLabel(action)}
          </button>
        ))}
      </div>
      {showDetails && (
        <div className="review-header-details">
          {capabilities?.merge === true && (
            <div className="review-workflow-row review-header-merge-options">
              <label>
                <input
                  type="checkbox"
                  checked={mergeOptions.squash}
                  onChange={(event) => {
                    setMergeOptions({
                      ...mergeOptions,
                      squash: event.target.checked,
                    });
                    if (confirmation === "merge") setConfirmation(null);
                  }}
                />
                Squash commits
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={mergeOptions.cleanup}
                  onChange={(event) => {
                    setMergeOptions({
                      ...mergeOptions,
                      cleanup: event.target.checked,
                    });
                    if (confirmation === "merge") setConfirmation(null);
                  }}
                />
                Delete source branch after merge
              </label>
            </div>
          )}
          {recoveryError !== null && (
            <div className="notice notice-error" role="alert">
              {recoveryError}
            </div>
          )}
          {actionQuick?.message && (
            <div
              className={`notice notice-${actionQuick.status === "rejected" ? "error" : "warning"}`}
              role={actionQuick.status === "rejected" ? "alert" : "status"}
            >
              <span>{actionQuick.message}</span>
              {actionQuick.status === "unknown" && (
                <span className="review-workflow-row">
                  <button
                    className="button button-secondary"
                    onClick={() => void recover()}
                  >
                    Check retained receipt
                  </button>
                  <button
                    className="button button-secondary"
                    onClick={() =>
                      apply((current) =>
                        acknowledgeQuickUncertainty(
                          current,
                          actionQuick.operationId,
                        ),
                      )
                    }
                  >
                    I inspected the forge; acknowledge uncertainty
                  </button>
                </span>
              )}
            </div>
          )}
        </div>
      )}
    </>
  );
}

/**
 * Whether a quick intent's command is one of the lifecycle actions rather
 * than a verdict or a comment. Each surface reports only the intents it sent.
 */
export function isActionCommand(value: unknown): value is {
  readonly action: ReviewAction;
  readonly revision: ReviewRevisionDto;
} {
  return (
    value !== null &&
    typeof value === "object" &&
    "action" in value &&
    (value.action === "merge" ||
      value.action === "close" ||
      value.action === "reopen" ||
      value.action === "unapprove") &&
    "revision" in value &&
    value.revision !== null &&
    typeof value.revision === "object"
  );
}

export function actionLabel(action: ReviewAction): string {
  return action === "merge"
    ? "Merge"
    : action === "close"
      ? "Close"
      : action === "reopen"
        ? "Reopen"
        : "Remove approval";
}
