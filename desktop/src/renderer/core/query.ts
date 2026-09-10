import type { DesktopBridge, DesktopRead } from "../../shared/bridge.js";

export class StaleQueryError extends Error {}
export interface CoordinatedRead<T> {
  readonly requestTokens: readonly string[];
  readonly result: Promise<T>;
}
type QueryRead<T> = DesktopRead<T> | CoordinatedRead<T>;

export class QueryCoordinator {
  private generation = 0;
  private active = new Map<
    string,
    { readonly generation: number; readonly tokens: readonly string[] }
  >();

  constructor(private readonly bridge: Pick<DesktopBridge, "cancelRead">) {}

  async run<T>(key: string, begin: () => QueryRead<T>): Promise<T> {
    const generation = ++this.generation;
    const previous = this.active.get(key);
    const read = begin();
    const tokens =
      "requestTokens" in read ? read.requestTokens : [read.requestToken];
    this.active.set(key, { generation, tokens });
    if (previous)
      for (const token of previous.tokens)
        void this.bridge.cancelRead(token).catch(() => false);
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
