"""GitHub forge client implementation."""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime

import httpx

from tongs.errors import ForgeError, NetworkError, redact_credentials
from tongs.forges.base import ForgeClient
from tongs.forges.http import map_http_error, paginate, request
from tongs.forges.models import (
    CIStatus,
    Commit,
    Discussion,
    ForgeHost,
    ForgeMutationResult,
    InlineComment,
    MRDetail,
    MRState,
    MRSummary,
    Pipeline,
    PipelineJob,
    ReviewDecision,
    User,
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


def _parse_mr_state(value: str) -> MRState:
    return {
        "open": MRState.OPEN,
        "closed": MRState.CLOSED,
        "merged": MRState.MERGED,
    }.get(value.lower(), MRState.OPEN)


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


class GitHubClient(ForgeClient):
    """GitHub REST + GraphQL API client."""

    def __init__(self, host: ForgeHost, http_client: httpx.AsyncClient):
        self._host = host
        self._http = http_client

    @property
    def _graphql_url(self) -> str:
        if self._host.hostname == "github.com":
            return "https://api.github.com/graphql"
        return f"https://{self._host.hostname}/api/graphql"

    async def _graphql(self, query: str, variables: dict | None = None) -> dict:
        """Execute a GraphQL query against GitHub's API."""
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
        if "errors" in data:
            msg = data["errors"][0].get("message", "GraphQL error")
            raise ForgeError(redact_credentials(msg))
        return data.get("data", {})

    async def list_mrs(
        self,
        repo_path: str,
        state: str = "open",
        per_page: int = 100,
    ) -> list[MRSummary]:
        import asyncio

        owner, repo = _split_repo_path(repo_path)
        data = await paginate(
            self._http,
            f"/repos/{owner}/{repo}/pulls",
            per_page=per_page,
            params={"state": state},
        )
        ci_tasks = [
            self._fetch_ci_status(owner, repo, pr.get("head", {}).get("sha", ""))
            for pr in data
        ]
        ci_results = await asyncio.gather(*ci_tasks)
        return [
            self._parse_pr_summary(pr, repo_path, ci_status=ci)
            for pr, ci in zip(data, ci_results)
        ]

    async def list_my_reviews(self) -> list[MRSummary]:
        return await self._search_prs("is:pr is:open review-requested:@me")

    async def list_my_mrs(self) -> list[MRSummary]:
        return await self._search_prs("is:pr is:open author:@me")

    async def _search_prs(self, query: str) -> list[MRSummary]:
        data = await request(
            self._http,
            "GET",
            "/search/issues",
            params={"q": query, "per_page": 100},
        )
        results = []
        for item in data.get("items", []):
            repo_url = item.get("repository_url", "")
            number = item.get("number")
            if not repo_url or not number:
                continue
            repo_path = self._repo_path_from_api_url(repo_url)
            if not repo_path:
                continue
            try:
                owner, repo = _split_repo_path(repo_path)
                pr_data = await request(
                    self._http,
                    "GET",
                    f"/repos/{owner}/{repo}/pulls/{number}",
                )
                results.append(self._parse_pr_summary(pr_data, repo_path))
            except (ForgeError, ValueError, KeyError, TypeError, AttributeError):
                continue
        return results

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

    async def get_mr_diff(self, repo_path: str, number: int) -> list[dict]:
        owner, repo = _split_repo_path(repo_path)
        data = await paginate(
            self._http,
            f"/repos/{owner}/{repo}/pulls/{number}/files",
        )
        return data

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
                web_url=c.get("html_url", ""),
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
        discussions = []
        for root_id, comment in roots.items():
            parsed = self._parse_review_comment(comment)
            root = InlineComment(
                id=parsed.id,
                author=parsed.author,
                body=parsed.body,
                created_at=parsed.created_at,
                file_path=parsed.file_path,
                old_line=parsed.old_line,
                new_line=parsed.new_line,
                is_resolved=parsed.is_resolved,
                replies=tuple(
                    self._parse_review_comment(reply) for reply in replies[root_id]
                ),
            )
            is_inline = bool(comment.get("path"))
            discussions.append(
                Discussion(
                    id=str(root_id),
                    is_inline=is_inline,
                    root_comment=root,
                    is_resolved=False,
                    resolvable=is_inline,
                )
            )
        return discussions

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
        query = """
        query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
            repository(owner: $owner, name: $repo) {
                pullRequest(number: $number) {
                    reviewThreads(first: 100, after: $cursor) {
                        nodes {
                            id
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
        cursor = None
        while True:
            data = await self._graphql(
                query,
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
        except ForgeError as exc:
            if "422" in str(exc):
                raise ForgeError("GitHub does not allow self-approving PRs") from exc
            raise

    async def merge_mr(
        self,
        repo_path: str,
        number: int,
        squash: bool = False,
        delete_branch: bool = True,
    ) -> None:
        owner, repo = _split_repo_path(repo_path)
        merge_method = "squash" if squash else "merge"
        await request(
            self._http,
            "PUT",
            f"/repos/{owner}/{repo}/pulls/{number}/merge",
            json={"merge_method": merge_method},
        )
        if delete_branch:
            try:
                pr_data = await request(
                    self._http,
                    "GET",
                    f"/repos/{owner}/{repo}/pulls/{number}",
                )
                head = pr_data.get("head", {})
                head_repo = head.get("repo", {}).get("full_name", "")
                branch = head.get("ref", "")
                if branch and head_repo == repo_path:
                    await request(
                        self._http,
                        "DELETE",
                        f"/repos/{owner}/{repo}/git/refs/heads/{branch}",
                    )
            except (ForgeError, ValueError, KeyError, TypeError, AttributeError):
                return

    async def close_mr(self, repo_path: str, number: int) -> None:
        owner, repo = _split_repo_path(repo_path)
        await request(
            self._http,
            "PATCH",
            f"/repos/{owner}/{repo}/pulls/{number}",
            json={"state": "closed"},
        )

    async def reopen_mr(self, repo_path: str, number: int) -> None:
        owner, repo = _split_repo_path(repo_path)
        await request(
            self._http,
            "PATCH",
            f"/repos/{owner}/{repo}/pulls/{number}",
            json={"state": "open"},
        )

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
        branch = pr_data.get("head", {}).get("ref", "")
        if not branch:
            return []
        data = await request(
            self._http,
            "GET",
            f"/repos/{owner}/{repo}/actions/runs",
            params={"branch": branch, "per_page": per_page},
        )
        return [self._parse_workflow_run(r) for r in data.get("workflow_runs", [])]

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

    async def close(self) -> None:
        await self._http.aclose()

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
            state=_parse_mr_state(data.get("state", "open")),
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
        )

    async def _fetch_ci_status(self, owner: str, repo: str, sha: str) -> CIStatus:
        """Fetch combined CI status from check-runs API."""
        try:
            data = await request(
                self._http,
                "GET",
                f"/repos/{owner}/{repo}/commits/{sha}/check-runs",
                params={"per_page": 100},
            )
            runs = data.get("check_runs", [])
            if not runs:
                return CIStatus.UNKNOWN
            statuses = [
                _parse_ci_status(r.get("status"), r.get("conclusion")) for r in runs
            ]
            if any(s == CIStatus.FAILED for s in statuses):
                return CIStatus.FAILED
            if any(s == CIStatus.RUNNING for s in statuses):
                return CIStatus.RUNNING
            if any(s == CIStatus.PENDING for s in statuses):
                return CIStatus.PENDING
            if all(s == CIStatus.SUCCESS for s in statuses):
                return CIStatus.SUCCESS
            return CIStatus.UNKNOWN
        except (ForgeError, ValueError, KeyError, TypeError, AttributeError):
            return CIStatus.UNKNOWN

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
            merge_status=data.get("mergeable_state", ""),
            reviewers=tuple(requested_reviewers),
            assignees=tuple(assignees),
            changes_count=data.get("changed_files", 0),
            head_sha=data.get("head", {}).get("sha", ""),
            base_sha=data.get("base", {}).get("sha", ""),
        )

    def _parse_review_comment(self, data: dict) -> InlineComment:
        return InlineComment(
            id=str(data.get("id", "")),
            author=_parse_user(data.get("user")),
            body=data.get("body", ""),
            created_at=_parse_datetime(data.get("created_at")) or datetime.now(UTC),
            file_path=data.get("path", ""),
            old_line=data.get("original_line") if data.get("side") == "LEFT" else None,
            new_line=data.get("line"),
        )

    def _parse_workflow_run(self, data: dict) -> Pipeline:
        return Pipeline(
            id=data["id"],
            status=_parse_ci_status(data.get("status"), data.get("conclusion")),
            ref=data.get("head_branch", ""),
            sha=data.get("head_sha", ""),
            web_url=data.get("html_url", ""),
            source=data.get("event", ""),
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
            stage=data.get("workflow_name", ""),
            status=_parse_ci_status(data.get("status"), data.get("conclusion")),
            web_url=data.get("html_url", ""),
            started_at=_parse_datetime(data.get("started_at")),
            finished_at=_parse_datetime(data.get("completed_at")),
        )

    def _extract_repo_path(self, pr_data: dict) -> str:
        base = pr_data.get("base", {})
        repo = base.get("repo", {})
        return repo.get("full_name", "")
