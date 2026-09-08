import {
  useCallback,
  useEffect,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import type { DesktopBridge, RepositoryDto } from "../../../shared/bridge.js";
import type { AppRoute } from "../../core/navigation.js";
import { safeError } from "../../core/presentation.js";
import type { QueryCoordinator } from "../../core/query.js";
import { useRetainedRead } from "../../core/use-read.js";

export type RepositoryForgeFilter = "all" | RepositoryDto["forge_type"];
export type RepositorySort = "name" | "forge" | "host";

interface Props {
  readonly bridge: DesktopBridge;
  readonly queries: QueryCoordinator;
  readonly selected: RepositoryDto | null | undefined;
  readonly navigate: (route: AppRoute) => void;
  readonly onDiscovery: (repositories: readonly RepositoryDto[]) => void;
  readonly pluginNavigation?: ReactNode;
}

export function RepositoryNavigation({
  bridge,
  queries,
  selected,
  navigate,
  onDiscovery,
  pluginNavigation,
}: Props): ReactNode {
  const begin = useCallback(() => bridge.discoverRepositories(), [bridge]);
  const state = useRetainedRead(queries, "repositories", begin, [bridge]);
  const repositories = state.value?.repositories ?? [];
  const [search, setSearch] = useState("");
  const [forge, setForge] = useState<RepositoryForgeFilter>("all");
  const [sort, setSort] = useState<RepositorySort>("name");
  const visibleRepositories = filterAndSortRepositories(
    repositories,
    search,
    forge,
    sort,
  );
  useEffect(() => {
    if (state.value) onDiscovery(state.value.repositories);
  }, [onDiscovery, state.value]);

  const focusFirstRepository = (
    event: KeyboardEvent<HTMLInputElement>,
  ): void => {
    if (event.key !== "Enter") return;
    const target = event.currentTarget
      .closest("aside")
      ?.querySelector<HTMLButtonElement>("button.repository-choice");
    if (target) {
      event.preventDefault();
      target.focus();
    }
  };
  const moveRepositoryFocus = (event: KeyboardEvent<HTMLDivElement>): void => {
    if (
      event.key !== "ArrowDown" &&
      event.key !== "ArrowUp" &&
      event.key !== "Home" &&
      event.key !== "End"
    )
      return;
    const buttons = [
      ...event.currentTarget.querySelectorAll<HTMLButtonElement>(
        "button.repository-choice",
      ),
    ];
    const current = buttons.indexOf(
      document.activeElement as HTMLButtonElement,
    );
    const direction = event.key === "ArrowDown" ? 1 : -1;
    const target =
      event.key === "Home"
        ? buttons[0]
        : event.key === "End"
          ? buttons.at(-1)
          : buttons[
              (current < 0 ? 0 : current + direction + buttons.length) %
                buttons.length
            ];
    if (target) {
      event.preventDefault();
      target.focus();
    }
  };

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
      <div className="repository-controls" aria-label="Repository controls">
        <label className="repository-control repository-search">
          <span>Search repositories</span>
          <input
            type="search"
            placeholder="Filter local repositories"
            value={search}
            onChange={(event) => setSearch(event.currentTarget.value)}
            onKeyDown={focusFirstRepository}
          />
        </label>
        <label className="repository-control">
          <span>Forge</span>
          <select
            value={forge}
            onChange={(event) =>
              setForge(event.currentTarget.value as RepositoryForgeFilter)
            }
          >
            <option value="all">All forges</option>
            <option value="github">GitHub</option>
            <option value="gitlab">GitLab</option>
          </select>
        </label>
        <label className="repository-control">
          <span>Sort repositories</span>
          <select
            value={sort}
            onChange={(event) =>
              setSort(event.currentTarget.value as RepositorySort)
            }
          >
            <option value="name">Name</option>
            <option value="forge">Forge</option>
            <option value="host">Host</option>
          </select>
        </label>
      </div>
      <p className="repository-count" aria-live="polite">
        Showing {visibleRepositories.length} of {repositories.length} repositories
      </p>
      <button
        className={`nav-item ${selected === null ? "nav-item-active" : ""}`}
        onClick={() => navigate({ kind: "inbox", repository: null })}
      >
        All reviews
      </button>
      <div className="repository-list" onKeyDown={moveRepositoryFocus}>
        {visibleRepositories.map((repository) => (
          <button
            key={repository.handle}
            className={`nav-item repository-choice ${selected?.handle === repository.handle ? "nav-item-active" : ""}`}
            data-forge={repository.forge_type}
            data-hostname={repository.hostname}
            data-repository-meta={`${repository.forge_type}${repository.hostname ? ` · ${repository.hostname}` : ""}`}
            aria-label={repository.display_name}
            title={`${repository.display_name} · ${repository.forge_type}${repository.hostname ? ` · ${repository.hostname}` : ""}`}
            onClick={() => navigate({ kind: "inbox", repository })}
          >
            <span>{repository.display_name}</span>
          </button>
        ))}
      </div>
      {state.loading && !state.value && (
        <Notice kind="loading">Finding admitted repositories…</Notice>
      )}
      {Boolean(state.error) && (
        <>
          <Notice kind="error">
            {state.value
              ? "Refresh failed. Showing the previous repository list."
              : safeError(state.error)}
          </Notice>
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
      {repositories.length > 0 && visibleRepositories.length === 0 && (
        <Notice kind="empty">
          No local repositories match the current search and forge filter.
        </Notice>
      )}
      {pluginNavigation}
    </aside>
  );
}

export function filterAndSortRepositories(
  repositories: readonly RepositoryDto[],
  search: string,
  forge: RepositoryForgeFilter,
  sort: RepositorySort,
): readonly RepositoryDto[] {
  const query = search.trim().toLocaleLowerCase();
  const filtered = repositories.filter(
    (repository) =>
      (forge === "all" || repository.forge_type === forge) &&
      repository.display_name.toLocaleLowerCase().includes(query),
  );
  filtered.sort((left, right) => {
    let order = 0;
    if (sort === "forge")
      order = compareText(left.forge_type, right.forge_type);
    else if (sort === "host") {
      if (left.hostname && !right.hostname) return -1;
      if (!left.hostname && right.hostname) return 1;
      if (left.hostname && right.hostname)
        order = compareText(left.hostname, right.hostname);
    }
    return (
      order ||
      compareText(left.display_name, right.display_name) ||
      compareText(left.handle, right.handle)
    );
  });
  return Object.freeze(filtered);
}

function compareText(left: string, right: string): number {
  return left.localeCompare(right, undefined, { sensitivity: "base" });
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
