export type Bridge = {
  invoke: (method: string, params?: Record<string, unknown>) => Promise<unknown>;
};

export type Review = {
  id: string;
  number: number;
  title: string;
  author: string;
  repo: string;
  forge: string;
  status: string;
};

export type DiffKind = "context" | "addition" | "deletion";

export type DiffLine = {
  old_line: number | null;
  new_line: number | null;
  kind: DiffKind;
  text: string;
};

export type Diff = {
  path: string;
  lines: DiffLine[];
};

export type PluginModule = {
  id: string;
  title: string;
  entry_url: string;
};

export type PluginRecord = {
  id: string;
  title: string;
  status: "ready" | "terminal_only" | "disabled" | "error";
  modules: PluginModule[];
};

export type PluginApi = {
  invoke: (method: string, params?: Record<string, unknown>) => Promise<unknown>;
};

export type PluginMount = (
  container: HTMLElement,
  api: PluginApi,
) => void | (() => void);

declare global {
  interface Window {
    tongs?: Bridge;
  }
}
