"""The terminal adapter lists each discovered forge project once."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from tongs.scanner.repo import ForgeType, Remote, Repo
from tongs.services.errors import ServiceError, ServiceErrorCode
from tongs.services.models import RepositoryRef, RepositorySnapshot
from tongs.services.session import ApplicationSession
from tongs.tui_services import TUIServiceAdapter, one_checkout_per_project


def _repo(
    path: str,
    project: str = "acme/widgets",
    *,
    worktree: bool = False,
    hostname: str = "github.com",
) -> Repo:
    remote = Remote(
        "origin",
        f"https://{hostname}/{project}.git",
        hostname,
        project,
        ForgeType.GITHUB,
    )
    return Repo(Path(path), (remote,), remote, worktree=worktree)


def test_main_checkout_wins_over_worktrees_in_any_order() -> None:
    first_worktree = _repo("/src/a-feature", worktree=True)
    main = _repo("/src/z-main")
    second_worktree = _repo("/src/b-fix", worktree=True)

    kept = one_checkout_per_project((first_worktree, main, second_worktree))

    assert kept == (main,)


def test_worktrees_alone_keep_the_first_by_path() -> None:
    later = _repo("/src/zz", worktree=True)
    earlier = _repo("/src/aa", worktree=True)

    assert one_checkout_per_project((later, earlier)) == (earlier,)


def test_two_main_checkouts_keep_the_first_by_path() -> None:
    later = _repo("/src/zz")
    earlier = _repo("/src/aa")

    assert one_checkout_per_project((later, earlier)) == (earlier,)


def test_other_projects_and_hosts_stay_in_order() -> None:
    widgets = _repo("/src/widgets")
    widgets_worktree = _repo("/src/widgets-wt", worktree=True)
    gadgets = _repo("/src/gadgets", "acme/gadgets")
    other_host = _repo("/src/widgets-gl", hostname="gitlab.com")

    kept = one_checkout_per_project((widgets_worktree, gadgets, widgets, other_host))

    assert kept == (widgets, gadgets, other_host)


def test_repositories_without_a_remote_are_kept() -> None:
    first = Repo(Path("/src/one"), ())
    second = Repo(Path("/src/two"), ())

    assert one_checkout_per_project((first, second)) == (first, second)


class _DiscoveringSession:
    def __init__(self, repositories: tuple[Repo, ...]) -> None:
        self.local_repositories = repositories

    async def discover_repositories(self) -> tuple[RepositorySnapshot, ...]:
        ref = RepositoryRef("github.com", "acme/widgets")
        return (RepositorySnapshot(ref, "acme/widgets", ForgeType.GITHUB),)


@pytest.mark.asyncio
async def test_discovery_publishes_one_row_but_the_session_keeps_all() -> None:
    worktree = _repo("/src/a-feature", worktree=True)
    main = _repo("/src/main")
    session = _DiscoveringSession((worktree, main))
    adapter = TUIServiceAdapter(cast(ApplicationSession, session))

    result = await adapter.discover_repositories()

    assert result.repositories == (main,)
    assert adapter.repositories == (main,)
    assert adapter.repository_ref(main) == RepositoryRef("github.com", "acme/widgets")
    assert session.local_repositories == (worktree, main)


class _OpeningSession(_DiscoveringSession):
    """Discovers ``acme/widgets`` and opens any other project it is asked to."""

    def __init__(self, repositories: tuple[Repo, ...]) -> None:
        super().__init__(repositories)
        self.opened: list[tuple[str, str]] = []
        self.error: ServiceError | None = None

    async def open_repository(
        self, hostname: str, project_path: str
    ) -> RepositorySnapshot:
        self.opened.append((hostname, project_path))
        if self.error is not None:
            raise self.error
        ref = RepositoryRef(hostname, project_path)
        return RepositorySnapshot(ref, project_path, ForgeType.GITLAB)


async def _opening_adapter(
    *repositories: Repo,
) -> tuple[TUIServiceAdapter, _OpeningSession]:
    session = _OpeningSession(repositories or (_repo("/src/widgets"),))
    adapter = TUIServiceAdapter(cast(ApplicationSession, session))
    await adapter.discover_repositories()
    return adapter, session


@pytest.mark.asyncio
async def test_opening_a_project_keeps_it_for_the_run() -> None:
    adapter, session = await _opening_adapter()

    result = await adapter.open_repository("gitlab.com", "group/sub/project")

    ref = RepositoryRef("gitlab.com", "group/sub/project")
    assert result.snapshot.ref == ref
    assert not result.already_local
    assert adapter.opened_repositories == (result.snapshot,)
    assert adapter.has_repository(ref)
    assert adapter.forge_name("gitlab.com") == "GitLab"
    assert session.opened == [("gitlab.com", "group/sub/project")]


@pytest.mark.asyncio
async def test_a_listed_project_is_returned_without_a_forge_read() -> None:
    adapter, session = await _opening_adapter()
    opened = await adapter.open_repository("gitlab.com", "group/project")

    local = await adapter.open_repository("github.com", "ACME/Widgets")
    again = await adapter.open_repository("gitlab.com", "Group/Project")

    assert local.already_local
    assert local.snapshot == adapter.repository_snapshot(adapter.repositories[0])
    assert again == opened
    assert session.opened == [("gitlab.com", "group/project")]
    assert adapter.opened_repositories == (opened.snapshot,)


@pytest.mark.asyncio
async def test_a_failed_open_keeps_nothing() -> None:
    adapter, session = await _opening_adapter()
    session.error = ServiceError(ServiceErrorCode.NOT_FOUND, "Not found.")

    with pytest.raises(ServiceError) as raised:
        await adapter.open_repository("gitlab.com", "group/project")

    assert raised.value.code is ServiceErrorCode.NOT_FOUND
    assert adapter.opened_repositories == ()


@pytest.mark.asyncio
async def test_an_invalid_identity_is_refused_before_the_session() -> None:
    adapter, session = await _opening_adapter()

    with pytest.raises(ServiceError) as raised:
        await adapter.open_repository("GitHub.com", "acme/gadgets")

    assert raised.value.code is ServiceErrorCode.INVALID_INPUT
    assert session.opened == []


@pytest.mark.asyncio
async def test_removing_forgets_only_the_opened_project() -> None:
    adapter, _session = await _opening_adapter()
    opened = await adapter.open_repository("gitlab.com", "group/project")
    local_ref = adapter.repository_ref(adapter.repositories[0])

    adapter.remove_repository(opened.snapshot.ref)
    adapter.remove_repository(local_ref)

    assert adapter.opened_repositories == ()
    assert not adapter.has_repository(opened.snapshot.ref)
    assert adapter.has_repository(local_ref)


@pytest.mark.asyncio
async def test_a_project_cloned_after_opening_is_listed_as_local() -> None:
    adapter, session = await _opening_adapter()
    opened = await adapter.open_repository("github.com", "Acme/Gadgets")
    gadgets = _repo("/src/gadgets", "acme/gadgets")
    session.local_repositories = (*session.local_repositories, gadgets)
    gadgets_ref = RepositoryRef("github.com", "acme/gadgets")
    widgets_ref = RepositoryRef("github.com", "acme/widgets")

    async def discover() -> tuple[RepositorySnapshot, ...]:
        return (
            RepositorySnapshot(widgets_ref, "acme/widgets", ForgeType.GITHUB),
            RepositorySnapshot(gadgets_ref, "acme/gadgets", ForgeType.GITHUB),
        )

    session.discover_repositories = discover  # type: ignore[method-assign]
    await adapter.discover_repositories()

    assert adapter.opened_repositories == ()
    # An inbox opened with the typed case still finds its project.
    assert adapter.has_repository(opened.snapshot.ref)
