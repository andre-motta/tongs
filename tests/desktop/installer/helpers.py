"""Synthetic metadata helpers layered on the deterministic S0 archive."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from tongs.desktop.artifact_contract import parse_release_manifest
from tongs.desktop.installer.metadata import (
    GITHUB_OIDC_ISSUER,
    GITHUB_WORKFLOW_BUILD_TYPE,
    INTOTO_STATEMENT_TYPE,
    OFFICIAL_REPOSITORY,
    OFFICIAL_REPOSITORY_ID,
    OFFICIAL_REPOSITORY_OWNER_ID,
    OFFICIAL_REPOSITORY_URL,
    OFFICIAL_WORKFLOW_PATH,
    RELEASE_BUNDLE_NAME,
    RELEASE_MANIFEST_NAME,
    SLSA_PREDICATE_TYPE,
)
from tongs.desktop.installer.models import (
    BuildIdentity,
    ReleaseAsset,
    ReleaseRecord,
    VerifiedReleaseMetadata,
)

ROOT = Path(__file__).parents[1]
S0_FIXTURES = ROOT / "artifact_contract" / "fixtures"
INSTALLER_FIXTURES = Path(__file__).parent / "fixtures"
ARCHIVE_NAME = "tongs-desktop-1.2.3-fedora44-x86_64.synthetic.tar.gz"
SOURCE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
NOW = datetime(2026, 9, 7, tzinfo=UTC)


def documents() -> tuple[bytes, bytes, bytes]:
    return (
        (S0_FIXTURES / "desktop-manifest-v1.synthetic.json").read_bytes(),
        (S0_FIXTURES / ARCHIVE_NAME).read_bytes(),
        (
            INSTALLER_FIXTURES / "sigstore-python-4.5.0.intoto.sigstore.json"
        ).read_bytes(),
    )


def asset(asset_id: int, name: str, document: bytes) -> ReleaseAsset:
    return ReleaseAsset(
        asset_id,
        name,
        len(document),
        hashlib.sha256(document).hexdigest(),
        f"https://api.github.com/repos/{OFFICIAL_REPOSITORY}/releases/assets/{asset_id}",
    )


def release() -> ReleaseRecord:
    manifest, archive, bundle = documents()
    return ReleaseRecord(
        77,
        "desktop-v1.2.3",
        "1.2.3",
        NOW,
        (
            asset(1, RELEASE_MANIFEST_NAME, manifest),
            asset(2, RELEASE_BUNDLE_NAME, bundle),
            asset(3, ARCHIVE_NAME, archive),
        ),
    )


def identity() -> BuildIdentity:
    ref = "refs/tags/desktop-v1.2.3"
    builder = f"{OFFICIAL_REPOSITORY_URL}/{OFFICIAL_WORKFLOW_PATH}@{ref}"
    return BuildIdentity(
        GITHUB_OIDC_ISSUER,
        OFFICIAL_REPOSITORY,
        OFFICIAL_WORKFLOW_PATH,
        ref,
        SOURCE_COMMIT,
        "push",
        builder,
    )


def verified_metadata() -> VerifiedReleaseMetadata:
    manifest_document, _archive, _bundle = documents()
    manifest = parse_release_manifest(manifest_document)
    item = manifest.artifacts[0]
    record = release()
    return VerifiedReleaseMetadata(
        record,
        manifest,
        item,
        record.assets[2],
        hashlib.sha256(manifest_document).hexdigest(),
        len(manifest_document),
        SOURCE_COMMIT,
        identity(),
    )


def statement(subjects: dict[str, str]) -> bytes:
    build = identity()
    return json.dumps(
        {
            "_type": INTOTO_STATEMENT_TYPE,
            "subject": [
                {"name": name, "digest": {"sha256": digest}}
                for name, digest in subjects.items()
            ],
            "predicateType": SLSA_PREDICATE_TYPE,
            "predicate": {
                "buildDefinition": {
                    "buildType": GITHUB_WORKFLOW_BUILD_TYPE,
                    "externalParameters": {
                        "workflow": {
                            "ref": build.ref,
                            "repository": OFFICIAL_REPOSITORY_URL,
                            "path": OFFICIAL_WORKFLOW_PATH,
                        }
                    },
                    "internalParameters": {
                        "github": {
                            "event_name": "push",
                            "repository_id": OFFICIAL_REPOSITORY_ID,
                            "repository_owner_id": OFFICIAL_REPOSITORY_OWNER_ID,
                            "runner_environment": "github-hosted",
                        }
                    },
                    "resolvedDependencies": [
                        {
                            "uri": f"git+{OFFICIAL_REPOSITORY_URL}@{build.ref}",
                            "digest": {"gitCommit": SOURCE_COMMIT},
                        }
                    ],
                },
                "runDetails": {
                    "builder": {"id": build.builder_id},
                    "metadata": {
                        "invocationId": (
                            f"{OFFICIAL_REPOSITORY_URL}/actions/runs/123/attempts/1"
                        )
                    },
                },
            },
        },
        separators=(",", ":"),
    ).encode()
