import { useEffect, useSyncExternalStore, type ReactNode } from "react";
import type {
  DesktopBridge,
  ServiceState,
  ServiceStatusDto,
} from "../../shared/bridge.js";

export const SERVICE_STATUS_TEXT = Object.freeze({
  connecting: "Connecting to the local service…",
  connected: "Local service connected",
  stopped: "Local service not running",
  unavailable: "Local service unavailable",
  updates: "Updates are available. Refresh the current view.",
} as const);

export interface ServiceStatusView {
  readonly text: string;
  readonly className: string;
}

type StatusBridge = Pick<DesktopBridge, "getServiceStatus" | "onServiceStatus">;

const BASE_CLASS = "service-status";

/**
 * Owns the header's service line. The main process reports whether the local
 * service is running; the renderer adds its own probe result and the
 * updates-available notice. A stopped service outranks everything else, so the
 * header never says connected while reads fail because the service is gone.
 */
export class ServiceStatusModel {
  #state: ServiceState | null = null;
  #revision = -1;
  #probeOk = false;
  #probeFailed = false;
  #updates = false;
  #view: ServiceStatusView = view(SERVICE_STATUS_TEXT.connecting, BASE_CLASS);
  #notices: readonly string[] = NO_NOTICES;
  readonly #dismissed = new Set<string>();
  readonly #listeners = new Set<() => void>();

  get view(): ServiceStatusView {
    return this.#view;
  }

  subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };

  snapshot = (): ServiceStatusView => this.#view;

  /** The service's startup warnings the user has not dismissed yet. */
  notices = (): readonly string[] => this.#notices;

  dismissNotice(notice: string): void {
    this.#dismissed.add(notice);
    this.#setNotices(this.#notices);
  }

  /** Applies a main-process report, ignoring any older than one already seen. */
  applyStatus(status: ServiceStatusDto): void {
    if (!isStatus(status) || status.revision <= this.#revision) return;
    this.#revision = status.revision;
    const reconnected = status.state === "connected" && this.#state === "stopped";
    this.#state = status.state;
    if (reconnected) {
      this.#probeFailed = false;
      this.#updates = false;
    }
    this.#refresh();
    // Keep every notice already shown until the user dismisses it, even when
    // a later report, such as a stopped service, carries none.
    this.#setNotices([...new Set([...this.#notices, ...statusNotices(status)])]);
  }

  probeSucceeded(): void {
    this.#probeOk = true;
    this.#probeFailed = false;
    this.#refresh();
  }

  probeFailed(): void {
    this.#probeFailed = true;
    this.#refresh();
  }

  updatesAvailable(): void {
    this.#updates = true;
    this.#refresh();
  }

  /** Subscribes to main-process reports first, then asks for the current state. */
  bind(bridge: StatusBridge): () => void {
    let active = true;
    const unsubscribe = bridge.onServiceStatus((status) => {
      if (active) this.applyStatus(status);
    });
    bridge.getServiceStatus().then(
      (status) => {
        if (active) this.applyStatus(status);
      },
      () => undefined,
    );
    return () => {
      active = false;
      unsubscribe();
    };
  }

  #setNotices(candidates: readonly string[]): void {
    const next = candidates.filter((notice) => !this.#dismissed.has(notice));
    if (
      next.length === this.#notices.length &&
      next.every((notice, index) => notice === this.#notices[index])
    ) {
      return;
    }
    this.#notices = next.length === 0 ? NO_NOTICES : Object.freeze(next);
    for (const listener of [...this.#listeners]) listener();
  }

  #refresh(): void {
    const next = this.#compute();
    if (next.text === this.#view.text && next.className === this.#view.className) {
      return;
    }
    this.#view = next;
    for (const listener of [...this.#listeners]) listener();
  }

  #compute(): ServiceStatusView {
    if (this.#state === "stopped") {
      return view(SERVICE_STATUS_TEXT.stopped, `${BASE_CLASS} service-status-error`);
    }
    if (this.#probeFailed) {
      return view(SERVICE_STATUS_TEXT.unavailable, `${BASE_CLASS} service-status-error`);
    }
    if (this.#updates) {
      return view(SERVICE_STATUS_TEXT.updates, `${BASE_CLASS} service-status-warning`);
    }
    if (this.#state === "connected" || this.#probeOk) {
      return view(SERVICE_STATUS_TEXT.connected, `${BASE_CLASS} service-status-ready`);
    }
    return view(SERVICE_STATUS_TEXT.connecting, BASE_CLASS);
  }
}

export function ServiceStatusLine({
  model,
  bridge,
}: {
  readonly model: ServiceStatusModel;
  readonly bridge: StatusBridge;
}): ReactNode {
  useEffect(() => model.bind(bridge), [model, bridge]);
  const current = useSyncExternalStore(model.subscribe, model.snapshot);
  return (
    <p id="service-status" className={current.className} role="status">
      {current.text}
    </p>
  );
}

/**
 * Shows the local service's startup warnings, such as a review draft whose
 * interrupted submission may have partly posted, until the user dismisses
 * each one.
 */
export function ServiceNotices({
  model,
}: {
  readonly model: ServiceStatusModel;
}): ReactNode {
  const notices = useSyncExternalStore(model.subscribe, model.notices);
  if (notices.length === 0) return null;
  return (
    <div id="service-notices" className="service-notices">
      {notices.map((notice) => (
        <div key={notice} className="service-notice" role="alert">
          <span>{notice}</span>
          <button
            type="button"
            className="button button-quiet"
            onClick={() => model.dismissNotice(notice)}
          >
            Dismiss
          </button>
        </div>
      ))}
    </div>
  );
}

const NO_NOTICES: readonly string[] = Object.freeze([]);

function statusNotices(status: ServiceStatusDto): readonly string[] {
  const notices: unknown = (status as { notices?: unknown }).notices;
  if (!Array.isArray(notices)) return NO_NOTICES;
  return notices.filter(
    (notice): notice is string => typeof notice === "string" && notice.length > 0,
  );
}

function view(text: string, className: string): ServiceStatusView {
  return Object.freeze({ text, className });
}

function isStatus(value: unknown): value is ServiceStatusDto {
  if (value === null || typeof value !== "object") return false;
  const record = value as Record<string, unknown>;
  return (
    (record.state === "connected" || record.state === "stopped") &&
    typeof record.revision === "number" &&
    Number.isSafeInteger(record.revision) &&
    record.revision >= 0
  );
}
