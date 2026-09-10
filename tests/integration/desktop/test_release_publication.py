"""Prove the release publication commands assemble and verify fail-closed.

These cases build a real release manifest and archive with the producer's own
document builder, so what ``verify`` accepts here is what the installer's
release path would accept, and the bundle verification is exercised through
the installer's own ``_verify_production_attestation`` with a policy-aware
verifier stub that replays the statement the trusted workflow would sign.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.build_desktop_archive import (
    BuildParameters,
    build_contract_documents,
    canonical_json,
)
from tests.integration.desktop import release_publication as publication
from tongs.desktop.installer.metadata import (
    INTOTO_PAYLOAD_TYPE,
    OFFICIAL_REPOSITORY_ID,
    OFFICIAL_REPOSITORY_OWNER_ID,
    OFFICIAL_REPOSITORY_URL,
    OFFICIAL_WORKFLOW_PATH,
    RELEASE_BUNDLE_NAME,
    RELEASE_MANIFEST_NAME,
    _build_identity,
)
from tongs.desktop.installer.models import BuildIdentity

TAG = "v1.0.0"
VERSION = "1.0.0"
SOURCE_COMMIT = "c" * 40
ROOT = Path(__file__).resolve().parents[3]
BUNDLE_FIXTURE = (
    ROOT / "tests/desktop/installer/fixtures/sigstore-python-4.5.0.intoto.sigstore.json"
)
RPM_NAMES = (
    f"tongs-desktop-{VERSION}-0.1.20260910gitcccccc.fc44.x86_64.rpm",
    f"python3-tongs-{VERSION}-0.1.20260910gitcccccc.fc44.noarch.rpm",
    f"python3-tongs+mcp-{VERSION}-0.1.20260910gitcccccc.fc44.noarch.rpm",
)
EXCLUDED_RPM_NAMES = (
    f"tongs-desktop-{VERSION}-0.1.20260910gitcccccc.fc44.src.rpm",
    f"tongs-desktop-debuginfo-{VERSION}-0.1.20260910gitcccccc.fc44.x86_64.rpm",
    f"tongs-desktop-debugsource-{VERSION}-0.1.20260910gitcccccc.fc44.x86_64.rpm",
)
COMPANION_RPM_NAMES = ("python3-mcp-1.2.0-1.fc44.noarch.rpm",)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _statement(identity: BuildIdentity, subjects: dict[str, str]) -> bytes:
    return canonical_json(
        {
            "_type": "https://in-toto.io/Statement/v1",
            "subject": [
                {"name": name, "digest": {"sha256": digest}}
                for name, digest in subjects.items()
            ],
            "predicateType": "https://slsa.dev/provenance/v1",
            "predicate": {
                "buildDefinition": {
                    "buildType": "https://actions.github.io/buildtypes/workflow/v1",
                    "externalParameters": {
                        "workflow": {
                            "ref": identity.ref,
                            "repository": OFFICIAL_REPOSITORY_URL,
                            "path": OFFICIAL_WORKFLOW_PATH,
                        }
                    },
                    "internalParameters": {
                        "github": {
                            "event_name": identity.event,
                            "repository_id": OFFICIAL_REPOSITORY_ID,
                            "repository_owner_id": OFFICIAL_REPOSITORY_OWNER_ID,
                            "runner_environment": "github-hosted",
                        }
                    },
                    "resolvedDependencies": [
                        {
                            "uri": f"git+{OFFICIAL_REPOSITORY_URL}@{identity.ref}",
                            "digest": {"gitCommit": identity.source_commit},
                        }
                    ],
                },
                "runDetails": {
                    "builder": {"id": identity.builder_id},
                    "metadata": {
                        "invocationId": (
                            f"{OFFICIAL_REPOSITORY_URL}/actions/runs/1234/attempts/1"
                        )
                    },
                },
            },
        }
    )


class _PolicyAwareVerifier:
    """Accept only the certificate policy the installer derives from the tag."""

    def __init__(self, identity: BuildIdentity, payload: bytes) -> None:
        self.identity = identity
        self.payload = payload
        self.calls = 0

    def verify_dsse(self, bundle: object, policy: Any) -> tuple[str, bytes]:
        del bundle
        self.calls += 1
        values = {
            (type(child).__name__, getattr(child, "_value", None))
            for child in policy._children
        }
        expected = {
            ("OIDCSourceRepositoryDigest", self.identity.source_commit),
            ("OIDCSourceRepositoryRef", self.identity.ref),
            ("OIDCBuildSignerURI", self.identity.builder_id),
            ("OIDCBuildConfigURI", self.identity.builder_id),
            ("OIDCBuildTrigger", self.identity.event),
        }
        if not expected <= values:
            from sigstore.errors import VerificationError

            raise VerificationError("policy does not match the release identity")
        return INTOTO_PAYLOAD_TYPE, self.payload


def _write_producer_output(
    root: Path, *, release_version: str = VERSION, core_maximum: str = "2.0.0"
) -> dict[str, bytes]:
    """Lay out the attestation artifact and the RPM artifact like the workflow."""

    archive_root = root / "transfer" / "archive"
    evidence_root = root / "transfer" / publication.ATTESTATION_EVIDENCE_DIRECTORY
    archive_root.mkdir(parents=True)
    evidence_root.mkdir(parents=True)
    contract = json.loads(
        (ROOT / "packaging/desktop/archive/contract.json").read_text()
    )
    payload = {
        "runtime/tongs-desktop": (b"launcher", 0o755),
        "runtime/resources/app.asar": (b"asar", 0o644),
        "runtime/LICENSES.json": (canonical_json({"components": []}), 0o644),
    }
    built = build_contract_documents(
        payload,
        BuildParameters(
            release_version=release_version,
            core_minimum="0.4.2-dev.183",
            core_maximum_exclusive=core_maximum,
            source_commit=SOURCE_COMMIT,
            source_date_epoch=1_700_000_000,
        ),
        contract,
    )
    (archive_root / built.archive_name).write_bytes(built.archive)
    (archive_root / RELEASE_MANIFEST_NAME).write_bytes(built.release_manifest)
    (archive_root / "desktop-install.json").write_bytes(built.install_manifest)
    bundle = BUNDLE_FIXTURE.read_bytes()
    (evidence_root / publication.CANDIDATE_BUNDLE_NAME).write_bytes(bundle)
    (evidence_root / publication.CANDIDATE_SBOM_NAME).write_bytes(
        canonical_json({"spdxVersion": "SPDX-2.3"})
    )
    (evidence_root / publication.CANDIDATE_SBOM_BUNDLE_NAME).write_bytes(bundle)

    final = root / "rpm" / publication.FINAL_RPM_DIRECTORY
    companions = root / "rpm" / publication.COMPANION_RPM_DIRECTORY
    final.mkdir(parents=True)
    companions.mkdir(parents=True)
    for name in (*RPM_NAMES, *EXCLUDED_RPM_NAMES):
        (final / name).write_bytes(f"rpm {name}\n".encode())
    (final / "SHA256SUMS").write_text("ignored producer checksum list\n")
    for name in COMPANION_RPM_NAMES:
        (companions / name).write_bytes(f"rpm {name}\n".encode())
    (root / "notes.md").write_text("# Tongs v1.0.0\n\nRelease notes.\n")
    return {
        "archive": built.archive,
        "manifest": built.release_manifest,
        "archive_name": built.archive_name.encode(),
    }


def _verifier_for(root: Path, produced: dict[str, bytes]) -> _PolicyAwareVerifier:
    identity = _build_identity(TAG, SOURCE_COMMIT)
    subjects = {
        RELEASE_MANIFEST_NAME: _digest(produced["manifest"]),
        produced["archive_name"].decode(): _digest(produced["archive"]),
    }
    return _PolicyAwareVerifier(identity, _statement(identity, subjects))


def _assemble(root: Path) -> Path:
    output = root / "assets"
    publication.assemble_assets(
        tag=publication.ReleaseTag.parse(TAG),
        transfer_root=root / "transfer",
        rpm_root=root / "rpm",
        output_dir=output,
    )
    return output


def _verify(root: Path, assets: Path, verifier: _PolicyAwareVerifier) -> dict:
    return publication.verify_assets(
        tag=publication.ReleaseTag.parse(TAG),
        source_commit=SOURCE_COMMIT,
        assets_dir=assets,
        notes_path=root / "notes.md",
        verifier=verifier,
    )


def _rewrite_checksums(assets: Path) -> None:
    lines = []
    for entry in sorted(assets.iterdir()):
        if entry.name == publication.CHECKSUM_FILE_NAME:
            continue
        lines.append(f"{_digest(entry.read_bytes())}  {entry.name}")
    (assets / publication.CHECKSUM_FILE_NAME).write_text("\n".join(lines) + "\n")


def test_assemble_publishes_exactly_the_installer_assets_and_binary_rpms(
    tmp_path: Path,
) -> None:
    produced = _write_producer_output(tmp_path)
    assets = _assemble(tmp_path)

    names = sorted(entry.name for entry in assets.iterdir())
    assert names == sorted(
        [
            RELEASE_MANIFEST_NAME,
            RELEASE_BUNDLE_NAME,
            publication.archive_name_for(VERSION),
            publication.sbom_name_for(VERSION),
            publication.sbom_bundle_name_for(VERSION),
            publication.CHECKSUM_FILE_NAME,
            *RPM_NAMES,
            *COMPANION_RPM_NAMES,
        ]
    )
    for excluded in EXCLUDED_RPM_NAMES:
        assert not (assets / excluded).exists()
    assert (assets / RELEASE_BUNDLE_NAME).read_bytes() == BUNDLE_FIXTURE.read_bytes()
    assert (assets / RELEASE_MANIFEST_NAME).read_bytes() == produced["manifest"]
    checksums = publication.parse_checksums(
        (assets / publication.CHECKSUM_FILE_NAME).read_bytes()
    )
    assert set(checksums) == set(names) - {publication.CHECKSUM_FILE_NAME}
    for name, digest in checksums.items():
        assert _digest((assets / name).read_bytes()) == digest


def test_assemble_refuses_an_existing_output_and_a_missing_rpm_set(
    tmp_path: Path,
) -> None:
    _write_producer_output(tmp_path)
    (tmp_path / "assets").mkdir()
    with pytest.raises(publication.ReleasePublicationError, match="must not already"):
        _assemble(tmp_path)
    (tmp_path / "assets").rmdir()
    for name in (*RPM_NAMES, *EXCLUDED_RPM_NAMES):
        (tmp_path / "rpm" / publication.FINAL_RPM_DIRECTORY / name).unlink()
    for name in COMPANION_RPM_NAMES:
        (tmp_path / "rpm" / publication.COMPANION_RPM_DIRECTORY / name).unlink()
    with pytest.raises(publication.ReleasePublicationError, match="no binary RPM"):
        _assemble(tmp_path)


def test_assemble_refuses_a_symlinked_producer_file(tmp_path: Path) -> None:
    _write_producer_output(tmp_path)
    manifest = tmp_path / "transfer" / "archive" / RELEASE_MANIFEST_NAME
    real = tmp_path / "elsewhere.json"
    manifest.rename(real)
    manifest.symlink_to(real)
    with pytest.raises(publication.ReleasePublicationError, match="unable to open"):
        _assemble(tmp_path)


def test_verify_accepts_the_assembled_release_under_the_installer_policy(
    tmp_path: Path,
) -> None:
    produced = _write_producer_output(tmp_path)
    assets = _assemble(tmp_path)
    verifier = _verifier_for(tmp_path, produced)

    report = _verify(tmp_path, assets, verifier)

    assert report["result"] == "pass"
    assert report["release_version"] == VERSION
    assert report["builder_id"] == (
        f"{OFFICIAL_REPOSITORY_URL}/{OFFICIAL_WORKFLOW_PATH}@refs/tags/{TAG}"
    )
    assert set(report["subjects"]) == {
        RELEASE_MANIFEST_NAME,
        publication.archive_name_for(VERSION),
    }
    assert report["rpms"] == sorted([*RPM_NAMES, *COMPANION_RPM_NAMES])
    assert verifier.calls == 1


def test_verify_rejects_a_bundle_signed_for_another_tag_or_commit(
    tmp_path: Path,
) -> None:
    produced = _write_producer_output(tmp_path)
    assets = _assemble(tmp_path)
    identity = _build_identity("v1.0.1", SOURCE_COMMIT)
    subjects = {
        RELEASE_MANIFEST_NAME: _digest(produced["manifest"]),
        produced["archive_name"].decode(): _digest(produced["archive"]),
    }
    verifier = _PolicyAwareVerifier(identity, _statement(identity, subjects))
    with pytest.raises(publication.ReleasePublicationError, match="release policy"):
        _verify(tmp_path, assets, verifier)

    identity = _build_identity(TAG, "d" * 40)
    verifier = _PolicyAwareVerifier(identity, _statement(identity, subjects))
    with pytest.raises(publication.ReleasePublicationError, match="release policy"):
        _verify(tmp_path, assets, verifier)


def test_verify_rejects_a_statement_whose_subjects_do_not_match(
    tmp_path: Path,
) -> None:
    produced = _write_producer_output(tmp_path)
    assets = _assemble(tmp_path)
    identity = _build_identity(TAG, SOURCE_COMMIT)
    subjects = {
        RELEASE_MANIFEST_NAME: _digest(produced["manifest"]),
        produced["archive_name"].decode(): "e" * 64,
    }
    verifier = _PolicyAwareVerifier(identity, _statement(identity, subjects))
    with pytest.raises(publication.ReleasePublicationError, match="release policy"):
        _verify(tmp_path, assets, verifier)


def test_verify_rejects_a_manifest_for_another_version_or_commit(
    tmp_path: Path,
) -> None:
    produced = _write_producer_output(tmp_path, release_version="1.0.1")
    (
        tmp_path / "transfer" / "archive" / publication.archive_name_for(VERSION)
    ).write_bytes(produced["archive"])
    assets = _assemble(tmp_path)
    verifier = _verifier_for(tmp_path, produced)
    with pytest.raises(
        publication.ReleasePublicationError, match="does not equal the tag's version"
    ):
        _verify(tmp_path, assets, verifier)

    produced = _write_producer_output(tmp_path / "other")
    assets = _assemble(tmp_path / "other")
    with pytest.raises(
        publication.ReleasePublicationError, match="does not equal the tag's commit"
    ):
        publication.verify_assets(
            tag=publication.ReleaseTag.parse(TAG),
            source_commit="d" * 40,
            assets_dir=assets,
            notes_path=tmp_path / "other" / "notes.md",
            verifier=_verifier_for(tmp_path / "other", produced),
        )


def test_verify_rejects_a_release_the_installer_would_find_incompatible(
    tmp_path: Path,
) -> None:
    """The tag's own core must fall inside the manifest's compatibility interval."""

    produced = _write_producer_output(tmp_path, core_maximum="0.5.0")
    assets = _assemble(tmp_path)
    verifier = _verifier_for(tmp_path, produced)
    with pytest.raises(publication.ReleasePublicationError, match="would refuse"):
        _verify(tmp_path, assets, verifier)
    assert verifier.calls == 0


def test_verify_rejects_tampered_assets_and_checksums(tmp_path: Path) -> None:
    produced = _write_producer_output(tmp_path)
    assets = _assemble(tmp_path)
    verifier = _verifier_for(tmp_path, produced)

    archive = assets / publication.archive_name_for(VERSION)
    original = archive.read_bytes()
    archive.write_bytes(original + b"\n")
    with pytest.raises(publication.ReleasePublicationError, match="disagrees with"):
        _verify(tmp_path, assets, verifier)

    _rewrite_checksums(assets)
    with pytest.raises(
        publication.ReleasePublicationError, match="disagrees with the release manifest"
    ):
        _verify(tmp_path, assets, verifier)

    archive.write_bytes(original)
    _rewrite_checksums(assets)
    (assets / "stray.txt").write_text("stray\n")
    with pytest.raises(publication.ReleasePublicationError, match="does not cover"):
        _verify(tmp_path, assets, verifier)
    _rewrite_checksums(assets)
    with pytest.raises(publication.ReleasePublicationError, match="unexpected assets"):
        _verify(tmp_path, assets, verifier)


def test_verify_requires_the_desktop_and_core_rpms_and_non_empty_notes(
    tmp_path: Path,
) -> None:
    produced = _write_producer_output(tmp_path)
    assets = _assemble(tmp_path)
    verifier = _verifier_for(tmp_path, produced)

    (tmp_path / "notes.md").write_text("\n")
    with pytest.raises(publication.ReleasePublicationError, match="notes are empty"):
        _verify(tmp_path, assets, verifier)
    (tmp_path / "notes.md").write_text("# notes\n")

    (assets / RPM_NAMES[0]).unlink()
    _rewrite_checksums(assets)
    with pytest.raises(publication.ReleasePublicationError, match="no RPM named"):
        _verify(tmp_path, assets, verifier)


@pytest.mark.parametrize(
    "tag", ["1.0.0", "v1.0", "v1.0.0rc1", "desktop-v1.0.0", "v01.0.0", "V1.0.0"]
)
def test_only_stable_v_tags_are_release_tags(tag: str) -> None:
    with pytest.raises(publication.ReleasePublicationError, match="not a stable"):
        publication.ReleaseTag.parse(tag)


def test_stable_tag_names_its_version() -> None:
    parsed = publication.ReleaseTag.parse("v12.0.3")
    assert (parsed.tag, parsed.version) == ("v12.0.3", "12.0.3")


def test_require_absent_distinguishes_absent_present_and_errors() -> None:
    tag = publication.ReleaseTag.parse(TAG)
    calls: list[list[str]] = []

    def absent(arguments: Any) -> tuple[int, str, str]:
        calls.append(list(arguments))
        return 1, "", "gh: Not Found (HTTP 404)\n"

    publication.require_release_absent(tag, gh=absent)
    assert calls == [["api", "repos/andre-motta/tongs/releases/tags/v1.0.0"]]

    with pytest.raises(publication.ReleasePublicationError, match="already exists"):
        publication.require_release_absent(tag, gh=lambda _: (0, "{}", ""))
    with pytest.raises(publication.ReleasePublicationError, match="lookup .* failed"):
        publication.require_release_absent(
            tag, gh=lambda _: (1, "", "gh: connection reset (HTTP 503)\n")
        )


def _published(assets_dir: Path, **overrides: Any) -> dict[str, Any]:
    release: dict[str, Any] = {
        "id": 42,
        "tag_name": TAG,
        "draft": False,
        "prerelease": False,
        "immutable": True,
        "assets": [
            {
                "name": entry.name,
                "size": entry.stat().st_size,
                "digest": f"sha256:{_digest(entry.read_bytes())}",
                "state": "uploaded",
            }
            for entry in sorted(assets_dir.iterdir())
        ],
    }
    release.update(overrides)
    return release


def test_verify_published_confirms_an_immutable_release_with_every_asset(
    tmp_path: Path,
) -> None:
    _write_producer_output(tmp_path)
    assets = _assemble(tmp_path)
    tag = publication.ReleaseTag.parse(TAG)
    document = json.dumps(_published(assets))

    report = publication.verify_published_release(
        tag, assets, expect_draft=False, gh=lambda _: (0, document, "")
    )
    assert report["result"] == "pass"
    assert report["immutable"] is True
    assert report["assets"] == sorted(entry.name for entry in assets.iterdir())

    draft = json.dumps(_published(assets, draft=True, immutable=False))
    report = publication.verify_published_release(
        tag, assets, expect_draft=True, gh=lambda _: (0, draft, "")
    )
    assert report["draft"] is True


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"immutable": False}, "not immutable"),
        ({"draft": True}, "not published"),
        ({"prerelease": True}, "prerelease"),
        ({"tag_name": "v1.0.1"}, "does not equal the requested tag"),
        ({"assets": []}, "differs from the assembled set"),
    ],
)
def test_verify_published_rejects_a_release_the_installer_would_refuse(
    tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    _write_producer_output(tmp_path)
    assets = _assemble(tmp_path)
    tag = publication.ReleaseTag.parse(TAG)
    document = json.dumps(_published(assets, **overrides))
    with pytest.raises(publication.ReleasePublicationError, match=message):
        publication.verify_published_release(
            tag, assets, expect_draft=False, gh=lambda _: (0, document, "")
        )


def test_verify_published_rejects_a_partial_or_altered_upload(tmp_path: Path) -> None:
    _write_producer_output(tmp_path)
    assets = _assemble(tmp_path)
    tag = publication.ReleaseTag.parse(TAG)

    release = _published(assets)
    release["assets"][0]["state"] = "starter"
    with pytest.raises(publication.ReleasePublicationError, match="not fully uploaded"):
        publication.verify_published_release(
            tag, assets, expect_draft=False, gh=lambda _: (0, json.dumps(release), "")
        )

    release = _published(assets)
    release["assets"][0]["digest"] = "sha256:" + "f" * 64
    with pytest.raises(publication.ReleasePublicationError, match="differs from the"):
        publication.verify_published_release(
            tag, assets, expect_draft=False, gh=lambda _: (0, json.dumps(release), "")
        )

    with pytest.raises(publication.ReleasePublicationError, match="lookup .* failed"):
        publication.verify_published_release(
            tag, assets, expect_draft=False, gh=lambda _: (1, "", "HTTP 500")
        )


def test_cli_assembles_and_verifies_with_reports(tmp_path: Path, monkeypatch) -> None:
    produced = _write_producer_output(tmp_path)
    verifier = _verifier_for(tmp_path, produced)
    monkeypatch.setattr(
        publication.Verifier, "production", staticmethod(lambda: verifier)
    )
    assets = tmp_path / "assets"
    assert (
        publication.main(
            [
                "assemble",
                "--tag",
                TAG,
                "--transfer-root",
                str(tmp_path / "transfer"),
                "--rpm-root",
                str(tmp_path / "rpm"),
                "--output-dir",
                str(assets),
            ]
        )
        == 0
    )
    report = tmp_path / "reports" / "release-verification.json"
    assert (
        publication.main(
            [
                "verify",
                "--tag",
                TAG,
                "--source-commit",
                SOURCE_COMMIT,
                "--assets-dir",
                str(assets),
                "--notes",
                str(tmp_path / "notes.md"),
                "--report",
                str(report),
            ]
        )
        == 0
    )
    assert json.loads(report.read_text())["result"] == "pass"
    assert (
        publication.main(
            [
                "verify",
                "--tag",
                TAG,
                "--source-commit",
                "d" * 40,
                "--assets-dir",
                str(assets),
                "--notes",
                str(tmp_path / "notes.md"),
            ]
        )
        == 1
    )
    assert publication.main(["require-absent", "--tag", "v1.0"]) == 1
