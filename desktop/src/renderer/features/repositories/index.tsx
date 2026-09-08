import { useCallback, useEffect, type ReactNode } from "react";
import type { DesktopBridge, RepositoryDto } from "../../../shared/bridge.js";
import type { AppRoute } from "../../core/navigation.js";
import { safeError } from "../../core/presentation.js";
import type { QueryCoordinator } from "../../core/query.js";
import { useRetainedRead } from "../../core/use-read.js";

interface Props {
  readonly bridge: DesktopBridge;
  readonly queries: QueryCoordinator;
  readonly selected: RepositoryDto | null;
  readonly navigate: (route: AppRoute) => void;
  readonly onDiscovery: (repositories: readonly RepositoryDto[]) => void;
}

export function RepositoryNavigation({
  bridge,
  queries,
  selected,
  navigate,
  onDiscovery,
}: Props): ReactNode {
  const begin = useCallback(() => bridge.discoverRepositories(), [bridge]);
  const state = useRetainedRead(queries, "repositories", begin, [bridge]);
  const repositories = state.value?.repositories ?? [];
  useEffect(() => {
    if (state.value) onDiscovery(state.value.repositories);
  }, [onDiscovery, state.value]);
  return (
    <aside className="sidebar" aria-label="Repository navigation">
      <h2 className="sidebar-title">Repositories</h2>
      <button
        className="button button-secondary repository-refresh"
        disabled={state.loading}
        onClick={state.refresh}
      >
        Refresh local repositories
      </button>
      <button
        className={`nav-item ${selected === null ? "nav-item-active" : ""}`}
        onClick={() => navigate({ kind: "inbox", repository: null })}
      >
        All reviews
      </button>
      {repositories.map((repository) => (
        <button
          key={repository.handle}
          className={`nav-item ${selected?.handle === repository.handle ? "nav-item-active" : ""}`}
          data-forge={repository.forge_type}
          onClick={() => navigate({ kind: "inbox", repository })}
        >
          {repository.display_name}
        </button>
      ))}
      {state.loading && (
        <Notice kind="loading">Finding admitted repositories…</Notice>
      )}
      {Boolean(state.error) && (
        <>
          <Notice kind="error">{safeError(state.error)}</Notice>
          <button className="button button-secondary" onClick={state.refresh}>
            Retry
          </button>
        </>
      )}
      {!state.loading && !state.error && repositories.length === 0 && (
        <Notice kind="empty">
          No local repositories were found. Clone a repository under the
          configured scan root, then refresh.
        </Notice>
      )}
    </aside>
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
