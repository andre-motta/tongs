"""Private extraction and release watermark tests."""

from __future__ import annotations

import asyncio
import os
import stat
import tarfile
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

import tongs.desktop.installer.extract as extract_module
from tongs.desktop.installer.extract import (
    _accept_release,
    discard_staged_artifact,
    extract_verified_archive,
)
from tongs.desktop.installer.models import (
    AcceptedReleaseState,
    InstallerError,
    InstallerErrorCode,
    InstallerLimits,
    InstallRequest,
)

from .helpers import ARCHIVE_NAME, S0_FIXTURES, documents, verified_metadata


class MemoryState:
    def __init__(self, current: AcceptedReleaseState | None = None) -> None:
        self.current = current
        self.writes = 0

    async def read(self) -> AcceptedReleaseState | None:
        return self.current

    async def compare_and_swap(
        self,
        expected: AcceptedReleaseState | None,
        replacement: AcceptedReleaseState,
    ) -> bool:
        if self.current != expected:
            return False
        self.current = replacement
        self.writes += 1
        return True


def test_extracts_exact_validated_bytes_to_private_unique_directory(
    tmp_path: Path,
) -> None:
    archive = documents()[1]
    result = extract_verified_archive(archive, verified_metadata(), tmp_path / "stage")

    assert stat.S_IMODE((tmp_path / "stage").stat().st_mode) == 0o700
    assert stat.S_IMODE(result.staging_path.stat().st_mode) == 0o700
    assert result.launcher_path.is_file()
    assert os.access(result.launcher_path, os.X_OK)
    with tarfile.open(S0_FIXTURES / ARCHIVE_NAME, mode="r:gz") as opened:
        expected = opened.extractfile("runtime/resources/app.asar")
        assert expected is not None
        assert (result.staging_path / "runtime/resources/app.asar").read_bytes() == (
            expected.read()
        )

    discard_staged_artifact(result)
    assert not result.staging_path.exists()


def test_rejects_unsafe_staging_root_without_touching_it(tmp_path: Path) -> None:
    root = tmp_path / "shared"
    root.mkdir(mode=0o755)

    with pytest.raises(InstallerError) as raised:
        extract_verified_archive(documents()[1], verified_metadata(), root)

    assert raised.value.code is InstallerErrorCode.EXTRACTION_FAILED
    assert tuple(root.iterdir()) == ()


def test_destination_collision_preserves_preexisting_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "stage"
    root.mkdir(mode=0o700)
    existing = root / f".tongs-stage-{'a' * 32}"
    existing.mkdir()
    marker = existing / "owned-by-another-attempt"
    marker.write_text("preserve")
    monkeypatch.setattr(
        "tongs.desktop.installer.extract.secrets.token_hex", lambda _: "a" * 32
    )

    with pytest.raises(InstallerError) as raised:
        extract_verified_archive(documents()[1], verified_metadata(), root)

    assert raised.value.code is InstallerErrorCode.DESTINATION_COLLISION
    assert marker.read_text() == "preserve"


def test_invalid_archive_never_creates_staging_content(tmp_path: Path) -> None:
    root = tmp_path / "stage"
    archive = documents()[1]

    with pytest.raises(InstallerError):
        extract_verified_archive(archive[:-1], verified_metadata(), root)

    assert not root.exists()


@pytest.mark.asyncio
async def test_acceptance_watermark_updates_only_higher_complete_stage(
    tmp_path: Path,
) -> None:
    staged = extract_verified_archive(
        documents()[1], verified_metadata(), tmp_path / "stage"
    )
    store = MemoryState()

    await _accept_release(store, staged, InstallRequest())

    assert store.current == AcceptedReleaseState(
        "1.2.3", staged.manifest_sha256, staged.source_commit
    )
    assert store.writes == 1


@pytest.mark.asyncio
async def test_explicit_verified_downgrade_never_lowers_watermark(
    tmp_path: Path,
) -> None:
    staged = extract_verified_archive(
        documents()[1], verified_metadata(), tmp_path / "stage"
    )
    highest = AcceptedReleaseState("2.0.0", "a" * 64, "b" * 40)
    store = MemoryState(highest)

    await _accept_release(store, staged, InstallRequest("1.2.3", allow_downgrade=True))

    assert store.current is highest
    assert store.writes == 0


@pytest.mark.asyncio
async def test_implicit_downgrade_and_equal_identity_conflict_are_blocked(
    tmp_path: Path,
) -> None:
    staged = extract_verified_archive(
        documents()[1], verified_metadata(), tmp_path / "stage"
    )
    older = MemoryState(AcceptedReleaseState("2.0.0", "a" * 64, "b" * 40))
    with pytest.raises(InstallerError) as rollback:
        await _accept_release(older, staged, InstallRequest())
    assert rollback.value.code is InstallerErrorCode.ROLLBACK_BLOCKED

    conflicting = MemoryState(
        replace(
            AcceptedReleaseState(
                staged.release.version,
                staged.manifest_sha256,
                staged.source_commit,
            ),
            manifest_sha256="c" * 64,
        )
    )
    with pytest.raises(InstallerError) as conflict:
        await _accept_release(conflicting, staged, InstallRequest())
    assert conflict.value.code is InstallerErrorCode.STATE_CONFLICT


@pytest.mark.asyncio
async def test_state_cancellation_propagates(tmp_path: Path) -> None:
    class CancelledState(MemoryState):
        async def read(self) -> AcceptedReleaseState | None:
            raise asyncio.CancelledError

    staged = extract_verified_archive(
        documents()[1], verified_metadata(), tmp_path / "stage"
    )
    with pytest.raises(asyncio.CancelledError):
        await _accept_release(
            CancelledState(),
            staged,
            InstallRequest(),
        )


@pytest.mark.asyncio
async def test_release_state_changes_only_after_complete_private_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata = verified_metadata()
    archive = documents()[1]
    staging_root = tmp_path / "stage"

    async def discover(*_args, **_kwargs):
        return metadata.release

    async def verify(*_args, **_kwargs):
        return metadata

    async def download(*_args, **_kwargs):
        return archive

    class ObservingState(MemoryState):
        async def compare_and_swap(
            self,
            expected: AcceptedReleaseState | None,
            replacement: AcceptedReleaseState,
        ) -> bool:
            attempts = tuple(staging_root.iterdir())
            assert len(attempts) == 1
            assert (attempts[0] / "desktop-install.json").is_file()
            assert (attempts[0] / "runtime/tongs-desktop").is_file()
            return await super().compare_and_swap(expected, replacement)

    monkeypatch.setattr(extract_module, "discover_release", discover)
    monkeypatch.setattr(extract_module, "verify_release_metadata", verify)
    monkeypatch.setattr(extract_module, "download_asset_bytes", download)
    state = ObservingState()
    async with httpx.AsyncClient() as client:
        result = await extract_module.stage_desktop_release(
            client,
            object(),
            state,
            lambda: metadata.artifact.platform,
            staging_root,
            core_version="1.2.3",
            rpc_api_major=1,
            plugin_api_major=1,
            limits=InstallerLimits(),
            clock=lambda: metadata.release.published_at,
        )

    assert result.launcher_path.is_file()
    assert state.writes == 1


@pytest.mark.asyncio
async def test_rejected_state_cleans_only_new_owned_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata = verified_metadata()
    archive = documents()[1]

    async def discover(*_args, **_kwargs):
        return metadata.release

    async def verify(*_args, **_kwargs):
        return metadata

    async def download(*_args, **_kwargs):
        return archive

    monkeypatch.setattr(extract_module, "discover_release", discover)
    monkeypatch.setattr(extract_module, "verify_release_metadata", verify)
    monkeypatch.setattr(extract_module, "download_asset_bytes", download)
    staging_root = tmp_path / "stage"
    previous = tmp_path / "previous-install"
    previous.mkdir()
    marker = previous / "keep"
    marker.write_text("owned by previous install")
    state = MemoryState(AcceptedReleaseState("2.0.0", "a" * 64, "b" * 40))

    async with httpx.AsyncClient() as client:
        with pytest.raises(InstallerError) as raised:
            await extract_module.stage_desktop_release(
                client,
                object(),
                state,
                lambda: metadata.artifact.platform,
                staging_root,
                core_version="1.2.3",
                rpc_api_major=1,
                plugin_api_major=1,
                clock=lambda: metadata.release.published_at,
            )

    assert raised.value.code is InstallerErrorCode.ROLLBACK_BLOCKED
    assert tuple(staging_root.iterdir()) == ()
    assert marker.read_text() == "owned by previous install"
    assert state.writes == 0


def test_discard_rejects_forged_nonstaging_path(tmp_path: Path) -> None:
    staged = extract_verified_archive(
        documents()[1], verified_metadata(), tmp_path / "stage"
    )
    victim = tmp_path / "previous-install"
    victim.mkdir()
    marker = victim / "keep"
    marker.write_text("preserve")

    with pytest.raises(InstallerError) as raised:
        discard_staged_artifact(replace(staged, staging_path=victim))

    assert raised.value.code is InstallerErrorCode.EXTRACTION_FAILED
    assert marker.read_text() == "preserve"
    discard_staged_artifact(staged)


@pytest.mark.asyncio
async def test_concurrent_staging_allocates_distinct_complete_directories(
    tmp_path: Path,
) -> None:
    root = tmp_path / "stage"
    archive = documents()[1]
    metadata = verified_metadata()

    first, second = await asyncio.gather(
        asyncio.to_thread(extract_verified_archive, archive, metadata, root),
        asyncio.to_thread(extract_verified_archive, archive, metadata, root),
    )

    assert first.staging_path != second.staging_path
    assert first.launcher_path.is_file()
    assert second.launcher_path.is_file()
    discard_staged_artifact(first)
    discard_staged_artifact(second)
