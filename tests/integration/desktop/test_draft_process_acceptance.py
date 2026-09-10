"""Shared draft and interrupted-submission acceptance across TUI and desktop."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from contextlib import suppress
from importlib.metadata import version
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

import tongs
from tests.integration.desktop.draft_acceptance_sidecar import (
    CHANGED_REVISION,
    HOST,
    ORIGINAL_REVISION,
    PROJECT,
    REVIEW_NUMBER,
    build_session,
)
from tongs.desktop.protocol.review_operations import REVIEW_CAPABILITY
from tongs.scanner.repo import ForgeType, Remote, Repo
from tongs.services import ReviewScope
from tongs.state.drafts import (
    DiffSide,
    DraftContent,
    DraftState,
    GeneralDraftComment,
    InlineAnchor,
    InlineDraftComment,
    ReconciliationResolution,
    context_fingerprint,
)
from tongs.state.drafts.store import DraftStore
from tongs.tui_services import TUIServiceAdapter

_SIDECAR = Path(__file__).with_name("draft_acceptance_sidecar.py")
_TIMEOUT = 10.0
_TONGS_PACKAGE_ROOT = Path(tongs.__file__).resolve().parent
_TONGS_IMPORT_ROOT = _TONGS_PACKAGE_ROOT.parent


def _frame(request_id: str, method: str, params: dict[str, object]) -> bytes:
    return (
        json.dumps(
            {
                "v": 1,
                "type": "request",
                "id": request_id,
                "method": method,
                "params": params,
            },
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _revision_wire(revision=CHANGED_REVISION) -> dict[str, object]:
    return {
        "head_sha": revision.head_sha,
        "base_sha": revision.base_sha,
        "start_sha": revision.start_sha,
    }


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def _eventually(read, predicate, *, timeout: float = _TIMEOUT):
    async with asyncio.timeout(timeout):
        while True:
            value = read()
            if predicate(value):
                return value
            await asyncio.sleep(0.01)


class _DesktopProcess:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self._request = 0

    @classmethod
    async def start(
        cls, evidence_root: Path, *, revision=CHANGED_REVISION, forge_mode: str
    ) -> _DesktopProcess:
        environment = os.environ.copy()
        environment.update(
            TONGS_DRAFT_ACCEPTANCE_ROOT=str(evidence_root),
            TONGS_DRAFT_ACCEPTANCE_HEAD=revision.head_sha,
            TONGS_DRAFT_ACCEPTANCE_FORGE_MODE=forge_mode,
            TONGS_DRAFT_ACCEPTANCE_IMPORT_ROOT=str(_TONGS_IMPORT_ROOT),
            TONGS_DRAFT_ACCEPTANCE_PACKAGE_ROOT=str(_TONGS_PACKAGE_ROOT),
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            str(_SIDECAR),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd="/tmp",
            env=environment,
        )
        client = cls(process)
        try:
            handshake = await client.request(
                "handshake",
                {
                    "protocol_major": 1,
                    "core_version": version("tongs"),
                    "capabilities": [REVIEW_CAPABILITY],
                },
            )
            assert handshake["result"]["accepted_capabilities"] == [REVIEW_CAPABILITY]
            events = _read_jsonl(evidence_root / "process-events.jsonl")
            assert events[-1]["event"] == "session_started"
            assert events[-1]["source_root"] == str(_TONGS_IMPORT_ROOT)
            assert events[-1]["forge_mode"] == forge_mode
        except BaseException:
            await client.kill()
            raise
        return client

    def send(self, method: str, params: dict[str, object]) -> str:
        self._request += 1
        request_id = f"acceptance-{self._request}"
        stdin = self.process.stdin
        assert stdin is not None
        stdin.write(_frame(request_id, method, params))
        return request_id

    async def request(
        self, method: str, params: dict[str, object]
    ) -> dict[str, object]:
        request_id = self.send(method, params)
        stdin = self.process.stdin
        assert stdin is not None
        await stdin.drain()
        return await self.response(request_id)

    async def response(self, request_id: str) -> dict[str, object]:
        stdout = self.process.stdout
        assert stdout is not None
        async with asyncio.timeout(_TIMEOUT):
            while True:
                raw = await stdout.readline()
                if not raw:
                    stderr = self.process.stderr
                    diagnostic = await stderr.read() if stderr is not None else b""
                    raise AssertionError(
                        f"sidecar exited before {request_id}: {diagnostic.decode()}"
                    )
                frame = json.loads(raw)
                if frame.get("type") == "response" and frame.get("id") == request_id:
                    return cast(dict[str, object], frame)

    async def open_review(self) -> str:
        repository = await self.request(
            "repositories.open",
            {"hostname": HOST.hostname, "project_path": PROJECT},
        )
        repository_handle = repository["result"]["handle"]
        reviews = await self.request(
            "reviews.list",
            {
                "scope": "all_open",
                "repository": repository_handle,
            },
        )
        items = reviews["result"]["items"]
        assert len(items) == 1
        return cast(str, items[0]["handle"])

    async def close(self) -> None:
        if self.process.returncode is not None:
            return
        try:
            response = await self.request("shutdown", {})
            assert response["result"] == {"accepted": True}
            stdin = self.process.stdin
            assert stdin is not None
            stdin.close()
            await stdin.wait_closed()
            assert await asyncio.wait_for(self.process.wait(), _TIMEOUT) == 0
            stderr = self.process.stderr
            assert stderr is not None
            assert await stderr.read() == b""
        finally:
            await self.kill()

    async def kill(self) -> None:
        if self.process.returncode is None:
            with suppress(ProcessLookupError):
                self.process.kill()
            await asyncio.wait_for(self.process.wait(), _TIMEOUT)


def _repo(root: Path) -> Repo:
    remote = Remote(
        "origin",
        "https://acceptance.example/acceptance/shared-drafts.git",
        HOST.hostname,
        PROJECT,
        ForgeType.GITHUB,
    )
    return Repo(root / "shared-drafts", (remote,), remote)


async def _tui_adapter(evidence_root: Path, revision):
    repo = _repo(evidence_root)
    session = build_session(
        evidence_root,
        revision,
        discoverer=lambda *_args, **_kwargs: (repo,),
    )
    try:
        await session.start()
        adapter = TUIServiceAdapter(session)
        discovered = await adapter.discover_repositories()
        assert discovered.repositories == (repo,)
        page = await adapter.list_reviews(ReviewScope.ALL_OPEN, repository=repo)
        assert not page.failures
        assert len(page.items) == 1
        summary = page.items[0].summary
        snapshot = await adapter.get_review(summary)
        assert snapshot.revision == revision
        target = adapter.draft_target(summary, revision)
        return session, adapter, target
    except BaseException:
        with suppress(BaseException):
            await asyncio.wait_for(session.close(), _TIMEOUT)
        raise


@pytest.mark.asyncio
async def test_tui_desktop_shared_draft_roundtrip_preserves_conflict_content_and_anchor(
    tmp_path: Path,
) -> None:
    evidence_root = tmp_path / "shared-boundary"
    session, adapter, target = await _tui_adapter(evidence_root, ORIGINAL_REVISION)
    anchor = InlineAnchor(
        ORIGINAL_REVISION,
        "src/old.py",
        "src/new.py",
        10,
        11,
        DiffSide.NEW,
        context_fingerprint(("before", "selected", "after")),
    )
    comment = InlineDraftComment(UUID(int=106), "TUI-created inline note", anchor)
    try:
        created = await adapter.create_draft(
            target,
            DraftContent(
                body="TUI-created summary",
                comments=(comment,),
            ),
        )
    finally:
        await asyncio.wait_for(session.close(), _TIMEOUT)

    desktop = await _DesktopProcess.start(
        evidence_root, revision=CHANGED_REVISION, forge_mode="acknowledge"
    )
    try:
        review = await desktop.open_review()
        loaded = await desktop.request(
            "drafts.get", {"review": review, "draft_id": str(created.id)}
        )
        loaded_draft = loaded["result"]
        loaded_anchor = loaded_draft["comments"][0]["anchor"]
        assert loaded_draft["body"] == "TUI-created summary"
        assert loaded_draft["version"] == 1
        assert loaded_anchor["revision"] == _revision_wire(ORIGINAL_REVISION)
        assert loaded_anchor["new_line"] == 11
        assert loaded_anchor["stale"] is True

        desktop_content = {
            "body": "desktop committed summary",
            "verdict": loaded_draft["verdict"],
            "comments": loaded_draft["comments"],
        }
        saved = await desktop.request(
            "drafts.save",
            {
                "review": review,
                "draft_id": str(created.id),
                "expected_version": 1,
                "content": desktop_content,
            },
        )
        assert saved["result"]["version"] == 2
        assert saved["result"]["comments"][0]["anchor"] == loaded_anchor

        caller_recovery = copy.deepcopy(desktop_content)
        caller_recovery["body"] = "caller text retained after stale save"
        rejected_payload = copy.deepcopy(caller_recovery)
        conflict = await desktop.request(
            "drafts.save",
            {
                "review": review,
                "draft_id": str(created.id),
                "expected_version": 1,
                "content": caller_recovery,
            },
        )
        assert conflict["error"]["code"] == "service_error"
        assert conflict["error"]["details"] == {
            "service_code": "conflict",
            "draft_id": str(created.id),
            "expected_version": "1",
            "current_version": "2",
        }
        assert caller_recovery == rejected_payload

        current = await desktop.request(
            "drafts.get", {"review": review, "draft_id": str(created.id)}
        )
        assert current["result"]["body"] == "desktop committed summary"
        assert current["result"]["version"] == 2
        retried = await desktop.request(
            "drafts.save",
            {
                "review": review,
                "draft_id": str(created.id),
                "expected_version": 2,
                "content": caller_recovery,
            },
        )
        assert retried["result"]["body"] == rejected_payload["body"]
        assert retried["result"]["version"] == 3
        assert retried["result"]["comments"][0]["anchor"] == loaded_anchor
    finally:
        await desktop.close()

    restarted, adapter, changed_target = await _tui_adapter(
        evidence_root, CHANGED_REVISION
    )
    try:
        recovered = await adapter.list_drafts(changed_target)
        assert len(recovered) == 1
        assert recovered[0].id == created.id
        assert recovered[0].version == 3
        assert recovered[0].body == "caller text retained after stale save"
        recovered_comment = cast(InlineDraftComment, recovered[0].comments[0])
        assert recovered_comment.body == "TUI-created inline note"
        assert recovered_comment.anchor.revision == ORIGINAL_REVISION
        assert recovered_comment.anchor.new_line == 11
        assert recovered_comment.anchor.stale is True
    finally:
        await asyncio.wait_for(restarted.close(), _TIMEOUT)

    assert _read_jsonl(evidence_root / "mock-forge-ledger.jsonl") == []


@pytest.mark.asyncio
async def test_interrupted_process_requires_explicit_unknown_recovery_without_replay(
    tmp_path: Path,
) -> None:
    evidence_root = tmp_path / "interrupted-boundary"
    session, adapter, target = await _tui_adapter(evidence_root, CHANGED_REVISION)
    try:
        draft = await adapter.create_draft(
            target,
            DraftContent(
                comments=(
                    GeneralDraftComment(
                        UUID(int=107), "one controlled remote acceptance"
                    ),
                )
            ),
        )
    finally:
        await asyncio.wait_for(session.close(), _TIMEOUT)

    interrupted = await _DesktopProcess.start(
        evidence_root,
        revision=CHANGED_REVISION,
        forge_mode="block_after_accept",
    )
    ledger_path = evidence_root / "mock-forge-ledger.jsonl"
    try:
        review = await interrupted.open_review()
        interrupted.send(
            "review_submissions.start",
            {
                "review": review,
                "draft_id": str(draft.id),
                "expected_version": draft.version,
            },
        )
        stdin = interrupted.process.stdin
        assert stdin is not None
        await stdin.drain()
        ledger = await _eventually(
            lambda: _read_jsonl(ledger_path), lambda entries: len(entries) == 1
        )
        assert interrupted.process.returncode is None
        assert ledger == [
            {
                "action": "add_comment",
                "body": "one controlled remote acceptance",
                "project": PROJECT,
                "remote_id": "remote-comment-106",
                "review_number": REVIEW_NUMBER,
                "sequence": 1,
                "stage": "remote_accepted_before_ack",
            }
        ]
    finally:
        await interrupted.kill()

    restarted = await _DesktopProcess.start(
        evidence_root, revision=CHANGED_REVISION, forge_mode="acknowledge"
    )
    try:
        review = await restarted.open_review()
        recovered = await restarted.request(
            "review_submissions.list", {"review": review}
        )
        attempts = recovered["result"]["attempts"]
        assert len(attempts) == 1
        unknown = attempts[0]
        assert unknown["draft_id"] == str(draft.id)
        assert unknown["state"] == "unknown"
        assert unknown["outcome"] == "unknown"
        assert unknown["receipts"] == []
        assert unknown["unknown_step_ids"] == [f"comment:{UUID(int=107).hex}"]
        assert _read_jsonl(ledger_path) == ledger

        refused = await restarted.request(
            "review_submissions.resume",
            {"review": review, "attempt_id": unknown["attempt_id"]},
        )
        assert refused["error"]["code"] == "service_error"
        assert refused["error"]["details"]["service_code"] == "conflict"
        assert _read_jsonl(ledger_path) == ledger

        confirmed = await restarted.request(
            "review_submissions.reconcile",
            {
                "review": review,
                "attempt_id": unknown["attempt_id"],
                "resolution": "mark_submitted",
            },
        )
        assert confirmed["result"]["state"] == "submitted"
        assert confirmed["result"]["outcome"] == "submitted"
        assert confirmed["result"]["receipts"] == []
        assert _read_jsonl(ledger_path) == ledger
        attempt_id = UUID(unknown["attempt_id"])
    finally:
        await restarted.close()

    store = DraftStore(evidence_root / "drafts.db")
    await store.open()
    try:
        durable = await store.get_attempt(attempt_id)
        assert durable.state is DraftState.SUBMITTED
        assert durable.receipts == ()
        assert len(durable.unknown_outcomes) == 1
        assert tuple(item.resolution for item in durable.reconciliations) == (
            ReconciliationResolution.MARK_SUBMITTED,
        )
    finally:
        with suppress(BaseException):
            await asyncio.wait_for(store.close(), _TIMEOUT)

    assert _read_jsonl(ledger_path) == ledger
