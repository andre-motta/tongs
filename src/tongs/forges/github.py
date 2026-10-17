"""GitHub forge client implementation."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from urllib.parse import quote as urlquote

import httpx

from tongs.errors import (
    AuthError,
    ConflictError,
    ForgeError,
    ForgePermissionError,
    NetworkError,
    NotFoundError,
    PartialResultsError,
    RateLimitError,
    ValidationError,
    redact_credentials,
)
from tongs.forges.base import ForgeClient, is_full_commit_sha
from tongs.forges.http import (
    graphql_rate_limit_error,
    map_http_error,
    paginate,
    request,
    request_bytes,
    request_log_tail,
    request_page,
)
from tongs.forges.models import (
    CIStatus,
    Commit,
    Discussion,
    ForgeHost,
    ForgeMergeResult,
    ForgeMutationResult,
    InlineComment,
    JobLogTail,
    MRDetail,
    MRPage,
    MRState,
    MRSummary,
    Pipeline,
    PipelineJob,
    ReviewDecision,
    SourceCleanupStatus,
    User,
)

# The pull request files API lists at most this many files.
_PR_FILES_CAP = 3000

#: The most bytes read from the compare call that finds a merge base. The
#: paged call answers about 15 KB; an unpaged one can pass 1 MB, so dropping
#: the paging parameters fails closed.
MERGE_BASE_MAX_BYTES = 512 * 1024

# The search API returns at most this many results for one query, in pages of
# at most 100.
_SEARCH_PAGE_SIZE = 100
_SEARCH_RESULT_CAP = 1000

# Check runs and commit statuses are each read in pages of 100, up to this
# many pages per commit.
_CHECK_RUNS_PAGE_SIZE = 100
_CHECK_RUNS_MAX_PAGES = 10

# The state of one legacy commit status (Jenkins, Prow and similar). Only
# GraphQL reports "expected": a required status that has not reported yet,
# which is still pending work.
_COMMIT_STATUS_STATES = {
    "success": CIStatus.SUCCESS,
    "failure": CIStatus.FAILED,
    "error": CIStatus.FAILED,
    "pending": CIStatus.PENDING,
    "expected": CIStatus.PENDING,
}

# A CI source the token may not read counts as empty when the other answers:
# a token without the commit statuses permission still shows its check runs.
_UNREADABLE_CI_SOURCE = (ForgePermissionError, NotFoundError)

# Per-pull-request CI reads share one GraphQL query of up to this many pull
# requests, across repositories. A batch goes out once no read arrived for
# the window, at most the longest wait after its first read, or at once when
# it is full. Each pull request reads up to 100 checks, so a large query is
# slow (about 14 s for 50 on busy repositories); smaller batches sent side by
# side fill a screen sooner for the same number of requests per row.
_CI_BATCH_WINDOW_SECONDS = 0.015
_CI_BATCH_MAX_WAIT_SECONDS = 0.06
_CI_BATCH_SIZE = 15
_CI_BATCHES_IN_FLIGHT = 4
# A head commit with more checks than one query reads (the ``contexts(first:
# 100)`` of the fragment below) is read over REST.
_CI_ROLLUP_PAGE_SIZE = 100
# A 404 from GraphQL means the host has none, so CI is read over REST from
# then on. A 403 reads that batch over REST only; this many in a row (with
# no batch answered between them) pause batching for the given time.
_CI_GRAPHQL_FORBIDDEN_LIMIT = 2
_CI_GRAPHQL_PAUSE_SECONDS = 600.0

# The head commit and its checks for one pull request of a CI batch. Check
# runs and commit statuses are classified like the REST reads.
_CI_ROLLUP_FRAGMENT = """
fragment ci on PullRequest {
    headRefOid
    commits(last: 1) {
        nodes {
            commit {
                oid
                statusCheckRollup {
                    contexts(first: 100) {
                        totalCount
                        nodes {
                            __typename
                            ... on CheckRun { status conclusion }
                            ... on StatusContext { state }
                        }
                    }
                }
            }
        }
    }
}
"""

# Review threads are read in GraphQL pages of 100, up to this many pages.
_REVIEW_THREADS_MAX_PAGES = 50

# One page of a pull request's review threads, with each thread's resolution
# and the database ids of its first comments.  Resolving a thread and reading
# the resolution of every thread share this shape.
_REVIEW_THREADS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
    repository(owner: $owner, name: $repo) {
        pullRequest(number: $number) {
            reviewThreads(first: 100, after: $cursor) {
                nodes {
                    id
                    isResolved
                    comments(first: 100) {
                        nodes { databaseId }
                        pageInfo { hasNextPage endCursor }
                    }
                }
                pageInfo { hasNextPage endCursor }
            }
        }
    }
}
"""


def _files_overflowed(changed_files: object) -> bool:
    """Return whether a pull request has more files than the files API lists."""
    return (
        isinstance(changed_files, int)
        and not isinstance(changed_files, bool)
        and changed_files > _PR_FILES_CAP
    )


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _parse_ci_status(status: str | None, conclusion: str | None) -> CIStatus:
    if not status:
        return CIStatus.UNKNOWN
    status = status.lower()
    if status == "completed":
        conclusion = (conclusion or "").lower()
        return {
            "success": CIStatus.SUCCESS,
            "failure": CIStatus.FAILED,
            "cancelled": CIStatus.CANCELED,
            "skipped": CIStatus.SKIPPED,
            "timed_out": CIStatus.FAILED,
            "action_required": CIStatus.PENDING,
        }.get(conclusion, CIStatus.UNKNOWN)
    return {
        "queued": CIStatus.PENDING,
        "in_progress": CIStatus.RUNNING,
        "waiting": CIStatus.PENDING,
        "requested": CIStatus.PENDING,
        "pending": CIStatus.PENDING,
    }.get(status, CIStatus.UNKNOWN)


def _check_run_state(run: dict) -> CIStatus:
    """Classify one check run for the commit-level aggregate.

    This differs from ``_parse_ci_status``, which also maps pipeline and job
    rows: a neutral check counts as passing, and the check-run-only
    conclusions ``startup_failure`` and ``stale`` get explicit states.
    """
    status = str(run.get("status") or "").lower()
    if status != "completed":
        return _parse_ci_status(status, None)
    return {
        "success": CIStatus.SUCCESS,
        "neutral": CIStatus.SUCCESS,
        "skipped": CIStatus.SKIPPED,
        "failure": CIStatus.FAILED,
        "timed_out": CIStatus.FAILED,
        "startup_failure": CIStatus.FAILED,
        "cancelled": CIStatus.CANCELED,
        "action_required": CIStatus.PENDING,
        # GitHub marks a check stale when it waited too long; it needs a rerun.
        "stale": CIStatus.PENDING,
    }.get(str(run.get("conclusion") or "").lower(), CIStatus.UNKNOWN)


def _commit_status_state(status: object) -> CIStatus:
    """Classify one legacy commit status for the commit-level aggregate."""
    state = status.get("state") if isinstance(status, dict) else None
    return _COMMIT_STATUS_STATES.get(str(state or "").lower(), CIStatus.UNKNOWN)


def _aggregate_ci_states(statuses: list[CIStatus]) -> CIStatus:
    """Combine the states of a commit's check runs and commit statuses.

    Any failure wins, then running, then pending work. Skipped and neutral
    checks do not block success; a commit whose checks were all skipped is
    SKIPPED. A commit with no check run and no commit status is UNKNOWN.
    """
    if not statuses:
        return CIStatus.UNKNOWN
    for blocking in (
        CIStatus.FAILED,
        CIStatus.RUNNING,
        CIStatus.PENDING,
        CIStatus.UNKNOWN,
        CIStatus.CANCELED,
    ):
        if blocking in statuses:
            return blocking
    if all(status == CIStatus.SKIPPED for status in statuses):
        return CIStatus.SKIPPED
    return CIStatus.SUCCESS


def _head_sha(head: object) -> str:
    """Return a pull request head's commit SHA, or an empty string."""
    sha = head.get("sha") if isinstance(head, dict) else None
    return sha if isinstance(sha, str) else ""


def _rollup_context_state(node: object) -> CIStatus:
    """Classify one check of a GraphQL status check rollup.

    A check run and a commit status map exactly as their REST reads do, so a
    batched read and a REST read agree. A commit status GraphQL reports as
    EXPECTED (required, not reported yet) is PENDING.
    """
    if not isinstance(node, dict):
        return CIStatus.UNKNOWN
    kind = node.get("__typename")
    if kind == "CheckRun":
        return _check_run_state(
            {"status": node.get("status"), "conclusion": node.get("conclusion")}
        )
    if kind == "StatusContext":
        return _commit_status_state({"state": node.get("state")})
    return CIStatus.UNKNOWN


def _rollup_ci_status(pull: object, head_sha: str) -> CIStatus | None:
    """Return a batched pull request's CI, or None to read it over REST.

    The answer is used only for the head commit asked for (the pull request's
    current head when none was named) and only when every check was read.
    """
    if not isinstance(pull, dict):
        return None
    commits = pull.get("commits")
    nodes = commits.get("nodes") if isinstance(commits, dict) else None
    if not isinstance(nodes, list) or len(nodes) != 1:
        return None
    commit = nodes[0].get("commit") if isinstance(nodes[0], dict) else None
    if not isinstance(commit, dict):
        return None
    oid = commit.get("oid")
    wanted = head_sha or pull.get("headRefOid")
    if not isinstance(oid, str) or not oid or oid != wanted:
        return None
    rollup = commit.get("statusCheckRollup")
    if rollup is None:
        # No check run and no commit status, as the REST reads would find.
        return CIStatus.UNKNOWN
    contexts = rollup.get("contexts") if isinstance(rollup, dict) else None
    if not isinstance(contexts, dict):
        return None
    total = contexts.get("totalCount")
    checks = contexts.get("nodes")
    if (
        type(total) is not int
        or not isinstance(checks, list)
        or total > _CI_ROLLUP_PAGE_SIZE
        or len(checks) < total
    ):
        return None
    return _aggregate_ci_states([_rollup_context_state(node) for node in checks])


def _graphql_error_paths(errors: object) -> tuple[tuple[str, ...], ...] | None:
    """Return the alias path of each GraphQL error, or None for one without.

    An error without a path concerns the whole query.
    """
    if not errors:
        return ()
    if not isinstance(errors, list):
        return None
    paths: list[tuple[str, ...]] = []
    for error in errors:
        path = error.get("path") if isinstance(error, dict) else None
        if not isinstance(path, list) or not path:
            return None
        paths.append(tuple(str(part) for part in path[:2]))
    return tuple(paths)


@dataclass
class _CIRead:
    """One pull request's CI read waiting for, or riding in, a GraphQL batch.

    The future resolves to the status, to None when the read falls back to
    REST, or to the error that failed the whole batch.
    """

    owner: str
    repo: str
    number: int
    head_sha: str
    future: asyncio.Future[CIStatus | None]


def _parse_mr_state(value: str) -> MRState:
    return {
        "open": MRState.OPEN,
        "closed": MRState.CLOSED,
        "merged": MRState.MERGED,
    }.get(value.lower(), MRState.OPEN)


def _parse_pr_state(data: dict) -> MRState:
    """Read a pull request's state, reporting a merged one as merged.

    GitHub reports a merged pull request with state ``closed``.  The single
    pull request response also carries ``merged``, but list and search items
    do not, so ``merged_at`` is the signal every response shares.
    """
    if data.get("merged") is True or data.get("merged_at"):
        return MRState.MERGED
    state = data.get("state")
    return _parse_mr_state(state if isinstance(state, str) else "open")


def _parse_review_decision(value: str | None) -> ReviewDecision:
    if not value:
        return ReviewDecision.NONE
    return {
        "APPROVED": ReviewDecision.APPROVED,
        "CHANGES_REQUESTED": ReviewDecision.CHANGES_REQUESTED,
        "REVIEW_REQUIRED": ReviewDecision.REVIEW_REQUIRED,
    }.get(value, ReviewDecision.NONE)


def _parse_user(data: dict | None) -> User:
    if not data:
        return User(username="unknown")
    return User(
        username=data.get("login", "unknown"),
        display_name=data.get("name", ""),
    )


def _split_repo_path(repo_path: str) -> tuple[str, str]:
    """Split 'owner/repo' into (owner, repo)."""
    parts = repo_path.split("/", 1)
    if len(parts) != 2:
        return repo_path, ""
    return parts[0], parts[1]


def _github_cleanup_target(
    value: dict | list,
    *,
    repo_path: str,
    head_sha: str | None,
    expected_source_repository: str | None,
    expected_source_branch: str | None,
    expected_target_branch: str | None,
) -> tuple[str | None, str | None, SourceCleanupStatus]:
    if not isinstance(value, dict):
        raise TypeError("invalid GitHub pull request response")
    head = value.get("head")
    base = value.get("base")
    if not isinstance(head, dict) or not isinstance(base, dict):
        raise TypeError("invalid GitHub pull request response")
    head_repo = head.get("repo")
    base_repo = base.get("repo")
    if not isinstance(head_repo, dict) or not isinstance(base_repo, dict):
        raise TypeError("invalid GitHub pull request response")
    source_repository = head_repo.get("full_name")
    source_branch = head.get("ref")
    source_head = head.get("sha")
    target_repository = base_repo.get("full_name")
    target_branch = base.get("ref")
    default_branch = base_repo.get("default_branch")
    if not all(
        isinstance(item, str) and item
        for item in (
            source_repository,
            source_branch,
            source_head,
            target_repository,
            target_branch,
        )
    ):
        raise ValueError("invalid GitHub pull request response")
    if target_repository.casefold() != repo_path.casefold():
        raise ConflictError("GitHub pull request target repository changed")
    if head_sha is not None and source_head != head_sha:
        raise ConflictError("GitHub pull request head changed")
    if (
        expected_source_repository is not None
        and source_repository.casefold() != expected_source_repository.casefold()
    ):
        raise ConflictError("GitHub pull request source repository changed")
    if expected_source_branch is not None and source_branch != expected_source_branch:
        raise ConflictError("GitHub pull request source branch changed")
    if expected_target_branch is not None and target_branch != expected_target_branch:
        raise ConflictError("GitHub pull request target branch changed")
    if (
        source_repository.casefold() != repo_path.casefold()
        or source_branch == target_branch
        or not isinstance(default_branch, str)
        or not default_branch
        or source_branch == default_branch
    ):
        return None, None, SourceCleanupStatus.REJECTED
    return source_branch, source_head, SourceCleanupStatus.UNKNOWN


def _github_ref_sha(value: dict | list) -> str:
    if not isinstance(value, dict) or not isinstance(value.get("object"), dict):
        raise TypeError("invalid GitHub reference response")
    sha = value["object"].get("sha")
    if not isinstance(sha, str) or not sha:
        raise ValueError("invalid GitHub reference response")
    return sha


def _github_state_result(
    value: dict | list, number: int, expected_state: str
) -> ForgeMutationResult:
    if (
        not isinstance(value, dict)
        or type(value.get("number")) is not int
        or value.get("number") != number
        or value.get("state") != expected_state
        or type(value.get("id")) is not int
        or value["id"] <= 0
    ):
        raise ValueError("invalid GitHub pull request state response")
    return ForgeMutationResult(str(value["id"]))


class GitHubClient(ForgeClient):
    """GitHub REST + GraphQL API client."""

    def __init__(
        self,
        host: ForgeHost,
        http_client: httpx.AsyncClient,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._host = host
        self._http = http_client
        self._clock = clock
        # Per-pull-request CI reads waiting for the next GraphQL batch.
        self._ci_reads: list[_CIRead] = []
        self._ci_timer: asyncio.TimerHandle | None = None
        self._ci_batch_deadline = 0.0
        self._ci_batches: set[asyncio.Task[None]] = set()
        # False once the host answered that it has no GraphQL API.
        self._ci_graphql = True
        # GraphQL refusals (403) in a row, and the ``clock`` time until which
        # repeated refusals pause batching.
        self._ci_forbidden = 0
        self._ci_paused_until = 0.0

    @property
    def _graphql_url(self) -> str:
        if self._host.hostname == "github.com":
            return "https://api.github.com/graphql"
        return f"https://{self._host.hostname}/api/graphql"

    async def _graphql_response(
        self, query: str, variables: dict | None = None
    ) -> dict:
        """Send a GraphQL query and return the whole response document.

        A failed request or a rate-limit error raises ForgeError; other
        errors stay in the document's ``errors`` beside any ``data``.
        """
        payload: dict = {"query": query}
        if variables:
            payload["variables"] = variables
        try:
            response = await self._http.post(self._graphql_url, json=payload)
        except httpx.TimeoutException as e:
            raise NetworkError(
                f"GraphQL request timed out: {redact_credentials(str(e))}"
            ) from e
        except httpx.TransportError as e:
            raise NetworkError(
                f"GraphQL transport error: {redact_credentials(str(e))}"
            ) from e
        if response.status_code >= 400:
            raise map_http_error(response)
        data = response.json()
        if not isinstance(data, dict):
            raise TypeError("GraphQL response is not an object")
        rate_limited = graphql_rate_limit_error(response, data.get("errors"))
        if rate_limited is not None:
            raise rate_limited
        return data

    async def _graphql(self, query: str, variables: dict | None = None) -> dict:
        """Execute a GraphQL query against GitHub's API."""
        data = await self._graphql_response(query, variables)
        errors = data.get("errors")
        if errors:
            first = errors[0] if isinstance(errors, list) else None
            msg = first.get("message") if isinstance(first, dict) else None
            if not isinstance(msg, str) or not msg:
                msg = "GraphQL error"
            raise ForgeError(redact_credentials(msg))
        return data.get("data", {})

    async def list_mrs(
        self,
        repo_path: str,
        state: str = "open",
        per_page: int = 100,
    ) -> list[MRSummary]:
        owner, repo = _split_repo_path(repo_path)
        data = await paginate(
            self._http,
            f"/repos/{owner}/{repo}/pulls",
            per_page=per_page,
            params={"state": state},
        )
        ci_results = await asyncio.gather(
            *(self._fetch_pr_ci_status(owner, repo, pr) for pr in data)
        )
        return [
            self._parse_pr_summary(pr, repo_path, ci_status=ci)
            for pr, ci in zip(data, ci_results)
        ]

    async def list_mrs_page(
        self,
        repo_path: str,
        state: str = "open",
        page: int = 1,
        per_page: int = 100,
        *,
        with_ci: bool = True,
    ) -> MRPage:
        """Read one page of pull requests, most recently updated first.

        CI status is fetched only for the pull requests on this page, and not
        at all when ``with_ci`` is false: each row then has UNKNOWN CI and its
        head commit for ``get_mr_ci_status``.
        """
        owner, repo = _split_repo_path(repo_path)
        data, has_next = await request_page(
            self._http,
            f"/repos/{owner}/{repo}/pulls",
            page=page,
            per_page=per_page,
            params={"state": state, "sort": "updated", "direction": "desc"},
        )
        if not with_ci:
            return MRPage(
                tuple(self._parse_pr_summary(pr, repo_path) for pr in data),
                has_next=has_next,
            )
        ci_results = await asyncio.gather(
            *(self._fetch_pr_ci_status(owner, repo, pr) for pr in data)
        )
        return MRPage(
            tuple(
                self._parse_pr_summary(pr, repo_path, ci_status=ci)
                for pr, ci in zip(data, ci_results)
            ),
            has_next=has_next,
        )

    async def get_mr_ci_status(
        self, repo_path: str, number: int, head_sha: str = ""
    ) -> CIStatus:
        """Read the CI status of one pull request's head commit.

        Reads that arrive together share one GraphQL query (see
        ``_batched_ci_status``). A read the batch cannot answer reads check
        runs and commit statuses over REST (two requests, more only past 100
        of either) and combines them the same way; without ``head_sha`` the
        pull request is read first to find its head. A failed request raises,
        so a caller never caches a failure as a status; a head with no usable
        check run or commit status is UNKNOWN.
        """
        owner, repo = _split_repo_path(repo_path)
        status = await self._batched_ci_status(owner, repo, number, head_sha)
        if status is not None:
            return status
        if not head_sha:
            data = await request(
                self._http, "GET", f"/repos/{owner}/{repo}/pulls/{number}"
            )
            head = data.get("head") if isinstance(data, dict) else None
            sha = head.get("sha") if isinstance(head, dict) else None
            if not isinstance(sha, str) or not sha:
                return CIStatus.UNKNOWN
            head_sha = sha
        return await self._read_ci_status(owner, repo, head_sha)

    async def list_my_reviews(self) -> list[MRSummary]:
        return await self._search_prs("is:pr is:open review-requested:@me archived:false")

    async def list_my_mrs(self) -> list[MRSummary]:
        return await self._search_prs("is:pr is:open author:@me archived:false")

    async def _search_prs(self, query: str) -> list[MRSummary]:
        """List the pull requests a search matches.

        Search pages are followed up to GitHub's 1000-result cap. Raises
        PartialResultsError, carrying the pull requests that were read, when a
        later search page or an individual pull request cannot be read, or
        when the cap or a search timeout truncates the results.
        """
        hits: list[tuple[str, int]] = []
        seen: set[tuple[str, int]] = set()
        truncation: ForgeError | None = None
        max_pages = _SEARCH_RESULT_CAP // _SEARCH_PAGE_SIZE
        for page in range(1, max_pages + 1):
            try:
                data = await request(
                    self._http,
                    "GET",
                    "/search/issues",
                    params={"q": query, "per_page": _SEARCH_PAGE_SIZE, "page": page},
                )
            except ForgeError as error:
                if page == 1:
                    raise
                truncation = error
                break
            if not isinstance(data, dict):
                error = ForgeError("GitHub returned an invalid search response")
                if page == 1:
                    raise error
                truncation = error
                break
            items = data.get("items", [])
            if not isinstance(items, list):
                items = []
            for item in items:
                hit = self._search_hit(item)
                if hit is not None and hit not in seen:
                    seen.add(hit)
                    hits.append(hit)
            total_count = data.get("total_count")
            if data.get("incomplete_results") is True:
                truncation = ForgeError("GitHub search timed out before it finished")
                break
            if type(total_count) is int and total_count > _SEARCH_RESULT_CAP:
                truncation = ForgeError(
                    f"GitHub search matched {total_count} results; "
                    f"only the first {_SEARCH_RESULT_CAP} are listed"
                )
            if len(items) < _SEARCH_PAGE_SIZE or (
                type(total_count) is int
                and page * _SEARCH_PAGE_SIZE >= min(total_count, _SEARCH_RESULT_CAP)
            ):
                break

        fetched = await asyncio.gather(
            *(self._fetch_search_hit(repo_path, number) for repo_path, number in hits)
        )
        results = [item for item in fetched if isinstance(item, MRSummary)]
        failures = [item for item in fetched if isinstance(item, ForgeError)]
        if failures:
            raise PartialResultsError(
                results,
                failures[0],
                f"{len(failures)} of {len(hits)} pull requests from the GitHub "
                "search could not be read",
            )
        if truncation is not None:
            raise PartialResultsError(results, truncation)
        return results

    def _search_hit(self, item: object) -> tuple[str, int] | None:
        """Return the repository path and number of one search result."""
        if not isinstance(item, dict):
            return None
        repo_url = item.get("repository_url", "")
        number = item.get("number")
        if not repo_url or not isinstance(repo_url, str):
            return None
        if type(number) is not int or number <= 0:
            return None
        repo_path = self._repo_path_from_api_url(repo_url)
        if not repo_path:
            return None
        return repo_path, number

    async def _fetch_search_hit(
        self, repo_path: str, number: int
    ) -> MRSummary | ForgeError:
        """Read one search result's pull request, returning a failure as a value."""
        owner, repo = _split_repo_path(repo_path)
        try:
            pr_data = await request(
                self._http,
                "GET",
                f"/repos/{owner}/{repo}/pulls/{number}",
            )
            if not isinstance(pr_data, dict):
                raise TypeError("expected a pull request object")
            return self._parse_pr_summary(pr_data, repo_path)
        except ForgeError as error:
            return error
        except (ValueError, KeyError, TypeError, AttributeError):
            return ForgeError("GitHub returned an unreadable pull request")

    def _repo_path_from_api_url(self, api_url: str) -> str:
        """Extract 'owner/repo' from a GitHub API URL like https://api.github.com/repos/owner/repo."""
        prefix = str(self._http.base_url).rstrip("/") + "/repos/"
        if api_url.startswith(prefix):
            return api_url[len(prefix) :]
        return ""

    async def get_mr(self, repo_path: str, number: int) -> MRDetail:
        import asyncio

        owner, repo = _split_repo_path(repo_path)
        data = await request(
            self._http,
            "GET",
            f"/repos/{owner}/{repo}/pulls/{number}",
        )
        sha = data.get("head", {}).get("sha", "")
        ci_task = asyncio.create_task(self._fetch_ci_status(owner, repo, sha))
        approvals_task = asyncio.create_task(self._fetch_approvals(owner, repo, number))
        ci_status = await ci_task
        approvals = await approvals_task
        detail = self._parse_pr_detail(data, repo_path)
        return MRDetail(
            **{
                f.name: getattr(detail, f.name)
                for f in fields(detail)
                if f.name not in ("ci_status", "approvals")
            },
            ci_status=ci_status,
            approvals=approvals,
        )

    async def get_mr_revision(self, repo_path: str, number: int) -> MRDetail:
        """Read only the pull request, without check runs or review pages."""
        owner, repo = _split_repo_path(repo_path)
        data = await request(
            self._http,
            "GET",
            f"/repos/{owner}/{repo}/pulls/{number}",
        )
        return self._parse_pr_detail(data, repo_path)

    async def get_mr_diff(self, repo_path: str, number: int) -> list[dict]:
        owner, repo = _split_repo_path(repo_path)
        data = await paginate(
            self._http,
            f"/repos/{owner}/{repo}/pulls/{number}/files",
        )
        return data

    async def get_file_at_ref(
        self, repo_path: str, path: str, ref: str, *, max_bytes: int
    ) -> bytes:
        """Read one file's raw bytes at a commit through the contents API."""
        owner, repo = _split_repo_path(repo_path)
        return await request_bytes(
            self._http,
            f"/repos/{owner}/{repo}/contents/{urlquote(path, safe='/')}",
            max_bytes=max_bytes,
            params={"ref": ref},
            headers={"Accept": "application/vnd.github.raw"},
        )

    async def get_merge_base(self, repo_path: str, base_sha: str, head_sha: str) -> str:
        """Return the merge base of ``base_sha`` and ``head_sha``.

        The compare call asks for its second page of one commit, which drops
        the changed-file list, so the body stays small (about 15 KB); the
        read is still capped at ``MERGE_BASE_MAX_BYTES``. The compare runs on
        the base repository, which also resolves a head commit on a fork.
        """
        if not is_full_commit_sha(base_sha) or not is_full_commit_sha(head_sha):
            raise ValidationError("A merge base needs two full commit SHAs")
        owner, repo = _split_repo_path(repo_path)
        body = await request_bytes(
            self._http,
            f"/repos/{owner}/{repo}/compare/{base_sha}...{head_sha}",
            max_bytes=MERGE_BASE_MAX_BYTES,
            params={"per_page": "1", "page": "2"},
        )
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        commit = data.get("merge_base_commit") if isinstance(data, dict) else None
        sha = commit.get("sha") if isinstance(commit, dict) else None
        if not is_full_commit_sha(sha):
            raise NotFoundError("The forge returned no merge base")
        return sha

    async def list_mr_commits(self, repo_path: str, number: int) -> list[Commit]:
        owner, repo = _split_repo_path(repo_path)
        data = await paginate(
            self._http,
            f"/repos/{owner}/{repo}/pulls/{number}/commits",
        )
        return [
            Commit(
                sha=c.get("sha", ""),
                short_sha=c.get("sha", "")[:8],
                title=c.get("commit", {}).get("message", "").split("\n", 1)[0],
                message=c.get("commit", {}).get("message", ""),
                author=_parse_user(c.get("author")),
                created_at=_parse_datetime(
                    c.get("commit", {}).get("author", {}).get("date")
                ),
                web_url=c.get("html_url") or "",
            )
            for c in data
        ]

    async def get_mr_discussions(self, repo_path: str, number: int) -> list[Discussion]:
        owner, repo = _split_repo_path(repo_path)
        data = await paginate(
            self._http,
            f"/repos/{owner}/{repo}/pulls/{number}/comments",
        )
        roots = {
            comment["id"]: comment
            for comment in data
            if not comment.get("in_reply_to_id")
        }
        replies: dict[int, list[dict]] = {root_id: [] for root_id in roots}
        for comment in data:
            root_id = comment.get("in_reply_to_id")
            if root_id in replies:
                replies[root_id].append(comment)
        resolution = (
            await self._review_thread_resolution(owner, repo, number)
            if any(comment.get("path") for comment in roots.values())
            else {}
        )
        discussions = []
        for root_id, comment in roots.items():
            parsed = self._parse_review_comment(comment)
            is_inline = bool(comment.get("path"))
            is_resolved = is_inline and resolution.get(root_id, False)
            root = InlineComment(
                id=parsed.id,
                author=parsed.author,
                body=parsed.body,
                created_at=parsed.created_at,
                file_path=parsed.file_path,
                old_line=parsed.old_line,
                new_line=parsed.new_line,
                is_resolved=is_resolved,
                replies=tuple(
                    self._parse_review_comment(reply) for reply in replies[root_id]
                ),
            )
            discussions.append(
                Discussion(
                    id=str(root_id),
                    is_inline=is_inline,
                    root_comment=root,
                    is_resolved=is_resolved,
                    resolvable=is_inline,
                )
            )
        return discussions

    async def _review_thread_resolution(
        self, owner: str, repo: str, number: int
    ) -> dict[int, bool]:
        """Map the REST ids of review thread comments to their thread's resolution.

        Only the first page of each thread's comments is read, which always
        holds the thread's root comment.  Any GraphQL failure, including an
        unexpected response shape, degrades every thread to unresolved instead
        of failing the discussions read.
        """
        resolution: dict[int, bool] = {}
        cursor: str | None = None
        try:
            for _ in range(_REVIEW_THREADS_MAX_PAGES):
                data = await self._graphql(
                    _REVIEW_THREADS_QUERY,
                    {"owner": owner, "repo": repo, "number": number, "cursor": cursor},
                )
                connection = data["repository"]["pullRequest"]["reviewThreads"]
                for thread in connection["nodes"]:
                    is_resolved = thread["isResolved"] is True
                    for comment in thread["comments"]["nodes"]:
                        comment_id = comment.get("databaseId")
                        if type(comment_id) is int:
                            resolution[comment_id] = is_resolved
                page = connection["pageInfo"]
                cursor = page.get("endCursor")
                if page.get("hasNextPage") is not True or not cursor:
                    break
        except (ForgeError, ValueError, LookupError, TypeError, AttributeError):
            return {}
        return resolution

    async def create_inline_comment(
        self,
        repo_path: str,
        number: int,
        file_path: str,
        line: int,
        side: str,
        body: str,
        start_line: int | None = None,
        start_side: str | None = None,
        *,
        old_path: str | None = None,
        new_path: str | None = None,
        head_sha: str | None = None,
        base_sha: str | None = None,
        start_sha: str | None = None,
        old_line: int | None = None,
        new_line: int | None = None,
        start_old_line: int | None = None,
        start_new_line: int | None = None,
    ) -> ForgeMutationResult:
        owner, repo = _split_repo_path(repo_path)
        commit_id = head_sha
        if commit_id is None:
            pr_data = await request(
                self._http, "GET", f"/repos/{owner}/{repo}/pulls/{number}"
            )
            commit_id = pr_data.get("head", {}).get("sha", "")
        payload: dict = {
            "body": body,
            "commit_id": commit_id,
            "path": new_path or file_path,
            "line": line,
            "side": side,
        }
        if start_line is not None:
            payload["start_line"] = start_line
            payload["start_side"] = start_side or side
        data = await request(
            self._http,
            "POST",
            f"/repos/{owner}/{repo}/pulls/{number}/comments",
            json=payload,
        )
        comment = self._parse_review_comment(data)
        return ForgeMutationResult(comment.id, comment_id=comment.id)

    async def reply_to_discussion(
        self,
        repo_path: str,
        number: int,
        discussion_id: str,
        body: str,
        *,
        root_comment_id: str | None = None,
    ) -> ForgeMutationResult:
        owner, repo = _split_repo_path(repo_path)
        data = await request(
            self._http,
            "POST",
            f"/repos/{owner}/{repo}/pulls/{number}/comments/"
            f"{root_comment_id or discussion_id}/replies",
            json={"body": body},
        )
        comment = self._parse_review_comment(data)
        return ForgeMutationResult(
            comment.id, comment_id=comment.id, discussion_id=discussion_id
        )

    async def resolve_discussion(
        self,
        repo_path: str,
        number: int,
        discussion_id: str,
        resolved: bool,
    ) -> ForgeMutationResult:
        """Resolve/unresolve a review thread via GraphQL."""
        owner, repo = _split_repo_path(repo_path)
        thread_node_id = await self._find_thread_node_id(
            owner, repo, number, int(discussion_id)
        )
        if not thread_node_id:
            raise ForgeError("Could not find review thread for this comment")
        if resolved:
            await self._graphql(
                "mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { reviewThread { isResolved } } }",
                {"id": thread_node_id},
            )
        else:
            await self._graphql(
                "mutation($id: ID!) { unresolveReviewThread(input: {threadId: $id}) { reviewThread { isResolved } } }",
                {"id": thread_node_id},
            )
        return ForgeMutationResult(thread_node_id, discussion_id=discussion_id)

    async def _find_thread_node_id(
        self, owner: str, repo: str, number: int, comment_id: int
    ) -> str | None:
        """Find the GraphQL node ID of the review thread containing a comment."""
        cursor = None
        while True:
            data = await self._graphql(
                _REVIEW_THREADS_QUERY,
                {"owner": owner, "repo": repo, "number": number, "cursor": cursor},
            )
            connection = (
                data.get("repository", {})
                .get("pullRequest", {})
                .get("reviewThreads", {})
            )
            for thread in connection.get("nodes", []):
                comments = thread.get("comments", {})
                if any(
                    c.get("databaseId") == comment_id for c in comments.get("nodes", [])
                ):
                    return thread["id"]
                found = await self._find_comment_in_thread_pages(
                    thread["id"], comments.get("pageInfo", {}), comment_id
                )
                if found:
                    return thread["id"]
            page = connection.get("pageInfo", {})
            if not page.get("hasNextPage"):
                break
            cursor = page.get("endCursor")
        return None

    async def _find_comment_in_thread_pages(
        self, thread_id: str, page: dict, comment_id: int
    ) -> bool:
        query = """
        query($id: ID!, $cursor: String) {
            node(id: $id) {
                ... on PullRequestReviewThread {
                    comments(first: 100, after: $cursor) {
                        nodes { databaseId }
                        pageInfo { hasNextPage endCursor }
                    }
                }
            }
        }
        """
        while page.get("hasNextPage"):
            data = await self._graphql(
                query, {"id": thread_id, "cursor": page.get("endCursor")}
            )
            comments = data.get("node", {}).get("comments", {})
            if any(
                c.get("databaseId") == comment_id for c in comments.get("nodes", [])
            ):
                return True
            page = comments.get("pageInfo", {})
        return False

    @property
    def supports_thread_resolution(self) -> bool:
        return True

    async def submit_review(
        self,
        repo_path: str,
        number: int,
        verdict: ReviewDecision,
        body: str,
        inline_comments: list[dict] | None = None,
        *,
        head_sha: str | None = None,
    ) -> ForgeMutationResult:
        owner, repo = _split_repo_path(repo_path)
        event = {
            ReviewDecision.APPROVED: "APPROVE",
            ReviewDecision.CHANGES_REQUESTED: "REQUEST_CHANGES",
            ReviewDecision.COMMENTED: "COMMENT",
        }.get(verdict, "COMMENT")
        payload: dict = {"event": event, "body": body or ""}
        if inline_comments:
            payload["comments"] = inline_comments
        if head_sha is not None:
            payload["commit_id"] = head_sha
        data = await request(
            self._http,
            "POST",
            f"/repos/{owner}/{repo}/pulls/{number}/reviews",
            json=payload,
        )
        return ForgeMutationResult(str(data["id"]))

    async def approve_mr(
        self, repo_path: str, number: int, *, head_sha: str | None = None
    ) -> ForgeMutationResult:
        try:
            return await self.submit_review(
                repo_path, number, ReviewDecision.APPROVED, "", head_sha=head_sha
            )
        except ValidationError as exc:
            message = str(exc).lower()
            if "self-approv" in message or "approve your own pull request" in message:
                raise ValidationError(
                    "GitHub does not allow self-approving PRs"
                ) from exc
            raise

    async def merge_mr(
        self,
        repo_path: str,
        number: int,
        squash: bool = False,
        delete_branch: bool = True,
        *,
        head_sha: str | None = None,
        expected_source_repository: str | None = None,
        expected_source_branch: str | None = None,
        expected_target_branch: str | None = None,
    ) -> ForgeMergeResult:
        owner, repo = _split_repo_path(repo_path)
        merge_method = "squash" if squash else "merge"
        cleanup = SourceCleanupStatus.NOT_REQUESTED
        cleanup_branch: str | None = None
        cleanup_head: str | None = None
        if delete_branch:
            pr_data = await request(
                self._http,
                "GET",
                f"/repos/{owner}/{repo}/pulls/{number}",
            )
            cleanup_branch, cleanup_head, cleanup = _github_cleanup_target(
                pr_data,
                repo_path=repo_path,
                head_sha=head_sha,
                expected_source_repository=expected_source_repository,
                expected_source_branch=expected_source_branch,
                expected_target_branch=expected_target_branch,
            )
        data = await request(
            self._http,
            "PUT",
            f"/repos/{owner}/{repo}/pulls/{number}/merge",
            json={
                "merge_method": merge_method,
                **({"sha": head_sha} if head_sha is not None else {}),
            },
        )
        if not isinstance(data, dict):
            raise TypeError("invalid GitHub merge response")
        if data.get("merged") is not True:
            if data.get("merged") is False and isinstance(data.get("message"), str):
                raise ConflictError("GitHub rejected the pull request merge")
            raise ValueError("invalid GitHub merge response")
        merge_sha = data.get("sha")
        if not isinstance(merge_sha, str) or not merge_sha:
            raise ValueError("invalid GitHub merge response")
        if cleanup_branch is not None and cleanup_head is not None:
            try:
                ref_data = await request(
                    self._http,
                    "GET",
                    f"/repos/{owner}/{repo}/git/ref/heads/"
                    f"{urlquote(cleanup_branch, safe='/')}",
                )
                if _github_ref_sha(ref_data) != cleanup_head:
                    cleanup = SourceCleanupStatus.REJECTED
                else:
                    await request(
                        self._http,
                        "DELETE",
                        f"/repos/{owner}/{repo}/git/refs/heads/"
                        f"{urlquote(cleanup_branch, safe='/')}",
                    )
                    cleanup = SourceCleanupStatus.CONFIRMED
            except (
                asyncio.CancelledError,
                ForgeError,
                ValueError,
                TypeError,
            ):
                cleanup = SourceCleanupStatus.UNKNOWN
        return ForgeMergeResult(merge_sha, merge_sha, cleanup)

    async def close_mr(self, repo_path: str, number: int) -> ForgeMutationResult:
        owner, repo = _split_repo_path(repo_path)
        data = await request(
            self._http,
            "PATCH",
            f"/repos/{owner}/{repo}/pulls/{number}",
            json={"state": "closed"},
        )
        return _github_state_result(data, number, "closed")

    async def reopen_mr(self, repo_path: str, number: int) -> ForgeMutationResult:
        owner, repo = _split_repo_path(repo_path)
        data = await request(
            self._http,
            "PATCH",
            f"/repos/{owner}/{repo}/pulls/{number}",
            json={"state": "open"},
        )
        return _github_state_result(data, number, "open")

    async def add_comment(
        self, repo_path: str, number: int, body: str
    ) -> ForgeMutationResult:
        owner, repo = _split_repo_path(repo_path)
        data = await request(
            self._http,
            "POST",
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            json={"body": body},
        )
        return ForgeMutationResult(str(data["id"]), comment_id=str(data["id"]))

    async def list_pipelines(
        self,
        repo_path: str,
        per_page: int = 20,
    ) -> list[Pipeline]:
        owner, repo = _split_repo_path(repo_path)
        data = await request(
            self._http,
            "GET",
            f"/repos/{owner}/{repo}/actions/runs",
            params={"per_page": per_page},
        )
        return [self._parse_workflow_run(r) for r in data.get("workflow_runs", [])]

    async def get_pipeline_jobs(
        self, repo_path: str, pipeline_id: int
    ) -> list[PipelineJob]:
        owner, repo = _split_repo_path(repo_path)
        path = f"/repos/{owner}/{repo}/actions/runs/{pipeline_id}/jobs"
        jobs: list[dict] = []
        total_count: int | None = None
        for page in range(1, 101):
            data = await request(
                self._http,
                "GET",
                path,
                params={"per_page": 100, "page": page},
            )
            if not isinstance(data, dict):
                raise ForgeError("GitHub returned an invalid pipeline jobs response")
            page_total = data.get("total_count")
            page_jobs = data.get("jobs")
            if (
                type(page_total) is not int
                or page_total < 0
                or page_total > 10_000
                or not isinstance(page_jobs, list)
                or len(page_jobs) > 100
                or (total_count is not None and page_total != total_count)
            ):
                raise ForgeError("GitHub returned invalid pipeline jobs metadata")
            total_count = page_total
            jobs.extend(page_jobs)
            if len(jobs) == total_count:
                return [self._parse_job(job) for job in jobs]
            if len(jobs) > total_count or not page_jobs:
                raise ForgeError("GitHub returned incomplete pipeline jobs metadata")
        raise ForgeError("GitHub pipeline job pagination exceeded its limit")

    async def get_job_log(self, repo_path: str, job_id: int) -> str:
        owner, repo = _split_repo_path(repo_path)
        try:
            response = await self._http.get(
                f"/repos/{owner}/{repo}/actions/jobs/{job_id}/logs",
            )
        except httpx.TimeoutException as e:
            raise NetworkError(
                f"Request timed out: {redact_credentials(str(e))}"
            ) from e
        except httpx.TransportError as e:
            raise NetworkError(f"Transport error: {redact_credentials(str(e))}") from e
        if response.status_code >= 400:
            raise map_http_error(response)
        return response.text

    async def get_job_log_tail(
        self, repo_path: str, job_id: int, max_bytes: int
    ) -> JobLogTail:
        """Stream a job log and keep its last ``max_bytes`` bytes."""
        owner, repo = _split_repo_path(repo_path)
        return await request_log_tail(
            self._http,
            f"/repos/{owner}/{repo}/actions/jobs/{job_id}/logs",
            max_bytes=max_bytes,
        )

    async def retry_job(self, repo_path: str, job_id: int) -> None:
        owner, repo = _split_repo_path(repo_path)
        await request(
            self._http,
            "POST",
            f"/repos/{owner}/{repo}/actions/jobs/{job_id}/rerun",
        )

    async def cancel_pipeline(self, repo_path: str, pipeline_id: int) -> None:
        owner, repo = _split_repo_path(repo_path)
        await request(
            self._http,
            "POST",
            f"/repos/{owner}/{repo}/actions/runs/{pipeline_id}/cancel",
        )

    async def list_mr_pipelines(
        self, repo_path: str, number: int, per_page: int = 20
    ) -> list[Pipeline]:
        owner, repo = _split_repo_path(repo_path)
        pr_data = await request(
            self._http, "GET", f"/repos/{owner}/{repo}/pulls/{number}"
        )
        head = pr_data.get("head", {})
        branch = head.get("ref", "")
        head_sha = head.get("sha", "")
        if not branch or not head_sha:
            return []
        data = await request(
            self._http,
            "GET",
            f"/repos/{owner}/{repo}/actions/runs",
            params={"branch": branch, "head_sha": head_sha, "per_page": per_page},
        )
        # Some GitHub Enterprise Server versions ignore head_sha, so runs for
        # older commits on the branch, and base-SHA pull_request_target runs,
        # are dropped here as well.
        return [
            self._parse_workflow_run(r)
            for r in data.get("workflow_runs", [])
            if r.get("head_sha") == head_sha
        ]

    async def retry_pipeline(self, repo_path: str, pipeline_id: int) -> None:
        owner, repo = _split_repo_path(repo_path)
        await request(
            self._http,
            "POST",
            f"/repos/{owner}/{repo}/actions/runs/{pipeline_id}/rerun-failed-jobs",
        )

    @property
    def supports_batched_review(self) -> bool:
        return True

    @property
    def supports_lazy_ci(self) -> bool:
        return True

    @property
    def batched_ci_lookups(self) -> int:
        """How many per-pull-request CI lookups share one request right now.

        While GraphQL batching is on, lookups in flight together become
        queries of up to ``_CI_BATCH_SIZE`` pull requests, and enough may be in
        flight for ``_CI_BATCHES_IN_FLIGHT`` queries at once; otherwise each
        lookup is its own REST read and this is 1.
        """
        return _CI_BATCH_SIZE * _CI_BATCHES_IN_FLIGHT if self._ci_batching() else 1

    async def close(self) -> None:
        self._cancel_ci_batches()
        await self._http.aclose()

    def _ci_batching(self) -> bool:
        """Return whether CI reads go out in GraphQL batches right now."""
        return self._ci_graphql and self._clock() >= self._ci_paused_until

    async def _batched_ci_status(
        self, owner: str, repo: str, number: int, head_sha: str
    ) -> CIStatus | None:
        """Read one pull request's CI in the next GraphQL batch.

        Reads that arrive within ``_CI_BATCH_WINDOW_SECONDS`` of each other
        (and within ``_CI_BATCH_MAX_WAIT_SECONDS`` of the first), from any
        repository, go out as one query of up to ``_CI_BATCH_SIZE`` pull
        requests, which takes one slot of the per-host request limit.
        Returns None when this read must use REST instead: GraphQL is
        unavailable on this host or paused after repeated refusals, the
        batch was refused, the batch could not answer for this pull
        request, its head moved, or it has more checks than one query reads.
        A rate limit, a sign-in failure or a network failure raises.
        """
        if not self._ci_batching() or type(number) is not int or number < 1:
            return None
        loop = asyncio.get_running_loop()
        future: asyncio.Future[CIStatus | None] = loop.create_future()
        self._ci_reads.append(_CIRead(owner, repo, number, head_sha, future))
        if len(self._ci_reads) >= _CI_BATCH_SIZE:
            self._send_ci_batch()
        else:
            now = loop.time()
            if self._ci_timer is None:
                self._ci_batch_deadline = now + _CI_BATCH_MAX_WAIT_SECONDS
            else:
                self._ci_timer.cancel()
            self._ci_timer = loop.call_at(
                min(now + _CI_BATCH_WINDOW_SECONDS, self._ci_batch_deadline),
                self._send_ci_batch,
            )
        # A cancelled caller cancels its future, and the batch skips it.
        return await future

    def _send_ci_batch(self) -> None:
        """Send the waiting CI reads, up to one full batch per query."""
        if self._ci_timer is not None:
            self._ci_timer.cancel()
            self._ci_timer = None
        reads = [read for read in self._ci_reads if not read.future.done()]
        self._ci_reads = []
        for start in range(0, len(reads), _CI_BATCH_SIZE):
            batch = reads[start : start + _CI_BATCH_SIZE]
            task = asyncio.get_running_loop().create_task(self._run_ci_batch(batch))
            self._ci_batches.add(task)
            task.add_done_callback(self._ci_batches.discard)

    def _cancel_ci_batches(self) -> None:
        if self._ci_timer is not None:
            self._ci_timer.cancel()
            self._ci_timer = None
        for read in self._ci_reads:
            read.future.cancel()
        self._ci_reads = []
        for task in tuple(self._ci_batches):
            task.cancel()

    async def _run_ci_batch(self, batch: list[_CIRead]) -> None:
        """Answer every read of one batch, falling back to REST where needed."""
        try:
            results = await self._query_ci_batch(batch)
        except asyncio.CancelledError:
            for read in batch:
                read.future.cancel()
            raise
        except NotFoundError:
            # The host has no GraphQL API (an older GitHub Enterprise), so
            # this client reads CI over REST from then on.
            self._ci_graphql = False
            results = [None] * len(batch)
        except ForgePermissionError:
            # The token may not use GraphQL, or the host refused this query.
            # This batch reads REST; repeated refusals pause batching for a
            # while rather than for the client's lifetime.
            self._ci_forbidden += 1
            if self._ci_forbidden >= _CI_GRAPHQL_FORBIDDEN_LIMIT:
                self._ci_forbidden = 0
                self._ci_paused_until = self._clock() + _CI_GRAPHQL_PAUSE_SECONDS
            results = [None] * len(batch)
        except (RateLimitError, AuthError, NetworkError) as error:
            # A rate limit, a sign-in failure or an unreachable host would
            # fail the REST reads too, so it is reported instead of retried.
            for read in batch:
                if not read.future.done():
                    read.future.set_exception(error)
            return
        except (ForgeError, ValueError, KeyError, TypeError, AttributeError):
            # Any other failed or unreadable query falls back to REST.
            results = [None] * len(batch)
        except Exception as error:  # noqa: BLE001 - Never leave a reader waiting.
            for read in batch:
                if not read.future.done():
                    read.future.set_exception(error)
            return
        else:
            # The host answered, so earlier refusals were not repeated ones.
            self._ci_forbidden = 0
        for read, result in zip(batch, results):
            if not read.future.done():
                read.future.set_result(result)

    async def _query_ci_batch(self, batch: list[_CIRead]) -> list[CIStatus | None]:
        """Read the CI of a batch of pull requests in one GraphQL query.

        Pull requests are grouped by repository; each repository and pull
        request gets an alias, and the repository names travel as variables.
        A pull request the query errored on, or answered without a usable
        head commit, gets None.
        """
        repositories: dict[tuple[str, str], int] = {}
        for read in batch:
            repositories.setdefault((read.owner, read.repo), len(repositories))
        numbers: dict[int, list[int]] = {}
        for read in batch:
            index = repositories[(read.owner, read.repo)]
            wanted = numbers.setdefault(index, [])
            if read.number not in wanted:
                wanted.append(read.number)
        declarations: list[str] = []
        selections: list[str] = []
        variables: dict[str, str] = {}
        for (owner, repo), index in repositories.items():
            declarations.append(f"$o{index}: String!, $n{index}: String!")
            variables[f"o{index}"] = owner
            variables[f"n{index}"] = repo
            pulls = " ".join(
                f"p{number}: pullRequest(number: {number}) {{ ...ci }}"
                for number in numbers[index]
            )
            selections.append(
                f"r{index}: repository(owner: $o{index}, name: $n{index}) {{ {pulls} }}"
            )
        query = (
            f"query({', '.join(declarations)}) {{ {' '.join(selections)} }}"
            + _CI_ROLLUP_FRAGMENT
        )
        document = await self._graphql_response(query, variables)
        failed = _graphql_error_paths(document.get("errors"))
        data = document.get("data")
        if failed is None or not isinstance(data, dict):
            return [None] * len(batch)
        results: list[CIStatus | None] = []
        for read in batch:
            index = repositories[(read.owner, read.repo)]
            repo_alias = f"r{index}"
            pull_alias = f"p{read.number}"
            if any(
                path[0] == repo_alias and (len(path) == 1 or path[1] == pull_alias)
                for path in failed
            ):
                results.append(None)
                continue
            repository = data.get(repo_alias)
            pull = repository.get(pull_alias) if isinstance(repository, dict) else None
            results.append(_rollup_ci_status(pull, read.head_sha))
        return results

    def _parse_pr_summary(
        self, data: dict, repo_path: str, ci_status: CIStatus = CIStatus.UNKNOWN
    ) -> MRSummary:
        head = data.get("head", {})
        base = data.get("base", {})
        user = data.get("user", {})
        labels = [label.get("name", "") for label in data.get("labels", [])]

        return MRSummary(
            forge_host=self._host,
            repo_path=repo_path,
            local_path="",
            number=data.get("number", 0),
            title=data.get("title", ""),
            author=_parse_user(user),
            state=_parse_pr_state(data),
            is_draft=data.get("draft", False),
            source_branch=head.get("ref", ""),
            target_branch=base.get("ref", ""),
            ci_status=ci_status,
            created_at=_parse_datetime(data.get("created_at")) or datetime.now(UTC),
            updated_at=_parse_datetime(data.get("updated_at")) or datetime.now(UTC),
            web_url=data.get("html_url", ""),
            comment_count=data.get("comments", 0) + data.get("review_comments", 0),
            has_conflicts=data.get("mergeable_state") == "dirty",
            labels=tuple(labels),
            additions=data.get("additions"),
            deletions=data.get("deletions"),
            head_commit_sha=_head_sha(head),
        )

    async def _fetch_pr_ci_status(self, owner: str, repo: str, pr: dict) -> CIStatus:
        """Read a listed pull request's CI, batched, reporting failure as UNKNOWN."""
        sha = _head_sha(pr.get("head"))
        try:
            status = await self._batched_ci_status(owner, repo, pr.get("number"), sha)
        except (ForgeError, ValueError, KeyError, TypeError, AttributeError):
            return CIStatus.UNKNOWN
        if status is not None:
            return status
        return await self._fetch_ci_status(owner, repo, sha)

    async def _fetch_ci_status(self, owner: str, repo: str, sha: str) -> CIStatus:
        """Fetch combined CI status, reporting any failure to read as UNKNOWN."""
        try:
            return await self._read_ci_status(owner, repo, sha)
        except (ForgeError, ValueError, KeyError, TypeError, AttributeError):
            return CIStatus.UNKNOWN

    async def _read_ci_status(self, owner: str, repo: str, sha: str) -> CIStatus:
        """Read a commit's CI from its check runs and its commit statuses.

        CI reports either through check runs (GitHub Actions and other apps)
        or through legacy commit statuses (Jenkins, Prow and similar), so both
        are read, at the same time, and combined by ``_aggregate_ci_states``:
        any failure wins, then running and pending work, then success, and a
        commit with neither stays UNKNOWN. A failure is reported even when the
        other source could not be read or was cut short; otherwise a source
        cut short makes the result UNKNOWN. A source the token may not read
        (403 or 404) counts as empty when the other one answers. Any other
        failed request raises ForgeError, so a caller never caches it.
        """
        path = f"/repos/{owner}/{repo}/commits/{urlquote(sha, safe='')}"
        results = await asyncio.gather(
            self._read_ci_source(f"{path}/check-runs", "check_runs", _check_run_state),
            self._read_ci_source(f"{path}/status", "statuses", _commit_status_state),
            return_exceptions=True,
        )
        states: list[CIStatus] = []
        complete = True
        errors: list[ForgeError] = []
        for result in results:
            if isinstance(result, ForgeError):
                errors.append(result)
            elif isinstance(result, BaseException):
                raise result
            elif result is None:
                complete = False
            else:
                source_states, source_complete = result
                states.extend(source_states)
                complete = complete and source_complete
        if CIStatus.FAILED in states:
            return CIStatus.FAILED
        if errors and (
            len(errors) == len(results)
            or not all(isinstance(error, _UNREADABLE_CI_SOURCE) for error in errors)
        ):
            raise errors[0]
        if not complete:
            # Some runs or statuses were never read, so only a failure is
            # certain.
            return CIStatus.UNKNOWN
        return _aggregate_ci_states(states)

    async def _read_ci_source(
        self,
        path: str,
        items_key: str,
        classify: Callable[[dict], CIStatus],
    ) -> tuple[list[CIStatus], bool] | None:
        """Read one CI source of a commit, paged up to its ``total_count``.

        Returns the state of each item read and whether every item was read,
        or None for an unreadable response. A failed request raises.
        """
        try:
            items: list[dict] = []
            total_count: int | None = None
            for page in range(1, _CHECK_RUNS_MAX_PAGES + 1):
                data = await request(
                    self._http,
                    "GET",
                    path,
                    params={"per_page": _CHECK_RUNS_PAGE_SIZE, "page": page},
                )
                if not isinstance(data, dict):
                    return None
                page_items = data.get(items_key, [])
                if not isinstance(page_items, list):
                    return None
                items.extend(page_items)
                if total_count is None:
                    reported = data.get("total_count", len(page_items))
                    if type(reported) is not int or reported < 0:
                        return None
                    total_count = reported
                if len(items) >= total_count or len(page_items) < _CHECK_RUNS_PAGE_SIZE:
                    break
            states = [classify(item) for item in items]
            return states, total_count is None or len(items) >= total_count
        except (ValueError, KeyError, TypeError, AttributeError):
            return None

    async def _fetch_approvals(
        self, owner: str, repo: str, number: int
    ) -> tuple[User, ...]:
        """Fetch users who approved the PR from reviews API."""
        try:
            reviews = await paginate(
                self._http,
                f"/repos/{owner}/{repo}/pulls/{number}/reviews",
            )
            approvers = {}
            for r in reviews:
                user = _parse_user(r.get("user"))
                state = r.get("state", "")
                if state == "APPROVED":
                    approvers[user.username] = user
                elif state in ("CHANGES_REQUESTED", "DISMISSED"):
                    approvers.pop(user.username, None)
            return tuple(approvers.values())
        except (ForgeError, ValueError, KeyError, TypeError, AttributeError):
            return ()

    def _parse_pr_detail(self, data: dict, repo_path: str) -> MRDetail:
        summary = self._parse_pr_summary(data, repo_path)
        requested_reviewers = [
            _parse_user(r) for r in data.get("requested_reviewers", [])
        ]
        assignees = [_parse_user(a) for a in data.get("assignees", [])]
        return MRDetail(
            **{f.name: getattr(summary, f.name) for f in fields(summary)},
            description=data.get("body", "") or "",
            merge_status=data.get("mergeable_state") or "",
            reviewers=tuple(requested_reviewers),
            assignees=tuple(assignees),
            changes_count=data.get("changed_files", 0),
            head_sha=data.get("head", {}).get("sha", ""),
            base_sha=data.get("base", {}).get("sha", ""),
            changes_overflow=_files_overflowed(data.get("changed_files")),
        )

    def _parse_review_comment(self, data: dict) -> InlineComment:
        """Parse a review comment, anchoring it to the side it was left on.

        A LEFT comment anchors to the old file only, and a RIGHT one to the new
        file only.  ``line`` is null on an outdated comment, which then falls
        back to ``original_line``.
        """
        line = data.get("line")
        original_line = data.get("original_line")
        is_left = data.get("side") == "LEFT"
        if is_left:
            anchor = original_line if original_line is not None else line
        else:
            anchor = line if line is not None else original_line
        return InlineComment(
            id=str(data.get("id", "")),
            author=_parse_user(data.get("user")),
            body=data.get("body", ""),
            created_at=_parse_datetime(data.get("created_at")) or datetime.now(UTC),
            file_path=data.get("path", ""),
            old_line=anchor if is_left else None,
            new_line=None if is_left else anchor,
        )

    def _parse_workflow_run(self, data: dict) -> Pipeline:
        return Pipeline(
            id=data["id"],
            status=_parse_ci_status(data.get("status"), data.get("conclusion")),
            ref=data.get("head_branch") or "",
            sha=data.get("head_sha") or "",
            web_url=data.get("html_url") or "",
            source=data.get("event") or "",
            created_at=_parse_datetime(data.get("created_at")),
            finished_at=_parse_datetime(data.get("updated_at")),
            duration_seconds=data.get("run_duration_ms", 0) // 1000
            if data.get("run_duration_ms")
            else None,
        )

    def _parse_job(self, data: dict) -> PipelineJob:
        return PipelineJob(
            id=data["id"],
            name=data.get("name", ""),
            stage=data.get("workflow_name") or "",
            status=_parse_ci_status(data.get("status"), data.get("conclusion")),
            web_url=data.get("html_url") or "",
            started_at=_parse_datetime(data.get("started_at")),
            finished_at=_parse_datetime(data.get("completed_at")),
        )

    def _extract_repo_path(self, pr_data: dict) -> str:
        base = pr_data.get("base", {})
        repo = base.get("repo", {})
        return repo.get("full_name", "")
