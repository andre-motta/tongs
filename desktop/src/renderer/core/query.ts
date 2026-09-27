import type { DesktopBridge, DesktopRead } from "../../shared/bridge.js";

export class StaleQueryError extends Error {}
export interface CoordinatedRead<T> {
  /** Read at cancellation time, so a paged read may still be adding to it. */
  readonly requestTokens: readonly string[];
  readonly result: Promise<T>;
  /** Stops a read that issues more requests, such as a paged read. */
  readonly cancel?: () => void;
}
type QueryRead<T> = DesktopRead<T> | CoordinatedRead<T>;

export class QueryCoordinator {
  private generation = 0;
  private active = new Map<
    string,
    {
      readonly generation: number;
      readonly tokens: readonly string[];
      readonly stop: (() => void) | null;
    }
  >();

  constructor(private readonly bridge: Pick<DesktopBridge, "cancelRead">) {}

  async run<T>(key: string, begin: () => QueryRead<T>): Promise<T> {
    const generation = ++this.generation;
    const previous = this.active.get(key);
    const read = begin();
    const tokens =
      "requestTokens" in read ? read.requestTokens : [read.requestToken];
    const stop = "requestTokens" in read ? (read.cancel ?? null) : null;
    this.active.set(key, { generation, tokens, stop });
    if (previous) {
      previous.stop?.();
      for (const token of previous.tokens)
        void this.bridge.cancelRead(token).catch(() => false);
    }
    try {
      const result = await read.result;
      if (this.active.get(key)?.generation !== generation)
        throw new StaleQueryError("Stale desktop read");
      return result;
    } finally {
      if (this.active.get(key)?.generation === generation)
        this.active.delete(key);
    }
  }

  async cancel(key: string): Promise<void> {
    const active = this.active.get(key);
    if (!active) return;
    this.active.delete(key);
    active.stop?.();
    await Promise.all(
      active.tokens.map((token) =>
        this.bridge.cancelRead(token).catch(() => false),
      ),
    );
  }

  async cancelAll(): Promise<void> {
    await Promise.all([...this.active.keys()].map((key) => this.cancel(key)));
  }
}
