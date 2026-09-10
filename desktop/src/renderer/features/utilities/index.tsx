import {
  useEffect,
  useRef,
  useSyncExternalStore,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import type { ClearCacheResult, CopyReviewUrlResult } from "../../../shared/utilities.js";
import type {
  AppRoute,
  FeatureContext,
  FeatureContribution,
} from "../../core/navigation.js";

type UtilityState =
  | { readonly kind: "idle" }
  | { readonly kind: "confirm_clear" }
  | { readonly kind: "clearing" }
  | {
      readonly kind: "notice";
      readonly severity: "status" | "error";
      readonly message: string;
    };

export class WorkspaceUtilityController {
  private stateValue: UtilityState = Object.freeze({ kind: "idle" });
  private listeners = new Set<() => void>();
  private clearInFlight = false;

  get state(): UtilityState {
    return this.stateValue;
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  beginClear(): void {
    if (this.clearInFlight) return;
    this.update({ kind: "confirm_clear" });
  }

  cancel(): void {
    if (this.clearInFlight) return;
    this.update({ kind: "idle" });
  }

  async copyReviewUrl(
    context: FeatureContext,
    route: AppRoute,
  ): Promise<void> {
    if (route.kind !== "review") return;
    let result: CopyReviewUrlResult;
    try {
      result = await context.bridge.copyReviewUrl(route.item.handle);
    } catch {
      result = {
        outcome: "failed",
        message: "The review URL could not be copied. Check clipboard access and retry.",
      };
    }
    this.update({
      kind: "notice",
      severity: result.outcome === "copied" ? "status" : "error",
      message: result.message,
    });
  }

  async confirmClear(context: FeatureContext): Promise<void> {
    if (this.clearInFlight || this.stateValue.kind !== "confirm_clear") return;
    this.clearInFlight = true;
    this.update({ kind: "clearing" });
    let result: ClearCacheResult;
    try {
      await context.queries.cancelAll();
      result = await context.bridge.clearCache();
    } catch {
      result = {
        outcome: "failed",
        message: "The shared API cache could not be cleared. Retry after reconnecting the local service.",
      };
    } finally {
      this.clearInFlight = false;
    }
    this.update({
      kind: "notice",
      severity: result.outcome === "cleared" ? "status" : "error",
      message: result.message,
    });
  }

  private update(state: UtilityState): void {
    this.stateValue = Object.freeze(state);
    for (const listener of this.listeners) listener();
  }
}

export function createUtilitiesFeature(
  controller: WorkspaceUtilityController,
): FeatureContribution {
  return {
    id: "workspace.utilities",
    order: 90,
    commands: [
      {
        id: "workspace.copy-review-url",
        label: "Copy URL",
        order: 80,
        isVisible: (_context, route) => route.kind === "review",
        disabledReason: () => null,
        run: (context, route) => controller.copyReviewUrl(context, route),
      },
      {
        id: "workspace.clear-cache",
        label: "Clear Cache",
        order: 900,
        isVisible: () => true,
        disabledReason: () =>
          controller.state.kind === "clearing"
            ? "The shared API cache is being cleared."
            : null,
        run: () => controller.beginClear(),
      },
    ],
    matches: () => false,
    render: () => null,
  };
}

export function WorkspaceUtilityOverlay({
  controller,
  context,
}: {
  readonly controller: WorkspaceUtilityController;
  readonly context: FeatureContext;
}): ReactNode {
  const state = useSyncExternalStore(
    controller.subscribe,
    () => controller.state,
  );
  const confirm = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (state.kind === "confirm_clear") confirm.current?.focus();
  }, [state.kind]);
  if (state.kind === "idle") return null;
  if (state.kind === "confirm_clear" || state.kind === "clearing") {
    return (
      <div
        className="utility-dialog-backdrop"
        onKeyDown={(event) => cancelOnEscape(event, controller)}
      >
        <section className="utility-dialog" role="alertdialog" aria-modal="true">
          <div>
            <strong>Clear shared API cache?</strong>
            <p>
              Cached forge responses will be removed. Saved and in-progress review
              drafts will be preserved.
            </p>
          </div>
          <div className="utility-actions">
            <button
              className="button button-quiet"
              disabled={state.kind === "clearing"}
              onClick={() => controller.cancel()}
            >
              Cancel
            </button>
            <button
              ref={confirm}
              className="button"
              disabled={state.kind === "clearing"}
              onClick={() => void controller.confirmClear(context)}
            >
              {state.kind === "clearing" ? "Clearing…" : "Clear cache"}
            </button>
          </div>
        </section>
      </div>
    );
  }
  return (
    <section
      className={`utility-notice utility-notice-${state.severity}`}
      role={state.severity === "error" ? "alert" : "status"}
    >
      <span>{state.message}</span>
      <button className="button button-quiet" onClick={() => controller.cancel()}>
        Dismiss
      </button>
    </section>
  );
}

function cancelOnEscape(
  event: KeyboardEvent<HTMLDivElement>,
  controller: WorkspaceUtilityController,
): void {
  if (event.key !== "Escape") return;
  event.preventDefault();
  controller.cancel();
}
