"""Strict manifest parser tests."""

from __future__ import annotations

import dataclasses
import json
from copy import deepcopy

import pytest

from tongs.desktop.artifact_contract import (
    ArtifactContractError,
    ArtifactContractErrorCode,
    PackageKind,
    TargetArchitecture,
    parse_install_manifest,
    parse_release_manifest,
    select_release_artifact,
)
from tongs.desktop.artifact_contract._json import decode_json_document

from .reference_builder import ARCHIVE_NAME, FIXTURE_ROOT, canonical_json


def _release_data() -> dict[str, object]:
    return json.loads(
        (FIXTURE_ROOT / "desktop-manifest-v1.synthetic.json").read_bytes()
    )


def _install_data() -> dict[str, object]:
    return json.loads((FIXTURE_ROOT / "desktop-install.synthetic.json").read_bytes())


def _error(
    parser: object,
    document: bytes,
    code: ArtifactContractErrorCode,
) -> ArtifactContractError:
    with pytest.raises(ArtifactContractError) as raised:
        parser(document)  # type: ignore[operator]
    assert raised.value.code is code
    return raised.value


def test_reference_manifests_parse_to_frozen_typed_models() -> None:
    release = parse_release_manifest(
        (FIXTURE_ROOT / "desktop-manifest-v1.synthetic.json").read_bytes()
    )
    install = parse_install_manifest(
        (FIXTURE_ROOT / "desktop-install.synthetic.json").read_bytes()
    )

    assert release.artifacts[0].name == ARCHIVE_NAME
    assert release.artifacts[0].package_kind is PackageKind.USER_ARCHIVE
    assert install.platform.architecture is TargetArchitecture.X86_64
    with pytest.raises(dataclasses.FrozenInstanceError):
        install.release_version = "9.9.9"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("document", "code"),
    [
        (b'{"field":1,"field":2}', ArtifactContractErrorCode.DUPLICATE_VALUE),
        (b'{"field":"\\ud800"}', ArtifactContractErrorCode.INVALID_JSON),
        (b'{"field":NaN}', ArtifactContractErrorCode.INVALID_JSON),
        (b'"not an object"', ArtifactContractErrorCode.INVALID_JSON),
        (b'{"field":"\xff"}', ArtifactContractErrorCode.INVALID_JSON),
        (
            b'{"field":' + (b"9" * 5_000) + b"}",
            ArtifactContractErrorCode.INVALID_JSON,
        ),
    ],
)
def test_json_decoder_normalizes_malformed_inputs(
    document: bytes, code: ArtifactContractErrorCode
) -> None:
    error = _error(decode_json_document, document, code)
    assert "Traceback" not in error.message


def test_json_decoder_enforces_document_depth_and_value_bounds() -> None:
    _error(
        decode_json_document,
        b'{"field":"' + (b"x" * (256 * 1024 + 1)) + b'"}',
        ArtifactContractErrorCode.LIMIT_EXCEEDED,
    )
    _error(
        decode_json_document,
        (b'{"v":' * 26) + b"0" + (b"}" * 26),
        ArtifactContractErrorCode.LIMIT_EXCEEDED,
    )


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (
            lambda value: value.update(schema_version=2),
            ArtifactContractErrorCode.UNSUPPORTED_SCHEMA,
        ),
        (
            lambda value: value.update(extra=True),
            ArtifactContractErrorCode.INVALID_FIELD,
        ),
        (
            lambda value: value.update(release_version="1.0"),
            ArtifactContractErrorCode.INVALID_FIELD,
        ),
        (
            lambda value: value.update(source_commit="not-a-commit"),
            ArtifactContractErrorCode.INVALID_FIELD,
        ),
        (
            lambda value: value["compatibility"].update(core_maximum_exclusive="1.2.0"),
            ArtifactContractErrorCode.INCOMPATIBLE,
        ),
        (
            lambda value: value["artifacts"][0].update(sha256="A" * 64),
            ArtifactContractErrorCode.INVALID_FIELD,
        ),
        (
            lambda value: value["artifacts"][0].update(ownership="system"),
            ArtifactContractErrorCode.INVALID_FIELD,
        ),
        (
            lambda value: value["artifacts"][0].update(package_kind="zip"),
            ArtifactContractErrorCode.INVALID_FIELD,
        ),
        (
            lambda value: value.update(schema_version="1"),
            ArtifactContractErrorCode.INVALID_FIELD,
        ),
    ],
)
def test_release_manifest_rejects_invalid_fields(
    mutation: object, code: ArtifactContractErrorCode
) -> None:
    data = _release_data()
    mutation(data)  # type: ignore[operator]
    _error(parse_release_manifest, canonical_json(data), code)


def test_release_manifest_rejects_duplicate_id_name_and_target() -> None:
    for field, value in (
        ("artifact_id", "fedora-44-x86-64-user-archive"),
        ("name", ARCHIVE_NAME[0].upper() + ARCHIVE_NAME[1:]),
        ("platform", deepcopy(_release_data()["artifacts"][0]["platform"])),
    ):
        data = _release_data()
        duplicate = deepcopy(data["artifacts"][0])
        duplicate["artifact_id"] = "second-artifact"
        duplicate["name"] = "second-artifact.tar.gz"
        if field == "artifact_id":
            duplicate[field] = data["artifacts"][0][field]
        elif field == "name":
            duplicate[field] = value
        else:
            duplicate[field] = value
        data["artifacts"].append(duplicate)
        _error(
            parse_release_manifest,
            canonical_json(data),
            ArtifactContractErrorCode.DUPLICATE_VALUE,
        )


def test_install_manifest_rejects_traversal_collision_and_missing_runtime() -> None:
    cases = (
        (
            lambda files: files[0].update(path="runtime/../escape"),
            ArtifactContractErrorCode.INVALID_LAYOUT,
        ),
        (
            lambda files: files.append({**files[0]}),
            ArtifactContractErrorCode.DUPLICATE_VALUE,
        ),
        (
            lambda files: files.__setitem__(
                slice(None),
                [
                    item
                    for item in files
                    if item["path"] != "runtime/resources/app.asar"
                ],
            ),
            ArtifactContractErrorCode.INVALID_FIELD,
        ),
    )
    for mutate, code in cases:
        data = _install_data()
        mutate(data["files"])
        error = _error(parse_install_manifest, canonical_json(data), code)
        assert error.message


def test_install_manifest_rejects_unicode_normalization_collision() -> None:
    data = _install_data()
    template = data["files"][0]
    data["files"].extend(
        [
            {**template, "path": "runtime/caf\u00e9"},
            {**template, "path": "runtime/cafe\u0301"},
        ]
    )
    _error(
        parse_install_manifest,
        canonical_json(data),
        ArtifactContractErrorCode.DUPLICATE_VALUE,
    )


def test_release_selection_is_exact() -> None:
    release = parse_release_manifest(
        (FIXTURE_ROOT / "desktop-manifest-v1.synthetic.json").read_bytes()
    )
    artifact = select_release_artifact(
        release, release.artifacts[0].platform, PackageKind.USER_ARCHIVE
    )
    assert artifact.artifact_id == "fedora-44-x86_64-user-archive"
    with pytest.raises(ArtifactContractError) as raised:
        select_release_artifact(release, release.artifacts[0].platform, PackageKind.RPM)
    assert raised.value.code is ArtifactContractErrorCode.INCOMPATIBLE


def test_release_distinguishes_archive_and_rpm_for_the_same_platform() -> None:
    data = _release_data()
    rpm = deepcopy(data["artifacts"][0])
    rpm.update(
        artifact_id="fedora-44-x86-64-rpm",
        name="tongs-desktop-1.2.3-1.fc44.x86_64.rpm",
        package_kind="rpm",
        ownership="system",
    )
    data["artifacts"].append(rpm)

    release = parse_release_manifest(canonical_json(data))
    selected = select_release_artifact(
        release, release.artifacts[0].platform, PackageKind.RPM
    )
    assert selected.name.endswith(".rpm")
    assert selected.ownership.value == "system"
