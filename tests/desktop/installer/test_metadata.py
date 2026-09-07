"""Release discovery and strict attestation semantics tests."""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest
from sigstore.models import Bundle
from sigstore.verify.policy import VerificationPolicy

from tongs.desktop.artifact_contract import (
    DesktopPlatform,
    TargetArchitecture,
    TargetOperatingSystem,
)
from tongs.desktop.installer.metadata import (
    INTOTO_PAYLOAD_TYPE,
    RELEASE_MANIFEST_NAME,
    _decode_json,
    discover_release,
    normalize_platform_identity,
    resolve_tag_commit,
    verify_release_metadata,
)
from tongs.desktop.installer.models import (
    InstallerError,
    InstallerErrorCode,
    InstallerLimits,
    InstallRequest,
)

from .helpers import ARCHIVE_NAME, NOW, SOURCE_COMMIT, documents, release, statement


class MockSemanticVerifier:
    """Explicitly noncryptographic harness for post-verification parsing tests."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[tuple[Bundle, VerificationPolicy]] = []

    def verify_dsse(
        self, bundle: Bundle, policy: VerificationPolicy
    ) -> tuple[str, bytes]:
        self.calls.append((bundle, policy))
        return INTOTO_PAYLOAD_TYPE, self.payload


def _release_json(*, immutable: bool = True) -> dict:
    record = release()
    return {
        "id": record.release_id,
        "tag_name": record.tag,
        "draft": False,
        "prerelease": False,
        "immutable": immutable,
        "published_at": NOW.isoformat(),
        "assets": [
            {
                "id": item.asset_id,
                "name": item.name,
                "size": item.byte_count,
                "digest": f"sha256:{item.sha256}",
                "state": "uploaded",
                "url": item.api_url,
            }
            for item in record.assets
        ],
    }


@pytest.mark.asyncio
async def test_discovers_only_exact_immutable_desktop_release() -> None:
    pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        pages.append(request.url.params["page"])
        return httpx.Response(
            200,
            json=[
                {"tag_name": "v99.0.0"},
                _release_json(),
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        selected = await discover_release(
            client,
            InstallRequest(),
            limits=InstallerLimits(releases_per_page=100),
            clock=lambda: NOW,
        )

    assert selected.version == "1.2.3"
    assert pages == ["1"]


@pytest.mark.asyncio
async def test_newer_prerelease_and_draft_do_not_hide_latest_stable_release() -> None:
    prerelease = _release_json()
    prerelease.update(
        {
            "id": 9001,
            "tag_name": "desktop-v9.0.0",
            "prerelease": True,
        }
    )
    draft = _release_json()
    draft.update(
        {
            "id": 9002,
            "tag_name": "desktop-v8.0.0",
            "draft": True,
        }
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, json=[prerelease, draft, _release_json()]
            )
        )
    ) as client:
        selected = await discover_release(
            client,
            InstallRequest(),
            limits=InstallerLimits(releases_per_page=100),
            clock=lambda: NOW,
        )

    assert selected.version == "1.2.3"


@pytest.mark.asyncio
async def test_mutable_desktop_release_is_rejected() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=[_release_json(immutable=False)])
        )
    ) as client:
        with pytest.raises(InstallerError) as raised:
            await discover_release(
                client,
                InstallRequest(),
                limits=InstallerLimits(),
                clock=lambda: NOW,
            )
    assert raised.value.code is InstallerErrorCode.INVALID_METADATA


@pytest.mark.asyncio
async def test_release_enumeration_fails_closed_at_page_bound() -> None:
    response = lambda _request: httpx.Response(200, json=[{"tag_name": "v1.0.0"}])
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(InstallerError) as raised:
            await discover_release(
                client,
                InstallRequest(),
                limits=InstallerLimits(max_release_pages=1, releases_per_page=1),
                clock=lambda: NOW,
            )

    assert raised.value.code is InstallerErrorCode.LIMIT_EXCEEDED


@pytest.mark.asyncio
async def test_annotated_release_tag_resolves_to_exact_commit() -> None:
    tag_object = "a" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        if "/git/ref/tags/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "ref": "refs/tags/desktop-v1.2.3",
                    "object": {"type": "tag", "sha": tag_object},
                },
            )
        assert request.url.path.endswith(f"/git/tags/{tag_object}")
        return httpx.Response(
            200,
            json={"object": {"type": "commit", "sha": SOURCE_COMMIT}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolved = await resolve_tag_commit(
            client, "desktop-v1.2.3", limits=InstallerLimits()
        )

    assert resolved == SOURCE_COMMIT


def test_metadata_json_rejects_duplicate_keys_nonfinite_and_invalid_unicode() -> None:
    for document in (
        b'{"id":1,"id":2}',
        b'{"value":NaN}',
        b'{"value":"\\ud800"}',
        b'{"value":' + b"9" * 5_000 + b"}",
    ):
        with pytest.raises(InstallerError):
            _decode_json(document, 10_000, "test document")


@pytest.mark.asyncio
async def test_mocked_verifier_exercises_exact_production_semantics() -> None:
    manifest, archive, bundle = documents()
    subjects = {
        RELEASE_MANIFEST_NAME: hashlib.sha256(manifest).hexdigest(),
        ARCHIVE_NAME: hashlib.sha256(archive).hexdigest(),
    }
    verifier = MockSemanticVerifier(statement(subjects))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/assets/1"):
            return httpx.Response(200, content=manifest)
        if request.url.path.endswith("/assets/2"):
            return httpx.Response(200, content=bundle)
        if "/git/ref/tags/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "ref": "refs/tags/desktop-v1.2.3",
                    "object": {"type": "commit", "sha": SOURCE_COMMIT},
                },
            )
        raise AssertionError(f"unexpected request {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await verify_release_metadata(
            client,
            verifier,
            release(),
            normalize_platform_identity("linux", "amd64", "fedora", "44", "gnu"),
            core_version="1.2.3",
            rpc_api_major=1,
            plugin_api_major=1,
            limits=InstallerLimits(),
        )

    assert result.source_commit == SOURCE_COMMIT
    assert result.artifact.name == ARCHIVE_NAME
    assert len(verifier.calls) == 1


@pytest.mark.asyncio
async def test_valid_signature_semantics_for_wrong_subject_are_rejected() -> None:
    manifest, _archive, bundle = documents()
    verifier = MockSemanticVerifier(
        statement(
            {
                RELEASE_MANIFEST_NAME: hashlib.sha256(manifest).hexdigest(),
                ARCHIVE_NAME: "0" * 64,
            }
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/assets/1"):
            return httpx.Response(200, content=manifest)
        if request.url.path.endswith("/assets/2"):
            return httpx.Response(200, content=bundle)
        return httpx.Response(
            200,
            json={
                "ref": "refs/tags/desktop-v1.2.3",
                "object": {"type": "commit", "sha": SOURCE_COMMIT},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(InstallerError) as raised:
            await verify_release_metadata(
                client,
                verifier,
                release(),
                normalize_platform_identity("linux", "x86_64", "fedora", "44", "gnu"),
                core_version="1.2.3",
                rpc_api_major=1,
                plugin_api_major=1,
                limits=InstallerLimits(),
            )
    assert raised.value.code is InstallerErrorCode.PROVENANCE_FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformation",
    [
        "extra_dependency",
        "invalid_invocation",
        "wrong_repository_id",
        "wrong_repository_owner_id",
    ],
)
async def test_valid_signature_with_noncanonical_github_predicate_is_rejected(
    malformation: str,
) -> None:
    manifest, archive, bundle = documents()
    payload = json.loads(
        statement(
            {
                RELEASE_MANIFEST_NAME: hashlib.sha256(manifest).hexdigest(),
                ARCHIVE_NAME: hashlib.sha256(archive).hexdigest(),
            }
        )
    )
    if malformation == "extra_dependency":
        payload["predicate"]["buildDefinition"]["resolvedDependencies"].append(
            {
                "uri": "git+https://example.test/extra",
                "digest": {"gitCommit": "0" * 40},
            }
        )
    elif malformation == "invalid_invocation":
        payload["predicate"]["runDetails"]["metadata"]["invocationId"] = (
            "https://example.test/actions/runs/1/attempts/1"
        )
    elif malformation == "wrong_repository_id":
        payload["predicate"]["buildDefinition"]["internalParameters"]["github"][
            "repository_id"
        ] = "1305350435"
    else:
        payload["predicate"]["buildDefinition"]["internalParameters"]["github"][
            "repository_owner_id"
        ] = "30708956"
    verifier = MockSemanticVerifier(json.dumps(payload).encode())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/assets/1"):
            return httpx.Response(200, content=manifest)
        if request.url.path.endswith("/assets/2"):
            return httpx.Response(200, content=bundle)
        return httpx.Response(
            200,
            json={
                "ref": "refs/tags/desktop-v1.2.3",
                "object": {"type": "commit", "sha": SOURCE_COMMIT},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(InstallerError) as raised:
            await verify_release_metadata(
                client,
                verifier,
                release(),
                normalize_platform_identity("linux", "amd64", "fedora", "44", "gnu"),
                core_version="1.2.3",
                rpc_api_major=1,
                plugin_api_major=1,
                limits=InstallerLimits(),
            )

    assert raised.value.code is InstallerErrorCode.PROVENANCE_FAILED


@pytest.mark.asyncio
async def test_unsupported_target_fails_before_any_download() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    unsupported = DesktopPlatform(
        TargetOperatingSystem.LINUX,
        TargetArchitecture.AARCH64,
        "fedora",
        "44",
        "gnu",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(InstallerError) as raised:
            await verify_release_metadata(
                client,
                MockSemanticVerifier(b"{}"),
                release(),
                unsupported,
                core_version="1.2.3",
                rpc_api_major=1,
                plugin_api_major=1,
                limits=InstallerLimits(),
            )
    assert raised.value.code is InstallerErrorCode.UNSUPPORTED_PLATFORM
    assert called is False
