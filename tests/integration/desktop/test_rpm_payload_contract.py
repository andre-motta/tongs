"""Prove the exact-mode payload contract is bound to the fresh archive only.

These cases run the real ``packaging/rpm/desktop/package_contract.py``
validator and the checked-in base manifest, so a contract this module accepts
is one the issue #52 producer would also accept, and a contract it rejects
could not have reached the producer.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from tests.integration.desktop.rpm_payload_contract import (
    BASE_MANIFEST,
    CHECKSUM_FILE_NAME,
    INSTALL_MANIFEST_NAME,
    RELEASE_MANIFEST_NAME,
    ROOT,
    PayloadContractError,
    archive_filename_for,
    materialize_payload_contract,
    parse_checksums,
    require_exact_pairing,
)

SOURCE_COMMIT = "a" * 40
CORE_VERSION = "0.4.2.dev327"
ARTIFACT_ID = "10076121963"
ARTIFACT_NAME = f"desktop-archive-{SOURCE_COMMIT}-34274245440-1"
RUN_ID = "34274245440"
TAGGED_RELEASE_VERSION = "1.0.0"


def _base() -> dict[str, Any]:
    return json.loads((ROOT / BASE_MANIFEST).read_text())


def _write_archive_dir(directory: Path, release_version: str) -> Path:
    """Build one archive directory shaped like the real producer output."""

    directory.mkdir()
    base = _base()["accepted_desktop"]
    records: dict[str, bytes] = {}
    for index, name in enumerate(sorted(base["evidence"])):
        if name == CHECKSUM_FILE_NAME:
            continue
        record: dict[str, Any] = {"schema_version": 1, "record": name, "index": index}
        if name in {RELEASE_MANIFEST_NAME, INSTALL_MANIFEST_NAME}:
            record["release_version"] = release_version
        records[name] = json.dumps(record).encode()
    archive_name = archive_filename_for(release_version)
    records[archive_name] = b"fresh desktop archive payload\n" * 64

    lines = []
    for name in sorted(records):
        payload = records[name]
        (directory / name).write_bytes(payload)
        lines.append(f"{hashlib.sha256(payload).hexdigest()}  {name}")
    (directory / CHECKSUM_FILE_NAME).write_text("\n".join(lines) + "\n")
    return directory


@pytest.fixture()
def archive_dir(tmp_path: Path) -> Path:
    """An archive produced for the checked-in candidate release version."""

    base = _base()["accepted_desktop"]
    assert base["archive"]["filename"] == archive_filename_for(base["release_version"])
    return _write_archive_dir(tmp_path / "archive", base["release_version"])


@pytest.fixture()
def tagged_archive_dir(tmp_path: Path) -> Path:
    """An archive produced for a release tag's version."""

    return _write_archive_dir(tmp_path / "tagged-archive", TAGGED_RELEASE_VERSION)


def _materialize(archive_dir: Path, output: Path, **overrides: Any):
    arguments: dict[str, Any] = {
        "base_manifest_path": ROOT / BASE_MANIFEST,
        "archive_dir": archive_dir,
        "output_path": output,
        "source_commit": SOURCE_COMMIT,
        "artifact_id": ARTIFACT_ID,
        "artifact_name": ARTIFACT_NAME,
        "run_id": RUN_ID,
    }
    arguments.update(overrides)
    return materialize_payload_contract(**arguments)


def test_materializes_a_contract_bound_to_the_fresh_archive(
    archive_dir: Path, tmp_path: Path
) -> None:
    output = tmp_path / "payload-input-contract.json"
    binding = _materialize(archive_dir, output)

    contract = json.loads(output.read_text())
    base = _base()
    accepted = contract["accepted_desktop"]

    assert accepted["source_commit"] == SOURCE_COMMIT
    assert accepted["artifact_id"] == int(ARTIFACT_ID)
    assert accepted["artifact_name"] == ARTIFACT_NAME
    assert accepted["run_id"] == int(RUN_ID)
    assert "reviewed_fixture" not in accepted
    assert "rpm_pairing" not in contract

    filename = base["accepted_desktop"]["archive"]["filename"]
    observed = (archive_dir / filename).read_bytes()
    assert accepted["archive"] == {
        "filename": filename,
        "bytes": len(observed),
        "sha256": hashlib.sha256(observed).hexdigest(),
    }
    assert (
        accepted["archive"]["sha256"] != (base["accepted_desktop"]["archive"]["sha256"])
    )
    assert set(accepted["evidence"]) == set(base["accepted_desktop"]["evidence"])
    assert accepted["evidence"] != base["accepted_desktop"]["evidence"]
    assert binding.evidence_count == len(accepted["evidence"])
    assert binding.contract_sha256 == hashlib.sha256(output.read_bytes()).hexdigest()

    for key in ("target", "core_runtime_requirements", "companion_binary_count"):
        assert contract[key] == base[key]
    for key in ("electron_version", "release_version", "compatibility"):
        assert accepted[key] == base["accepted_desktop"][key]


def test_release_version_override_binds_the_tagged_archive(
    tagged_archive_dir: Path, tmp_path: Path
) -> None:
    """A release tag renames the accepted archive and release version only."""

    output = tmp_path / "payload-input-contract.json"
    binding = _materialize(
        tagged_archive_dir, output, release_version=TAGGED_RELEASE_VERSION
    )

    contract = json.loads(output.read_text())
    base = _base()["accepted_desktop"]
    accepted = contract["accepted_desktop"]
    filename = archive_filename_for(TAGGED_RELEASE_VERSION)
    observed = (tagged_archive_dir / filename).read_bytes()

    assert accepted["release_version"] == TAGGED_RELEASE_VERSION
    assert accepted["archive"] == {
        "filename": filename,
        "bytes": len(observed),
        "sha256": hashlib.sha256(observed).hexdigest(),
    }
    assert binding.archive_filename == filename
    assert set(accepted["evidence"]) == set(base["evidence"])
    for key in ("electron_version", "compatibility"):
        assert accepted[key] == base[key]
    # Exact pairing depends on the source commit, not on the release version.
    # Whether the tagged core itself falls inside the copied compatibility
    # interval is the base manifest's reviewed policy, pinned by
    # tests/ci/test_desktop_production_expectations.py rather than here.
    require_exact_pairing(contract, SOURCE_COMMIT, CORE_VERSION)


def test_release_version_override_refuses_an_archive_built_for_another_version(
    archive_dir: Path, tmp_path: Path
) -> None:
    """The candidate archive cannot be relabelled as a tagged release."""

    with pytest.raises(PayloadContractError, match="does not cover the reviewed"):
        _materialize(archive_dir, tmp_path / "contract.json", release_version="1.0.0")


def test_release_version_override_refuses_manifests_that_disagree(
    tagged_archive_dir: Path, tmp_path: Path
) -> None:
    """An archive named for the tag must also declare the tag's version."""

    manifest = tagged_archive_dir / RELEASE_MANIFEST_NAME
    document = json.loads(manifest.read_text())
    document["release_version"] = "0.5.0"
    payload = json.dumps(document).encode()
    manifest.write_bytes(payload)
    lines = [
        line
        if not line.endswith(f"  {RELEASE_MANIFEST_NAME}")
        else f"{hashlib.sha256(payload).hexdigest()}  {RELEASE_MANIFEST_NAME}"
        for line in (tagged_archive_dir / CHECKSUM_FILE_NAME).read_text().splitlines()
    ]
    (tagged_archive_dir / CHECKSUM_FILE_NAME).write_text("\n".join(lines) + "\n")
    with pytest.raises(PayloadContractError, match="does not carry release version"):
        _materialize(
            tagged_archive_dir,
            tmp_path / "contract.json",
            release_version=TAGGED_RELEASE_VERSION,
        )


@pytest.mark.parametrize(
    "value", ["v1.0.0", "1.0", "1.0.0rc1", "01.0.0", "", "1.0.0\n"]
)
def test_rejects_a_malformed_release_version_override(
    tagged_archive_dir: Path, tmp_path: Path, value: str
) -> None:
    with pytest.raises(PayloadContractError, match="release version is invalid"):
        _materialize(
            tagged_archive_dir, tmp_path / "contract.json", release_version=value
        )


def test_the_candidate_archive_must_declare_the_candidate_version(
    archive_dir: Path, tmp_path: Path
) -> None:
    """Without an override the archive must still carry the base version."""

    install = archive_dir / INSTALL_MANIFEST_NAME
    document = json.loads(install.read_text())
    document["release_version"] = "9.9.9"
    payload = json.dumps(document).encode()
    install.write_bytes(payload)
    lines = [
        line
        if not line.endswith(f"  {INSTALL_MANIFEST_NAME}")
        else f"{hashlib.sha256(payload).hexdigest()}  {INSTALL_MANIFEST_NAME}"
        for line in (archive_dir / CHECKSUM_FILE_NAME).read_text().splitlines()
    ]
    (archive_dir / CHECKSUM_FILE_NAME).write_text("\n".join(lines) + "\n")
    with pytest.raises(PayloadContractError, match="does not carry release version"):
        _materialize(archive_dir, tmp_path / "contract.json")


def test_materialized_contract_binds_only_as_exact_pairing(
    archive_dir: Path, tmp_path: Path
) -> None:
    output = tmp_path / "payload-input-contract.json"
    _materialize(archive_dir, output)
    contract = json.loads(output.read_text())
    require_exact_pairing(contract, SOURCE_COMMIT, CORE_VERSION)


def test_materialized_contract_refuses_a_different_core_commit(
    archive_dir: Path, tmp_path: Path
) -> None:
    output = tmp_path / "payload-input-contract.json"
    _materialize(archive_dir, output)
    contract = json.loads(output.read_text())
    with pytest.raises(RuntimeError, match="same source commit"):
        require_exact_pairing(contract, "b" * 40, CORE_VERSION)


def test_rejects_evidence_that_disagrees_with_the_producer_checksums(
    archive_dir: Path, tmp_path: Path
) -> None:
    name = next(
        item
        for item in sorted(_base()["accepted_desktop"]["evidence"])
        if item != CHECKSUM_FILE_NAME
    )
    (archive_dir / name).write_bytes(b'{"tampered": true}')
    with pytest.raises(PayloadContractError, match="disagrees with the producer"):
        _materialize(archive_dir, tmp_path / "contract.json")


def test_rejects_an_archive_that_disagrees_with_the_producer_checksums(
    archive_dir: Path, tmp_path: Path
) -> None:
    filename = _base()["accepted_desktop"]["archive"]["filename"]
    (archive_dir / filename).write_bytes(b"substituted archive\n")
    with pytest.raises(PayloadContractError, match="disagrees with the producer"):
        _materialize(archive_dir, tmp_path / "contract.json")


def test_rejects_an_incomplete_producer_checksum_list(
    archive_dir: Path, tmp_path: Path
) -> None:
    name = next(
        item
        for item in sorted(_base()["accepted_desktop"]["evidence"])
        if item != CHECKSUM_FILE_NAME
    )
    lines = [
        line
        for line in (archive_dir / CHECKSUM_FILE_NAME).read_text().splitlines()
        if not line.endswith(f"  {name}")
    ]
    (archive_dir / CHECKSUM_FILE_NAME).write_text("\n".join(lines) + "\n")
    with pytest.raises(PayloadContractError, match="does not cover the reviewed"):
        _materialize(archive_dir, tmp_path / "contract.json")


def test_rejects_an_unexpected_extra_checksum_record(
    archive_dir: Path, tmp_path: Path
) -> None:
    payload = b"extra\n"
    (archive_dir / "extra-inventory.json").write_bytes(payload)
    with (archive_dir / CHECKSUM_FILE_NAME).open("a") as handle:
        handle.write(f"{hashlib.sha256(payload).hexdigest()}  extra-inventory.json\n")
    with pytest.raises(PayloadContractError, match="does not cover the reviewed"):
        _materialize(archive_dir, tmp_path / "contract.json")


def test_rejects_a_missing_archive_file(archive_dir: Path, tmp_path: Path) -> None:
    (archive_dir / _base()["accepted_desktop"]["archive"]["filename"]).unlink()
    with pytest.raises(PayloadContractError, match="unable to open"):
        _materialize(archive_dir, tmp_path / "contract.json")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_commit", "not-a-commit"),
        ("source_commit", "A" * 40),
        ("artifact_id", "abc"),
        ("run_id", ""),
        ("artifact_name", ""),
    ],
)
def test_rejects_malformed_caller_identity(
    archive_dir: Path, tmp_path: Path, field: str, value: str
) -> None:
    with pytest.raises(PayloadContractError, match="invalid"):
        _materialize(archive_dir, tmp_path / "contract.json", **{field: value})


def test_refuses_to_overwrite_an_existing_contract(
    archive_dir: Path, tmp_path: Path
) -> None:
    output = tmp_path / "contract.json"
    output.write_text("{}\n")
    with pytest.raises(PayloadContractError, match="must not already exist"):
        _materialize(archive_dir, output)


def test_refuses_a_base_manifest_that_is_already_bound(
    archive_dir: Path, tmp_path: Path
) -> None:
    base = _base()
    base["rpm_pairing"] = {
        "core_pep440_version": CORE_VERSION,
        "core_source_commit": SOURCE_COMMIT,
        "mode": "exact",
        "payload_source_commit": SOURCE_COMMIT,
    }
    manifest = tmp_path / "base.json"
    manifest.write_text(json.dumps(base))
    with pytest.raises(PayloadContractError, match="must not already be bound"):
        _materialize(
            archive_dir, tmp_path / "contract.json", base_manifest_path=manifest
        )


def test_refuses_a_base_manifest_without_the_reviewed_fixture_block(
    archive_dir: Path, tmp_path: Path
) -> None:
    base = _base()
    base["accepted_desktop"].pop("reviewed_fixture")
    manifest = tmp_path / "base.json"
    manifest.write_text(json.dumps(base))
    with pytest.raises(PayloadContractError, match="reviewed fixture"):
        _materialize(
            archive_dir, tmp_path / "contract.json", base_manifest_path=manifest
        )


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"deadbeef  name\n",
        b"a" * 64 + b" name\n",
        (b"a" * 64) + b"  name\n" + (b"b" * 64) + b"  name\n",
        (b"a" * 64) + b"  name",
        "é".encode() + b"\n",
    ],
)
def test_checksum_parser_rejects_malformed_lists(payload: bytes) -> None:
    with pytest.raises(PayloadContractError):
        parse_checksums(payload)


def test_checksum_parser_accepts_the_producer_shape() -> None:
    records = parse_checksums(
        (b"a" * 64) + b"  desktop-install.json\n" + (b"b" * 64) + b"  SHA256SUMS\n"
    )
    assert records == {"desktop-install.json": "a" * 64, "SHA256SUMS": "b" * 64}
