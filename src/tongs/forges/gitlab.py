"""GitLab forge client implementation."""

from __future__ import annotations

import hashlib
from dataclasses import fields
from datetime import UTC, datetime
from urllib.parse import quote as urlquote

import httpx

from tongs.errors import ForgeError, NetworkError, redact_credentials
from tongs.forges.base import ForgeClient
from tongs.forges.http import map_http_error, paginate, request
from tongs.forges.models import (
    CIStatus,
    Commit,
    Discussion,
    ForgeHost,
    ForgeMergeResult,
    ForgeMutationResult,
    InlineComment,
    MRDetail,
    MRState,
    MRSummary,
    Pipeline,
    PipelineJob,
    ReviewDecision,
    SourceCleanupStatus,
    User,
)


def _encode_project(repo_path: str) -> str:
    """URL-encode a GitLab project path (e.g. 'group/repo' -> 'group%2Frepo')."""
    return urlquote(repo_path, safe="")


def _gitlab_action_result(
    value: dict | list,
    number: int,
    *,
    expected_state: str | None = None,
) -> ForgeMutationResult:
    if (
        not isinstance(value, dict)
        or type(value.get("iid")) is not int
        or value.get("iid") != number
        or type(value.get("id")) is not int
        or value["id"] <= 0
        or (expected_state is not None and value.get("state") != expected_state)
    ):
        raise ValueError("invalid GitLab merge request action response")
    return ForgeMutationResult(str(value["id"]))


def _gitlab_line_code(path: str, old_line: int | None, new_line: int | None) -> str:
    digest = hashlib.sha1(path.encode(), usedforsecurity=False).hexdigest()
    return f"{digest}_{old_line or 0}_{new_line or 0}"


def _gitlab_range_endpoint(
    path: str,
    line_type: str,
    old_line: int | None,
    new_line: int | None,
) -> dict[str, str | int]:
    endpoint: dict[str, str | int] = {
        "line_code": _gitlab_line_code(path, old_line, new_line),
        "type": line_type,
    }
    if old_line is not None:
        endpoint["old_line"] = old_line
    if new_line is not None:
        endpoint["new_line"] = new_line
    return endpoint


def _gitlab_line_type(old_line: int | None, new_line: int | None) -> str:
    if old_line is not None:
        return "old"
    if new_line is not None:
        return "new"
    raise ValueError("a GitLab diff position requires an old or new line")


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _parse_ci_status(value: str | None) -> CIStatus:
    mapping = {
        "success": CIStatus.SUCCESS,
        "failed": CIStatus.FAILED,
        "running": CIStatus.RUNNING,
        "pending": CIStatus.PENDING,
        "canceled": CIStatus.CANCELED,
        "cancelled": CIStatus.CANCELED,
        "skipped": CIStatus.SKIPPED,
        "created": CIStatus.PENDING,
        "manual": CIStatus.PENDING,
        "waiting_for_resource": CIStatus.PENDING,
        "preparing": CIStatus.PENDING,
        "scheduled": CIStatus.PENDING,
    }
    return mapping.get(value or "", CIStatus.UNKNOWN)


def _parse_mr_state(value: str) -> MRState:
    mapping = {
        "opened": MRState.OPEN,
        "closed": MRState.CLOSED,
        "merged": MRState.MERGED,
    }
    return mapping.get(value, MRState.OPEN)


def _parse_user(data: dict | None) -> User:
    if not data:
        return User(username="unknown")
    return User(
        username=data.get("username", "unknown"),
        display_name=data.get("name", ""),
    )


class GitLabClient(ForgeClient):
    """GitLab API v4 client."""

    def __init__(self, host: ForgeHost, http_client: httpx.AsyncClient):
        self._host = host
        self._http = http_client

    async def list_mrs(
        self,
        repo_path: str,
        state: str = "opened",
        per_page: int = 100,
    ) -> list[MRSummary]:
        project = _encode_project(repo_path)
        gitlab_state = "opened" if state == "open" else state
        data = await paginate(
            self._http,
            f"/projects/{project}/merge_requests",
            per_page=per_page,
            params={"state": gitlab_state},
        )
        return await self._enrich_ci_status(data, repo_path)

    async def list_my_reviews(self) -> list[MRSummary]:
        user_data = await request(self._http, "GET", "/user")
        username = user_data.get("username", "")
        data = await paginate(
            self._http,
            "/merge_requests",
            per_page=100,
            params={"scope": "all", "reviewer_username": username, "state": "opened"},
        )
        results = []
        for mr in data:
            rp = self._extract_repo_path(mr)
            results.append(self._parse_mr_summary(mr, rp))
        return results

    async def list_my_mrs(self) -> list[MRSummary]:
        data = await paginate(
            self._http,
            "/merge_requests",
            per_page=100,
            params={"scope": "created_by_me", "state": "opened"},
        )
        results = []
        for mr in data:
            rp = self._extract_repo_path(mr)
            results.append(self._parse_mr_summary(mr, rp))
        return results

    async def _enrich_ci_status(
        self, mrs_data: list[dict], repo_path: str
    ) -> list[MRSummary]:
        """Parse MR summaries, fetching pipeline status if not in listing data."""
        import asyncio

        summaries = [self._parse_mr_summary(mr, repo_path) for mr in mrs_data]
        needs_ci = [
            (i, mr)
            for i, (s, mr) in enumerate(zip(summaries, mrs_data))
            if s.ci_status == CIStatus.UNKNOWN and mr.get("iid")
        ]
        if not needs_ci:
            return summaries

        project = _encode_project(repo_path)

        async def fetch_ci(iid: int) -> CIStatus:
            try:
                pipelines = await request(
                    self._http,
                    "GET",
                    f"/projects/{project}/merge_requests/{iid}/pipelines",
                    params={"per_page": 1},
                )
                if pipelines and isinstance(pipelines, list) and pipelines[0]:
                    return _parse_ci_status(pipelines[0].get("status"))
            except (ForgeError, ValueError, KeyError, TypeError, AttributeError):
                return CIStatus.UNKNOWN
            return CIStatus.UNKNOWN

        ci_tasks = [fetch_ci(mr.get("iid")) for _, mr in needs_ci]
        ci_results = await asyncio.gather(*ci_tasks)

        for (idx, _), ci in zip(needs_ci, ci_results):
            if ci != CIStatus.UNKNOWN:
                old = summaries[idx]
                summaries[idx] = MRSummary(
                    **{
                        f.name: (ci if f.name == "ci_status" else getattr(old, f.name))
                        for f in fields(old)
                    }
                )

        return summaries

    async def get_mr(self, repo_path: str, number: int) -> MRDetail:
        import asyncio

        project = _encode_project(repo_path)
        mr_task = asyncio.create_task(
            request(self._http, "GET", f"/projects/{project}/merge_requests/{number}")
        )
        approvals_task = asyncio.create_task(self._fetch_approvals(project, number))
        data = await mr_task
        approvals = await approvals_task
        detail = self._parse_mr_detail(data, repo_path)
        if approvals:
            detail = MRDetail(
                **{
                    f.name: (
                        approvals if f.name == "approvals" else getattr(detail, f.name)
                    )
                    for f in fields(detail)
                }
            )
        return detail

    async def _fetch_approvals(self, project: str, number: int) -> tuple[User, ...]:
        try:
            data = await request(
                self._http,
                "GET",
                f"/projects/{project}/merge_requests/{number}/approvals",
            )
            return tuple(
                _parse_user(a.get("user", a)) for a in data.get("approved_by", [])
            )
        except (ForgeError, ValueError, KeyError, TypeError, AttributeError):
            return ()

    async def get_mr_diff(self, repo_path: str, number: int) -> list[dict]:
        project = _encode_project(repo_path)
        data = await request(
            self._http,
            "GET",
            f"/projects/{project}/merge_requests/{number}/changes",
        )
        return data.get("changes", [])

    async def list_mr_commits(self, repo_path: str, number: int) -> list[Commit]:
        project = _encode_project(repo_path)
        data = await paginate(
            self._http,
            f"/projects/{project}/merge_requests/{number}/commits",
        )
        return [
            Commit(
                sha=c.get("id", ""),
                short_sha=c.get("short_id", c.get("id", "")[:8]),
                title=c.get("title", ""),
                message=c.get("message", ""),
                author=User(
                    username=c.get("author_name", "unknown"),
                    display_name=c.get("author_name", ""),
                ),
                created_at=_parse_datetime(c.get("created_at")),
                web_url=c.get("web_url", ""),
            )
            for c in data
        ]

    async def get_mr_discussions(self, repo_path: str, number: int) -> list[Discussion]:
        project = _encode_project(repo_path)
        data = await paginate(
            self._http,
            f"/projects/{project}/merge_requests/{number}/discussions",
        )
        discussions = []
        for d in data:
            notes = d.get("notes", [])
            if not notes:
                continue
            root = notes[0]
            if root.get("system", False):
                continue
            replies = tuple(self._parse_note(n) for n in notes[1:])
            root_comment = self._parse_note(root)
            root_comment = InlineComment(
                id=root_comment.id,
                author=root_comment.author,
                body=root_comment.body,
                created_at=root_comment.created_at,
                file_path=root_comment.file_path,
                old_line=root_comment.old_line,
                new_line=root_comment.new_line,
                is_resolved=root_comment.is_resolved,
                replies=replies,
            )
            is_inline = root.get("position") is not None
            discussions.append(
                Discussion(
                    id=d["id"],
                    is_inline=is_inline,
                    root_comment=root_comment,
                    is_resolved=d.get("resolved", False),
                    resolvable=d.get("resolvable", True),
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
        project = _encode_project(repo_path)
        revisions = (head_sha, base_sha, start_sha)
        if all(value is None for value in revisions):
            mr_data = await request(
                self._http, "GET", f"/projects/{project}/merge_requests/{number}"
            )
            diff_refs = mr_data.get("diff_refs", {})
            head_sha = diff_refs.get("head_sha", "")
            base_sha = diff_refs.get("base_sha", "")
            start_sha = diff_refs.get("start_sha", "")
        elif any(value is None for value in revisions):
            raise ValueError("all GitLab revision fields are required together")

        position = {
            "position_type": "text",
            "base_sha": base_sha,
            "start_sha": start_sha,
            "head_sha": head_sha,
            "new_path": new_path or file_path,
            "old_path": old_path or file_path,
        }
        resolved_old = old_line
        resolved_new = new_line
        if resolved_old is None and resolved_new is None:
            if side == "LEFT":
                resolved_old = line
            else:
                resolved_new = line
        if resolved_old is not None:
            position["old_line"] = resolved_old
        if resolved_new is not None:
            position["new_line"] = resolved_new
        if start_line is not None:
            range_start_old = (
                start_old_line
                if start_old_line is not None
                else start_line
                if (start_side or side) == "LEFT"
                else None
            )
            range_start_new = (
                start_new_line
                if start_new_line is not None
                else start_line
                if (start_side or side) != "LEFT"
                else None
            )
            range_end_old = resolved_old
            range_end_new = resolved_new
            start_type = _gitlab_line_type(range_start_old, range_start_new)
            end_type = _gitlab_line_type(range_end_old, range_end_new)
            range_path = (
                old_path or file_path
                if start_type == "old" and range_start_new is None
                else new_path or file_path
            )
            position["line_range"] = {
                "start": _gitlab_range_endpoint(
                    range_path, start_type, range_start_old, range_start_new
                ),
                "end": _gitlab_range_endpoint(
                    range_path, end_type, range_end_old, range_end_new
                ),
            }

        data = await request(
            self._http,
            "POST",
            f"/projects/{project}/merge_requests/{number}/discussions",
            json={"body": body, "position": position},
        )
        notes = data.get("notes", [data])
        comment = self._parse_note(notes[0])
        return ForgeMutationResult(
            str(data.get("id", comment.id)),
            comment_id=comment.id,
            discussion_id=str(data.get("id", "")) or None,
        )

    async def reply_to_discussion(
        self,
        repo_path: str,
        number: int,
        discussion_id: str,
        body: str,
        *,
        root_comment_id: str | None = None,
    ) -> ForgeMutationResult:
        project = _encode_project(repo_path)
        data = await request(
            self._http,
            "POST",
            f"/projects/{project}/merge_requests/{number}/discussions/{discussion_id}/notes",
            json={"body": body},
        )
        comment = self._parse_note(data)
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
        project = _encode_project(repo_path)
        data = await request(
            self._http,
            "PUT",
            f"/projects/{project}/merge_requests/{number}/discussions/{discussion_id}",
            json={"resolved": resolved},
        )
        return ForgeMutationResult(
            str(data.get("id", discussion_id)), discussion_id=discussion_id
        )

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
        result: ForgeMutationResult | None = None
        if inline_comments:
            for comment in inline_comments:
                result = await self.create_inline_comment(
                    repo_path,
                    number,
                    file_path=comment["path"],
                    line=comment["line"],
                    side=comment.get("side", "RIGHT"),
                    body=comment["body"],
                )
        if verdict == ReviewDecision.APPROVED:
            result = await self.approve_mr(repo_path, number, head_sha=head_sha)
        if body:
            result = await self.add_comment(repo_path, number, body)
        if result is None:
            raise ValueError("review contains no mutation")
        return result

    async def approve_mr(
        self, repo_path: str, number: int, *, head_sha: str | None = None
    ) -> ForgeMutationResult:
        project = _encode_project(repo_path)
        data = await request(
            self._http,
            "POST",
            f"/projects/{project}/merge_requests/{number}/approve",
            json={"sha": head_sha} if head_sha is not None else None,
        )
        return ForgeMutationResult(str(data.get("id", number)))

    async def unapprove_mr(self, repo_path: str, number: int) -> ForgeMutationResult:
        project = _encode_project(repo_path)
        data = await request(
            self._http,
            "POST",
            f"/projects/{project}/merge_requests/{number}/unapprove",
        )
        return _gitlab_action_result(data, number)

    @property
    def supports_unapprove(self) -> bool:
        return True

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
        if (
            expected_source_repository is not None
            and expected_source_repository != repo_path
        ):
            raise ValueError("cross-project source cleanup is unsupported")
        project = _encode_project(repo_path)
        data = await request(
            self._http,
            "PUT",
            f"/projects/{project}/merge_requests/{number}/merge",
            json={
                "squash": squash,
                "should_remove_source_branch": delete_branch,
                **({"sha": head_sha} if head_sha is not None else {}),
            },
        )
        if (
            not isinstance(data, dict)
            or type(data.get("iid")) is not int
            or data.get("iid") != number
            or data.get("state") != "merged"
            or type(data.get("id")) is not int
            or data["id"] <= 0
        ):
            raise ValueError("invalid GitLab merge response")
        if (
            expected_source_branch is not None
            and data.get("source_branch") != expected_source_branch
        ):
            raise ValueError("GitLab merge response source branch changed")
        if (
            expected_target_branch is not None
            and data.get("target_branch") != expected_target_branch
        ):
            raise ValueError("GitLab merge response target branch changed")
        merge_sha = data.get("merge_commit_sha") or data.get("squash_commit_sha")
        if not isinstance(merge_sha, str) or not merge_sha:
            raise ValueError("invalid GitLab merge response")
        cleanup = (
            SourceCleanupStatus.UNKNOWN
            if delete_branch
            else SourceCleanupStatus.NOT_REQUESTED
        )
        return ForgeMergeResult(str(data["id"]), merge_sha, cleanup)

    async def close_mr(self, repo_path: str, number: int) -> ForgeMutationResult:
        project = _encode_project(repo_path)
        data = await request(
            self._http,
            "PUT",
            f"/projects/{project}/merge_requests/{number}",
            json={"state_event": "close"},
        )
        return _gitlab_action_result(data, number, expected_state="closed")

    async def reopen_mr(self, repo_path: str, number: int) -> ForgeMutationResult:
        project = _encode_project(repo_path)
        data = await request(
            self._http,
            "PUT",
            f"/projects/{project}/merge_requests/{number}",
            json={"state_event": "reopen"},
        )
        return _gitlab_action_result(data, number, expected_state="opened")

    async def add_comment(
        self, repo_path: str, number: int, body: str
    ) -> ForgeMutationResult:
        project = _encode_project(repo_path)
        data = await request(
            self._http,
            "POST",
            f"/projects/{project}/merge_requests/{number}/notes",
            json={"body": body},
        )
        return ForgeMutationResult(str(data["id"]), comment_id=str(data["id"]))

    async def list_pipelines(
        self,
        repo_path: str,
        per_page: int = 20,
    ) -> list[Pipeline]:
        project = _encode_project(repo_path)
        data = await request(
            self._http,
            "GET",
            f"/projects/{project}/pipelines",
            params={"per_page": per_page},
        )
        return [self._parse_pipeline(p) for p in data]

    async def get_pipeline_jobs(
        self, repo_path: str, pipeline_id: int
    ) -> list[PipelineJob]:
        project = _encode_project(repo_path)
        data = await paginate(
            self._http,
            f"/projects/{project}/pipelines/{pipeline_id}/jobs",
        )
        return [self._parse_job(j) for j in data]

    async def get_job_log(self, repo_path: str, job_id: int) -> str:
        project = _encode_project(repo_path)
        try:
            response = await self._http.get(
                f"/projects/{project}/jobs/{job_id}/trace",
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
        project = _encode_project(repo_path)
        await request(
            self._http,
            "POST",
            f"/projects/{project}/jobs/{job_id}/retry",
        )

    async def cancel_pipeline(self, repo_path: str, pipeline_id: int) -> None:
        project = _encode_project(repo_path)
        await request(
            self._http,
            "POST",
            f"/projects/{project}/pipelines/{pipeline_id}/cancel",
        )

    async def list_mr_pipelines(
        self, repo_path: str, number: int, per_page: int = 20
    ) -> list[Pipeline]:
        project = _encode_project(repo_path)
        data = await request(
            self._http,
            "GET",
            f"/projects/{project}/merge_requests/{number}/pipelines",
            params={"per_page": per_page},
        )
        return [self._parse_pipeline(p) for p in data]

    async def retry_pipeline(self, repo_path: str, pipeline_id: int) -> None:
        project = _encode_project(repo_path)
        await request(
            self._http,
            "POST",
            f"/projects/{project}/pipelines/{pipeline_id}/retry",
        )

    async def cancel_job(self, repo_path: str, job_id: int) -> None:
        project = _encode_project(repo_path)
        await request(
            self._http,
            "POST",
            f"/projects/{project}/jobs/{job_id}/cancel",
        )

    @property
    def supports_job_cancel(self) -> bool:
        return True

    @property
    def supports_thread_resolution(self) -> bool:
        return True

    async def close(self) -> None:
        await self._http.aclose()

    def _parse_mr_summary(self, data: dict, repo_path: str) -> MRSummary:
        pipeline = data.get("head_pipeline") or data.get("pipeline") or {}
        return MRSummary(
            forge_host=self._host,
            repo_path=repo_path,
            local_path="",
            number=data["iid"],
            title=data.get("title", ""),
            author=_parse_user(data.get("author")),
            state=_parse_mr_state(data.get("state", "opened")),
            is_draft=data.get("draft", False),
            source_branch=data.get("source_branch", ""),
            target_branch=data.get("target_branch", ""),
            ci_status=_parse_ci_status(pipeline.get("status")),
            created_at=_parse_datetime(data.get("created_at")) or datetime.now(UTC),
            updated_at=_parse_datetime(data.get("updated_at")) or datetime.now(UTC),
            web_url=data.get("web_url", ""),
            comment_count=data.get("user_notes_count", 0),
            has_conflicts=data.get("has_conflicts", False),
            labels=tuple(data.get("labels", [])),
        )

    def _parse_mr_detail(self, data: dict, repo_path: str) -> MRDetail:
        summary = self._parse_mr_summary(data, repo_path)
        diff_refs = data.get("diff_refs") or {}
        reviewers = [_parse_user(r) for r in data.get("reviewers", [])]
        assignees = [_parse_user(a) for a in data.get("assignees", [])]
        approvals = [_parse_user(a.get("user", a)) for a in data.get("approved_by", [])]
        return MRDetail(
            **{f.name: getattr(summary, f.name) for f in fields(summary)},
            description=data.get("description", "") or "",
            merge_status=data.get("merge_status", ""),
            approvals=tuple(approvals),
            reviewers=tuple(reviewers),
            assignees=tuple(assignees),
            changes_count=int(data.get("changes_count", 0) or 0),
            head_sha=diff_refs.get("head_sha", ""),
            base_sha=diff_refs.get("base_sha", ""),
            start_sha=diff_refs.get("start_sha") or None,
            detailed_merge_status=data.get("detailed_merge_status"),
        )

    def _parse_note(self, data: dict) -> InlineComment:
        position = data.get("position") or {}
        return InlineComment(
            id=str(data.get("id", "")),
            author=_parse_user(data.get("author")),
            body=data.get("body", ""),
            created_at=_parse_datetime(data.get("created_at")) or datetime.now(UTC),
            file_path=position.get("new_path", ""),
            old_line=position.get("old_line"),
            new_line=position.get("new_line"),
            is_resolved=data.get("resolved", False),
        )

    def _parse_pipeline(self, data: dict) -> Pipeline:
        return Pipeline(
            id=data["id"],
            status=_parse_ci_status(data.get("status")),
            ref=data.get("ref", ""),
            sha=data.get("sha", ""),
            web_url=data.get("web_url", ""),
            source=data.get("source", ""),
            created_at=_parse_datetime(data.get("created_at")),
            finished_at=_parse_datetime(data.get("finished_at")),
            duration_seconds=data.get("duration"),
        )

    def _parse_job(self, data: dict) -> PipelineJob:
        return PipelineJob(
            id=data["id"],
            name=data.get("name", ""),
            stage=data.get("stage", ""),
            status=_parse_ci_status(data.get("status")),
            web_url=data.get("web_url", ""),
            started_at=_parse_datetime(data.get("started_at")),
            finished_at=_parse_datetime(data.get("finished_at")),
            duration_seconds=data.get("duration"),
            allow_failure=data.get("allow_failure", False),
        )

    def _extract_repo_path(self, mr_data: dict) -> str:
        """Extract repo path from MR data returned by global endpoints."""
        refs = mr_data.get("references", {})
        full_ref = refs.get("full", "")
        if "!" in full_ref:
            return full_ref.rsplit("!", 1)[0]
        web_url = mr_data.get("web_url", "")
        if "/-/merge_requests/" in web_url:
            path = web_url.split("/-/merge_requests/")[0]
            return path.replace(f"https://{self._host.hostname}/", "")
        return ""
