"""Focused tests for the bounded desktop archive lifecycle receipt adapter."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from scripts.build_desktop_archive import (
    BuildParameters,
    build_contract_documents,
    canonical_json,
)

ROOT = Path(__file__).parents[3]

REPOSITORY = "andre-motta/tongs"
REPOSITORY_ID = "1305350434"
REPOSITORY_OWNER_ID = "30708955"
TRANSFER_REF = "refs/pull/140/merge"
TRANSFER_EVENT = "pull_request"
TRANSFER_RUN_ID = "34276954588"
TRANSFER_RUN_ATTEMPT = 1
RELEASE_VERSION = "0.5.0"
CORE_MINIMUM = "0.4.2-dev.183"
CORE_MAXIMUM_EXCLUSIVE = "0.5.0"
ARTIFACT_ID = "fedora-44-x86_64-user-archive"
CHECK_ID = "desktop-archive-lifecycle"


def _load_adapter() -> ModuleType:
    path = ROOT / "tests/integration/desktop/archive_evidence.py"
    spec = importlib.util.spec_from_file_location("focused_archive_evidence", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ADAPTER = _load_adapter()


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _contract_digest(directory: Path) -> str:
    """Recompute the trusted package digest independently of the adapter."""
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory).as_posix()
        if "__pycache__" in relative.split("/"):
            continue
        details = path.lstat()
        if stat.S_ISDIR(details.st_mode):
            continue
        content = path.read_bytes()
        digest.update(f"{relative}\0{len(content)}\0{_digest(content)}\n".encode())
    return digest.hexdigest()


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(root: Path) -> None:
    subprocess.run(
        (
            "git",
            "-C",
            str(root),
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            "archive evidence fixture",
        ),
        check=True,
    )


def _source_checkout(root: Path, electron_archive: bytes) -> Path:
    """Build a fixture subject checkout with a reviewed Electron configuration."""
    source = root / "source"
    (source / "desktop").mkdir(parents=True)
    (source / "packaging/desktop/archive").mkdir(parents=True)
    _git(source, "init", "-q")
    _git(source, "config", "user.name", "Archive Evidence Test")
    _git(source, "config", "user.email", "archive@example.invalid")
    (source / "desktop/package.json").write_bytes(
        canonical_json({"devDependencies": {"electron": "44.2.0"}})
    )
    shutil.copyfile(
        ROOT / "packaging/desktop/archive/contract.json",
        source / "packaging/desktop/archive/contract.json",
    )
    (
        source / "packaging/desktop/archive/electron-runtime-44.2.0-linux-x64.json"
    ).write_bytes(
        canonical_json(
            {
                "electron_version": "44.2.0",
                "files": [],
                "platform": "linux-x64",
                "schema_version": 1,
                "upstream_archive": {
                    "name": ADAPTER.ELECTRON_ARCHIVE_NAME,
                    "sha256": _digest(electron_archive),
                },
            }
        )
    )
    _git(source, "add", ".")
    _commit(source)
    return source


def _archive_documents(source_commit: str, epoch: int) -> dict[str, bytes]:
    """Build one real reproducible archive directory from the source contract."""
    license_inventory = canonical_json(
        {
            "schema_version": 1,
            "components": [
                {
                    "name": "fixture-runtime",
                    "version": "1.0.0",
                    "license": "MIT",
                    "license_paths": ["runtime/licenses/fixture/LICENSE"],
                }
            ],
        }
    )
    payload = {
        "runtime/tongs-desktop": (b"launcher", 0o755),
        "runtime/resources/app.asar": (b"asar fixture payload", 0o644),
        "runtime/LICENSES.json": (license_inventory, 0o644),
        "runtime/licenses/fixture/LICENSE": (b"fixture license\n", 0o644),
    }
    contract = json.loads(
        (ROOT / "packaging/desktop/archive/contract.json").read_text()
    )
    built = build_contract_documents(
        payload,
        BuildParameters(
            release_version=RELEASE_VERSION,
            core_minimum=CORE_MINIMUM,
            core_maximum_exclusive=CORE_MAXIMUM_EXCLUSIVE,
            source_commit=source_commit,
            source_date_epoch=epoch,
        ),
        contract,
    )
    install = json.loads(built.install_manifest)
    declarations = {item["path"]: item for item in install["files"]}
    asar = declarations["runtime/resources/app.asar"]
    documents = {
        built.archive_name: built.archive,
        "desktop-install.json": built.install_manifest,
        "desktop-manifest-v1.json": built.release_manifest,
        "license-inventory.json": license_inventory,
        "app-asar-inventory.json": canonical_json(
            {
                "schema_version": 1,
                "asar_sha256": asar["sha256"],
                "asar_byte_count": asar["byte_count"],
                "files": [{"path": "dist/app.js"}],
            }
        ),
        "runtime-inventory.json": canonical_json(
            {
                "schema_version": 1,
                "files": [
                    {
                        "path": item["path"],
                        "sha256": item["sha256"],
                        "byte_count": item["byte_count"],
                        "mode": "0755" if item["executable"] else "0644",
                    }
                    for item in install["files"]
                ],
            }
        ),
        "prepared-source-inventory.json": canonical_json(
            {
                "schema_version": 1,
                "source_commit": source_commit,
                "files": [{"path": "desktop/package.json"}],
                "npm_packages": [{"path": "node_modules/react"}],
            }
        ),
        "build-provenance.json": canonical_json(
            {
                "schema_version": 1,
                "candidate": "UNPUBLISHED",
                "source_commit": source_commit,
                "source_date_epoch": epoch,
                "release_version": RELEASE_VERSION,
                "compatibility": {
                    "core_minimum": CORE_MINIMUM,
                    "core_maximum_exclusive": CORE_MAXIMUM_EXCLUSIVE,
                    "rpc_api_major": contract.get("rpc_api_major", 1),
                    "plugin_api_major": contract.get("plugin_api_major", 1),
                },
                "compression": contract["compression"],
                "toolchain": contract["toolchain"],
                "electron_input": {},
                "outputs": {},
            }
        ),
    }
    return documents


def _seal(root: Path, expectations, *, rebuild_builds: bool = True) -> None:
    """Recompute the checksum lists and the unsigned transfer manifest."""
    archive_root = root / "archive"
    names = sorted(
        path.name for path in archive_root.iterdir() if path.name != "SHA256SUMS"
    )
    (archive_root / "SHA256SUMS").write_text(
        "".join(
            f"{_digest((archive_root / name).read_bytes())}  {name}\n" for name in names
        ),
        encoding="ascii",
    )
    if rebuild_builds:
        records = "".join(
            f"{_digest((archive_root / path.name).read_bytes())}  ./{path.name}\n"
            for path in sorted(archive_root.iterdir(), key=lambda item: item.name)
        )
        (root / "evidence/build-a.sha256").write_text(records, encoding="ascii")
        (root / "evidence/build-b.sha256").write_text(records, encoding="ascii")
    manifest_path = root / ADAPTER.TRANSFER_MANIFEST_NAME
    manifest_path.unlink(missing_ok=True)
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        content = path.read_bytes()
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": len(content),
                "sha256": _digest(content),
            }
        )
    files.sort(key=lambda item: item["path"])
    subjects = []
    for name in ("desktop-manifest-v1.json", expectations.archive_name):
        content = (archive_root / name).read_bytes()
        subjects.append(
            {
                "name": name,
                "path": f"archive/{name}",
                "size": len(content),
                "sha256": _digest(content),
            }
        )
    manifest_path.write_bytes(
        canonical_json(
            {
                "schema_version": 1,
                "candidate": "UNPUBLISHED",
                "source": {
                    "commit": expectations.source_commit,
                    "tree": expectations.source_tree,
                },
                "execution": {
                    "repository": expectations.transfer_repository,
                    "repository_id": expectations.transfer_repository_id,
                    "repository_owner_id": expectations.transfer_repository_owner_id,
                    "ref": expectations.transfer_ref,
                    "event": expectations.transfer_event,
                    "run_id": expectations.transfer_run_id,
                    "run_attempt": expectations.transfer_run_attempt,
                },
                "files": files,
                "subjects": subjects,
            }
        )
    )


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    """Build one reusable subject checkout and retained transfer tree."""
    base = tmp_path_factory.mktemp("archive-evidence-template")
    electron_archive = b"electron fixture archive payload"
    source = _source_checkout(base, electron_archive)
    commit = _git(source, "rev-parse", "HEAD")
    tree = _git(source, "rev-parse", "HEAD^{tree}")
    epoch = int(_git(source, "show", "-s", "--format=%ct", "HEAD"))
    source_tar = base / "subject-source.tar"
    _git(source, "archive", "--format=tar", f"--output={source_tar}", "HEAD")
    source_archive_bytes = source_tar.read_bytes()

    transfer = base / "transfer"
    (transfer / "archive").mkdir(parents=True)
    (transfer / "evidence").mkdir(parents=True)
    documents = _archive_documents(commit, epoch)
    release = json.loads(documents["desktop-manifest-v1.json"])
    artifact = release["artifacts"][0]
    install = json.loads(documents["desktop-install.json"])
    contract_bytes = (ROOT / "packaging/desktop/archive/contract.json").read_bytes()
    contract = json.loads(contract_bytes)
    electron_configuration = (
        source / "packaging/desktop/archive/electron-runtime-44.2.0-linux-x64.json"
    ).read_bytes()
    provenance = json.loads(documents["build-provenance.json"])
    provenance["electron_input"] = {
        "archive": {
            "byte_count": len(electron_archive),
            "name": ADAPTER.ELECTRON_ARCHIVE_NAME,
            "sha256": _digest(electron_archive),
        },
        "inventory_sha256": _digest(electron_configuration),
        "upstream_archive": {
            "name": ADAPTER.ELECTRON_ARCHIVE_NAME,
            "sha256": _digest(electron_archive),
        },
    }
    provenance["outputs"] = {
        "archive": {
            "byte_count": artifact["byte_count"],
            "name": artifact["name"],
            "sha256": artifact["sha256"],
        },
        "install_manifest": {
            "byte_count": len(documents["desktop-install.json"]),
            "sha256": _digest(documents["desktop-install.json"]),
        },
        "release_manifest": {
            "byte_count": len(documents["desktop-manifest-v1.json"]),
            "sha256": _digest(documents["desktop-manifest-v1.json"]),
        },
    }
    provenance["compatibility"] = {
        "core_minimum": install["compatibility"]["core_minimum"],
        "core_maximum_exclusive": install["compatibility"]["core_maximum_exclusive"],
        "rpc_api_major": install["compatibility"]["rpc_api_major"],
        "plugin_api_major": install["compatibility"]["plugin_api_major"],
    }
    documents["build-provenance.json"] = canonical_json(provenance)
    for name, document in documents.items():
        (transfer / "archive" / name).write_bytes(document)

    toolchain = contract["toolchain"]
    evidence = {
        "builder-image.json": b"{}\n",
        ADAPTER.ELECTRON_ARCHIVE_NAME: electron_archive,
        "inputs.env": (
            f"TONGS_HEAD_SHA={commit}\n"
            f"SOURCE_DATE_EPOCH={epoch}\n"
            f"RELEASE_VERSION={RELEASE_VERSION}\n"
            f"CORE_MINIMUM={CORE_MINIMUM}\n"
            f"CORE_MAXIMUM_EXCLUSIVE={CORE_MAXIMUM_EXCLUSIVE}\n"
            f"ELECTRON_ARCHIVE_SHA256={_digest(electron_archive)}\n"
        ).encode("ascii"),
        "rpm-nevra.txt": b"fixture-1.0-1.x86_64\n",
        "rpm-sha256-check.txt": b"fixture OK\n",
        "rpm-signatures.txt": b"fixture signature\n",
        "source.tar": source_archive_bytes,
        "toolchain.txt": (
            f"Python {toolchain['python']}\n"
            f"{toolchain['zlib_compile']} {toolchain['zlib_runtime']}\n"
            f"{toolchain['node']}\n"
            f"{toolchain['npm']}\n"
        ).encode("ascii"),
    }
    for name, document in evidence.items():
        (transfer / "evidence" / name).write_bytes(document)

    contract_package = ROOT / ADAPTER.ARTIFACT_CONTRACT_PACKAGE
    expectations = ADAPTER.ArchiveEvidenceExpectations(
        source_commit=commit,
        source_tree=tree,
        source_archive_sha256=_digest(source_archive_bytes),
        source_date_epoch=epoch,
        archive_name=artifact["name"],
        archive_artifact_id=ARTIFACT_ID,
        archive_sha256=artifact["sha256"],
        release_version=RELEASE_VERSION,
        release_manifest_sha256=_digest(documents["desktop-manifest-v1.json"]),
        install_manifest_sha256=_digest(documents["desktop-install.json"]),
        license_inventory_sha256=_digest(documents["license-inventory.json"]),
        electron_version="44.2.0",
        electron_configuration_sha256=_digest(electron_configuration),
        electron_archive_sha256=_digest(electron_archive),
        adapter_program_sha256=_digest((ROOT / ADAPTER.ADAPTER_PROGRAM).read_bytes()),
        transfer_validator_program_sha256=_digest(
            (ROOT / ADAPTER.TRANSFER_VALIDATOR_PROGRAM).read_bytes()
        ),
        receipt_reader_program_sha256=_digest(
            (ROOT / ADAPTER.RECEIPT_READER_PROGRAM).read_bytes()
        ),
        artifact_contract_sha256=_contract_digest(contract_package),
        transfer_repository=REPOSITORY,
        transfer_repository_id=REPOSITORY_ID,
        transfer_repository_owner_id=REPOSITORY_OWNER_ID,
        transfer_ref=TRANSFER_REF,
        transfer_event=TRANSFER_EVENT,
        transfer_run_id=TRANSFER_RUN_ID,
        transfer_run_attempt=TRANSFER_RUN_ATTEMPT,
    )
    _seal(transfer, expectations)
    return {"source": source, "transfer": transfer, "expectations": expectations}


@pytest.fixture
def case(template: dict[str, object], tmp_path: Path) -> dict[str, object]:
    """Give each test its own writable copy of the retained transfer tree."""
    transfer = tmp_path / "transfer"
    shutil.copytree(template["transfer"], transfer)
    expectations = template["expectations"]
    policy = ADAPTER.RECEIPTS.ReceiptPolicy(
        expected_commit=expectations.source_commit,
        expected_tree=expectations.source_tree,
        expected_repository=REPOSITORY,
        expected_run_id="2026090901",
        expected_attempt=1,
        expected_environment="local-archive-adapter-fedora44-x86_64",
        expected_provenance="controlled-fixture",
        expected_check_id=CHECK_ID,
        allowed_report_formats=(ADAPTER.RECEIPTS.ARTIFACT_LIFECYCLE_FORMAT,),
    )
    return {
        "source": template["source"],
        "transfer": transfer,
        "output": tmp_path / "evidence",
        "expectations": expectations,
        "policy": policy,
    }


def _produce(case: dict[str, object]):
    return ADAPTER.produce_archive_evidence(
        source_root=case["source"],
        transfer_root=case["transfer"],
        output_root=case["output"],
        receipt_policy=case["policy"],
        expectations=case["expectations"],
    )


def _consume(case: dict[str, object]):
    output = case["output"]
    return ADAPTER.consume_archive_evidence(
        evidence_root=output,
        receipt_path=output / ADAPTER.RECEIPT_PATH,
        transfer_root=case["transfer"],
        source_root=case["source"],
        receipt_policy=case["policy"],
        expectations=case["expectations"],
    )


def _rebind(output: Path, relative: str) -> None:
    receipt_path = output / ADAPTER.RECEIPT_PATH
    receipt = json.loads(receipt_path.read_bytes())
    content = (output / relative).read_bytes()
    for group in ("reports", "artifacts", "inputs"):
        for item in receipt[group]:
            if item["path"] == relative:
                item["sha256"] = _digest(content)
                if "size" in item:
                    item["size"] = len(content)
    receipt_path.write_bytes(canonical_json(receipt))


def _mutate_report(output: Path, mutate) -> None:
    report_path = output / ADAPTER.REPORT_PATH
    report = json.loads(report_path.read_bytes())
    mutate(report)
    report_path.write_bytes(canonical_json(report))
    _rebind(output, ADAPTER.REPORT_PATH)


def _cli_arguments(case: dict[str, object], command: str) -> list[str]:
    expectations = case["expectations"]
    policy = case["policy"]
    arguments = [
        command,
        "--source-root",
        str(case["source"]),
        "--transfer-root",
        str(case["transfer"]),
        "--expected-source-commit",
        expectations.source_commit,
        "--expected-source-tree",
        expectations.source_tree,
        "--expected-source-archive-sha256",
        expectations.source_archive_sha256,
        "--expected-source-date-epoch",
        str(expectations.source_date_epoch),
        "--expected-archive-name",
        expectations.archive_name,
        "--expected-archive-artifact-id",
        expectations.archive_artifact_id,
        "--expected-archive-sha256",
        expectations.archive_sha256,
        "--expected-release-version",
        expectations.release_version,
        "--expected-release-manifest-sha256",
        expectations.release_manifest_sha256,
        "--expected-install-manifest-sha256",
        expectations.install_manifest_sha256,
        "--expected-license-inventory-sha256",
        expectations.license_inventory_sha256,
        "--expected-electron-version",
        expectations.electron_version,
        "--expected-electron-configuration-sha256",
        expectations.electron_configuration_sha256,
        "--expected-electron-archive-sha256",
        expectations.electron_archive_sha256,
        "--expected-adapter-program-sha256",
        expectations.adapter_program_sha256,
        "--expected-transfer-validator-program-sha256",
        expectations.transfer_validator_program_sha256,
        "--expected-receipt-reader-program-sha256",
        expectations.receipt_reader_program_sha256,
        "--expected-artifact-contract-sha256",
        expectations.artifact_contract_sha256,
        "--expected-transfer-repository",
        expectations.transfer_repository,
        "--expected-transfer-repository-id",
        expectations.transfer_repository_id,
        "--expected-transfer-repository-owner-id",
        expectations.transfer_repository_owner_id,
        "--expected-transfer-ref",
        expectations.transfer_ref,
        "--expected-transfer-event",
        expectations.transfer_event,
        "--expected-transfer-run-id",
        expectations.transfer_run_id,
        "--expected-transfer-run-attempt",
        str(expectations.transfer_run_attempt),
        "--repository",
        policy.expected_repository,
        "--run-id",
        policy.expected_run_id,
        "--attempt",
        str(policy.expected_attempt),
        "--environment",
        policy.expected_environment,
        "--provenance",
        policy.expected_provenance,
        "--check-id",
        policy.expected_check_id,
    ]
    if command == "produce":
        arguments += ["--output-root", str(case["output"])]
    else:
        arguments += [
            "--evidence-root",
            str(case["output"]),
            "--receipt",
            str(case["output"] / ADAPTER.RECEIPT_PATH),
        ]
    return arguments


def test_producer_and_consumer_bind_the_retained_transfer(
    case: dict[str, object],
) -> None:
    produced = _produce(case)
    consumed = _consume(case)

    assert produced == consumed
    assert consumed.archive_sha256 == case["expectations"].archive_sha256
    assert consumed.archive_name == ADAPTER.ARCHIVE_NAME
    assert consumed.source_commit == case["expectations"].source_commit
    assert sorted(
        path.relative_to(case["output"]).as_posix()
        for path in case["output"].rglob("*")
        if path.is_file()
    ) == [
        ADAPTER.RECEIPT_PATH,
        ADAPTER.TRANSFER_MANIFEST_PATH,
        ADAPTER.REPORT_PATH,
    ]
    report = json.loads((case["output"] / ADAPTER.REPORT_PATH).read_bytes())
    assert [stage["name"] for stage in report["stages"]] == list(ADAPTER.STAGE_NAMES)
    assert {stage["result"] for stage in report["stages"]} == {"pass"}
    assert report["scope"]["transfer_identity"] == "unsigned"
    assert report["transfer"]["execution"]["event"] == TRANSFER_EVENT
    assert report["transfer"]["execution"]["ref"] == TRANSFER_REF
    assert report["reproducibility"]["record_count"] == 9
    receipt = json.loads((case["output"] / ADAPTER.RECEIPT_PATH).read_bytes())
    assert receipt["artifacts"] == []
    assert receipt["result"] == "success"


def test_published_evidence_does_not_copy_the_large_transfer_artifacts(
    case: dict[str, object],
) -> None:
    _produce(case)

    published = sorted(
        path.name for path in case["output"].rglob("*") if path.is_file()
    )
    assert ADAPTER.ARCHIVE_NAME not in published
    assert "source.tar" not in published
    assert ADAPTER.ELECTRON_ARCHIVE_NAME not in published
    assert (case["transfer"] / "archive" / ADAPTER.ARCHIVE_NAME).is_file()


def test_wrong_subject_source_is_rejected_before_publication(
    case: dict[str, object],
) -> None:
    case["expectations"] = replace(case["expectations"], source_tree="f" * 40)
    case["policy"] = replace(case["policy"], expected_tree="f" * 40)

    with pytest.raises(ADAPTER.ArchiveEvidenceError, match="subject source identity"):
        _produce(case)
    assert not case["output"].exists()


def test_unclean_subject_checkout_cannot_publish(
    case: dict[str, object], tmp_path: Path
) -> None:
    dirty = tmp_path / "dirty-source"
    shutil.copytree(case["source"], dirty)
    (dirty / "desktop/package.json").write_bytes(b"{}\n")
    case["source"] = dirty

    with pytest.raises(ADAPTER.ArchiveEvidenceError, match="must be clean"):
        _produce(case)
    assert not case["output"].exists()


@pytest.mark.parametrize(
    "field",
    [
        "adapter_program_sha256",
        "transfer_validator_program_sha256",
        "receipt_reader_program_sha256",
        "artifact_contract_sha256",
    ],
)
def test_wrong_trusted_tool_identity_is_rejected(
    case: dict[str, object], field: str
) -> None:
    case["expectations"] = replace(case["expectations"], **{field: "f" * 64})

    with pytest.raises(ADAPTER.ArchiveEvidenceError, match="trusted archive tool"):
        _produce(case)
    assert not case["output"].exists()


def test_substituted_trusted_module_origin_is_rejected(
    case: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ADAPTER, "RECEIPTS", SimpleNamespace(__file__="/tmp/not-the-reader.py")
    )

    with pytest.raises(ADAPTER.ArchiveEvidenceError, match="module origin"):
        ADAPTER._require_tool_identity(case["expectations"])


def test_pull_request_transfer_cannot_be_relabelled_as_a_push(
    case: dict[str, object],
) -> None:
    case["expectations"] = replace(case["expectations"], transfer_event="push")

    with pytest.raises(
        ADAPTER.ArchiveEvidenceError, match="transfer execution identity is invalid"
    ):
        _produce(case)
    assert not case["output"].exists()


@pytest.mark.parametrize(
    "field, value",
    [
        ("transfer_run_id", "34276954589"),
        ("transfer_run_attempt", 2),
        ("transfer_ref", "refs/pull/141/merge"),
        ("transfer_repository_id", "1"),
        ("transfer_repository_owner_id", "1"),
    ],
)
def test_wrong_upstream_execution_identity_is_rejected(
    case: dict[str, object], field: str, value: object
) -> None:
    case["expectations"] = replace(case["expectations"], **{field: value})

    with pytest.raises(
        ADAPTER.ArchiveEvidenceError, match="retained transfer validation failed"
    ):
        _produce(case)
    assert not case["output"].exists()


def test_omitted_transfer_entry_is_rejected_before_hashing(
    case: dict[str, object],
) -> None:
    (case["transfer"] / "evidence/rpm-nevra.txt").unlink()

    with pytest.raises(ADAPTER.ArchiveEvidenceError, match="path set is incomplete"):
        _produce(case)
    assert not case["output"].exists()


def test_extra_transfer_entry_is_rejected_before_hashing(
    case: dict[str, object],
) -> None:
    (case["transfer"] / "evidence/extra-note.txt").write_bytes(b"extra\n")

    with pytest.raises(ADAPTER.ArchiveEvidenceError, match="unexpected file"):
        _produce(case)
    assert not case["output"].exists()


def test_renamed_transfer_entry_is_rejected_before_hashing(
    case: dict[str, object],
) -> None:
    evidence = case["transfer"] / "evidence"
    (evidence / "rpm-nevra.txt").rename(evidence / "rpm-nevra.renamed.txt")

    with pytest.raises(ADAPTER.ArchiveEvidenceError, match="unexpected file"):
        _produce(case)
    assert not case["output"].exists()


def test_unexpected_transfer_directory_is_rejected(case: dict[str, object]) -> None:
    (case["transfer"] / "extra").mkdir()

    with pytest.raises(ADAPTER.ArchiveEvidenceError, match="unexpected directory"):
        _produce(case)
    assert not case["output"].exists()


def test_symlinked_transfer_entry_is_rejected(case: dict[str, object]) -> None:
    target = case["transfer"] / "evidence/rpm-nevra.txt"
    replacement = case["transfer"] / "evidence/rpm-nevra.link"
    replacement.symlink_to(target)
    target.unlink()
    replacement.rename(target)

    with pytest.raises(ADAPTER.ArchiveEvidenceError, match="nonregular file"):
        _produce(case)
    assert not case["output"].exists()


def test_oversized_transfer_entry_is_rejected_before_hashing(
    case: dict[str, object],
) -> None:
    (case["transfer"] / "evidence/toolchain.txt").write_bytes(
        b"x" * (ADAPTER.MAX_TEXT_BYTES + 1)
    )

    with pytest.raises(ADAPTER.ArchiveEvidenceError, match="per-class byte bound"):
        _produce(case)
    assert not case["output"].exists()


def test_hash_changed_transfer_entry_is_rejected(case: dict[str, object]) -> None:
    path = case["transfer"] / "evidence/rpm-nevra.txt"
    original = path.read_bytes()
    path.write_bytes(bytes([original[0] ^ 1]) + original[1:])

    with pytest.raises(
        ADAPTER.ArchiveEvidenceError, match="retained transfer validation failed"
    ):
        _produce(case)
    assert not case["output"].exists()


def test_path_unsafe_manifest_declaration_is_rejected(case: dict[str, object]) -> None:
    manifest_path = case["transfer"] / ADAPTER.TRANSFER_MANIFEST_NAME
    manifest = json.loads(manifest_path.read_bytes())
    manifest["files"][0]["path"] = "../escaped.txt"
    manifest_path.write_bytes(canonical_json(manifest))

    with pytest.raises(ADAPTER.ArchiveEvidenceError, match="safe relative path"):
        _produce(case)
    assert not case["output"].exists()


def test_oversized_manifest_declaration_is_rejected(case: dict[str, object]) -> None:
    manifest_path = case["transfer"] / ADAPTER.TRANSFER_MANIFEST_NAME
    manifest = json.loads(manifest_path.read_bytes())
    for item in manifest["files"]:
        if item["path"] == "evidence/toolchain.txt":
            item["size"] = ADAPTER.MAX_TEXT_BYTES + 1
    manifest_path.write_bytes(canonical_json(manifest))

    with pytest.raises(ADAPTER.ArchiveEvidenceError, match="per-class byte bound"):
        _produce(case)
    assert not case["output"].exists()


def test_malformed_transfer_manifest_is_rejected(case: dict[str, object]) -> None:
    (case["transfer"] / ADAPTER.TRANSFER_MANIFEST_NAME).write_bytes(b"{not json")

    with pytest.raises(ADAPTER.ArchiveEvidenceError, match="not valid UTF-8 JSON"):
        _produce(case)
    assert not case["output"].exists()


def test_unequal_build_checksum_lists_cannot_publish(case: dict[str, object]) -> None:
    path = case["transfer"] / "evidence/build-b.sha256"
    path.write_bytes(path.read_bytes().replace(b"./SHA256SUMS", b"./sha256sums"))
    _seal(case["transfer"], case["expectations"], rebuild_builds=False)

    with pytest.raises(
        ADAPTER.ArchiveEvidenceError, match="retained transfer validation failed"
    ):
        _produce(case)
    assert not case["output"].exists()


def test_reproducibility_stage_requires_byte_identical_lists(tmp_path: Path) -> None:
    root = tmp_path / "isolated"
    (root / "evidence").mkdir(parents=True)
    first = f"{'a' * 64}  ./SHA256SUMS\n".encode("ascii")
    second = f"{'b' * 64}  ./SHA256SUMS\n".encode("ascii")
    observed = {}
    for name, document in (("build-a.sha256", first), ("build-b.sha256", second)):
        (root / "evidence" / name).write_bytes(document)
        observed[f"evidence/{name}"] = {
            "size": len(document),
            "sha256": _digest(document),
        }

    with pytest.raises(ADAPTER.ArchiveEvidenceError, match="not byte identical"):
        ADAPTER._reproducibility_state(root, observed)


def test_equal_build_lists_unrelated_to_the_final_output_cannot_publish(
    case: dict[str, object],
) -> None:
    records = (case["transfer"] / "evidence/build-a.sha256").read_text()
    forged = "\n".join(
        f"{'c' * 64}  {line.split('  ', 1)[1]}" for line in records.splitlines()
    )
    forged += "\n"
    for name in ("build-a.sha256", "build-b.sha256"):
        (case["transfer"] / "evidence" / name).write_text(forged, encoding="ascii")
    _seal(case["transfer"], case["expectations"], rebuild_builds=False)

    with pytest.raises(
        ADAPTER.ArchiveEvidenceError, match="do not equal the validated archive"
    ):
        _produce(case)
    assert not case["output"].exists()


def test_build_lists_omitting_the_checksum_file_cannot_publish(
    case: dict[str, object],
) -> None:
    records = (case["transfer"] / "evidence/build-a.sha256").read_text()
    trimmed = "".join(
        f"{line}\n" for line in records.splitlines() if "./SHA256SUMS" not in line
    )
    for name in ("build-a.sha256", "build-b.sha256"):
        (case["transfer"] / "evidence" / name).write_text(trimmed, encoding="ascii")
    _seal(case["transfer"], case["expectations"], rebuild_builds=False)

    with pytest.raises(ADAPTER.ArchiveEvidenceError, match="omit the archive checksum"):
        _produce(case)
    assert not case["output"].exists()


def test_malformed_build_checksum_record_cannot_publish(
    case: dict[str, object],
) -> None:
    for name in ("build-a.sha256", "build-b.sha256"):
        (case["transfer"] / "evidence" / name).write_text(
            "not a checksum record\n", encoding="ascii"
        )
    _seal(case["transfer"], case["expectations"], rebuild_builds=False)

    with pytest.raises(ADAPTER.ArchiveEvidenceError, match="invalid record"):
        _produce(case)
    assert not case["output"].exists()


@pytest.mark.parametrize(
    "field, value, message",
    [
        ("source_date_epoch", 1, "build provenance is not bound"),
        ("release_version", "9.9.9", "build provenance is not bound"),
        ("candidate", "PUBLISHED", "retained transfer validation failed"),
    ],
)
def test_stale_provenance_metadata_cannot_publish(
    case: dict[str, object], field: str, value: object, message: str
) -> None:
    path = case["transfer"] / "archive/build-provenance.json"
    provenance = json.loads(path.read_bytes())
    provenance[field] = value
    path.write_bytes(canonical_json(provenance))
    _seal(case["transfer"], case["expectations"])

    with pytest.raises(ADAPTER.ArchiveEvidenceError, match=message):
        _produce(case)
    assert not case["output"].exists()


def test_provenance_toolchain_outside_the_source_definition_cannot_publish(
    case: dict[str, object],
) -> None:
    path = case["transfer"] / "archive/build-provenance.json"
    provenance = json.loads(path.read_bytes())
    provenance["toolchain"]["node"] = "v0.0.1"
    path.write_bytes(canonical_json(provenance))
    _seal(case["transfer"], case["expectations"])

    with pytest.raises(ADAPTER.ArchiveEvidenceError, match="toolchain differs"):
        _produce(case)
    assert not case["output"].exists()


def test_provenance_outputs_unrelated_to_the_archive_cannot_publish(
    case: dict[str, object],
) -> None:
    path = case["transfer"] / "archive/build-provenance.json"
    provenance = json.loads(path.read_bytes())
    provenance["outputs"]["archive"]["sha256"] = "d" * 64
    path.write_bytes(canonical_json(provenance))
    _seal(case["transfer"], case["expectations"])

    with pytest.raises(ADAPTER.ArchiveEvidenceError, match="provenance outputs differ"):
        _produce(case)
    assert not case["output"].exists()


def test_stale_producer_inputs_cannot_publish(case: dict[str, object]) -> None:
    path = case["transfer"] / "evidence/inputs.env"
    path.write_bytes(path.read_bytes().replace(b"RELEASE_VERSION=0.5.0", b"X=1"))
    _seal(case["transfer"], case["expectations"])

    with pytest.raises(ADAPTER.ArchiveEvidenceError, match="producer inputs"):
        _produce(case)
    assert not case["output"].exists()


def test_recorded_toolchain_outside_the_source_definition_cannot_publish(
    case: dict[str, object],
) -> None:
    path = case["transfer"] / "evidence/toolchain.txt"
    path.write_bytes(b"Python 0.0.0\nz z\nv0.0.0\n0.0.0\n")
    _seal(case["transfer"], case["expectations"])

    with pytest.raises(ADAPTER.ArchiveEvidenceError, match="recorded toolchain"):
        _produce(case)
    assert not case["output"].exists()


def test_runtime_inventory_disagreement_cannot_publish(
    case: dict[str, object],
) -> None:
    path = case["transfer"] / "archive/runtime-inventory.json"
    inventory = json.loads(path.read_bytes())
    inventory["files"][0]["sha256"] = "e" * 64
    path.write_bytes(canonical_json(inventory))
    _seal(case["transfer"], case["expectations"])

    with pytest.raises(ADAPTER.ArchiveEvidenceError, match="runtime inventory"):
        _produce(case)
    assert not case["output"].exists()


def test_application_archive_inventory_disagreement_cannot_publish(
    case: dict[str, object],
) -> None:
    path = case["transfer"] / "archive/app-asar-inventory.json"
    inventory = json.loads(path.read_bytes())
    inventory["asar_sha256"] = "e" * 64
    path.write_bytes(canonical_json(inventory))
    _seal(case["transfer"], case["expectations"])

    with pytest.raises(
        ADAPTER.ArchiveEvidenceError, match="application archive inventory"
    ):
        _produce(case)
    assert not case["output"].exists()


def test_reviewed_electron_configuration_mismatch_cannot_publish(
    case: dict[str, object],
) -> None:
    case["expectations"] = replace(
        case["expectations"], electron_configuration_sha256="b" * 64
    )

    with pytest.raises(
        ADAPTER.ArchiveEvidenceError, match="reviewed Electron configuration"
    ):
        _produce(case)
    assert not case["output"].exists()


def test_existing_output_root_cannot_be_overwritten(case: dict[str, object]) -> None:
    case["output"].mkdir(parents=True)

    with pytest.raises(ADAPTER.ArchiveEvidenceError, match="must not already exist"):
        _produce(case)
    assert not (case["output"] / ADAPTER.RECEIPT_PATH).exists()


@pytest.mark.parametrize("mutation", ["missing", "failed", "extra"])
def test_consumer_rejects_incomplete_or_failed_stages(
    case: dict[str, object], mutation: str
) -> None:
    _produce(case)

    def mutate(report: dict[str, object]) -> None:
        if mutation == "missing":
            report["stages"].pop()
        elif mutation == "failed":
            report["stages"][3]["result"] = "fail"
        else:
            report["stages"].append({"name": "unconfigured", "result": "pass"})

    _mutate_report(case["output"], mutate)

    with pytest.raises(ADAPTER.ArchiveEvidenceError, match="lifecycle stages"):
        _consume(case)


def test_consumer_rejects_a_failed_report_result(case: dict[str, object]) -> None:
    _produce(case)
    _mutate_report(case["output"], lambda report: report.update({"result": "fail"}))

    with pytest.raises(ADAPTER.ArchiveEvidenceError, match="identity or result"):
        _consume(case)


def test_consumer_rejects_a_forged_archive_binding(case: dict[str, object]) -> None:
    _produce(case)

    def mutate(report: dict[str, object]) -> None:
        report["archive"]["sha256"] = "d" * 64

    _mutate_report(case["output"], mutate)

    with pytest.raises(ADAPTER.ArchiveEvidenceError, match="archive binding is stale"):
        _consume(case)


def test_consumer_rejects_a_forged_reproducibility_binding(
    case: dict[str, object],
) -> None:
    _produce(case)

    def mutate(report: dict[str, object]) -> None:
        report["reproducibility"]["records"][0]["sha256"] = "d" * 64

    _mutate_report(case["output"], mutate)

    with pytest.raises(
        ADAPTER.ArchiveEvidenceError, match="reproducibility binding is stale"
    ):
        _consume(case)


def test_consumer_rejects_a_forged_source_binding(case: dict[str, object]) -> None:
    _produce(case)

    def mutate(report: dict[str, object]) -> None:
        report["source"]["date_epoch"] = 1

    _mutate_report(case["output"], mutate)

    with pytest.raises(ADAPTER.ArchiveEvidenceError, match="source identity is stale"):
        _consume(case)


@pytest.mark.parametrize(
    "relative", [ADAPTER.REPORT_PATH, ADAPTER.TRANSFER_MANIFEST_PATH]
)
def test_consumer_rejects_replaced_bound_files(
    case: dict[str, object], relative: str
) -> None:
    _produce(case)
    target = case["output"] / relative
    replacement = target.with_suffix(".replacement")
    replacement.write_bytes(b"x" * len(target.read_bytes()))
    os.replace(replacement, target)

    with pytest.raises(ADAPTER.ArchiveEvidenceError, match="receipt binding"):
        _consume(case)


def test_consumer_rejects_a_missing_bound_report(case: dict[str, object]) -> None:
    _produce(case)
    (case["output"] / ADAPTER.REPORT_PATH).unlink()

    with pytest.raises(ADAPTER.ArchiveEvidenceError, match="receipt binding"):
        _consume(case)


def test_consumer_rejects_a_receipt_that_records_failure(
    case: dict[str, object],
) -> None:
    _produce(case)
    receipt_path = case["output"] / ADAPTER.RECEIPT_PATH
    receipt = json.loads(receipt_path.read_bytes())
    receipt["result"] = "failure"
    receipt_path.write_bytes(canonical_json(receipt))

    with pytest.raises(ADAPTER.ArchiveEvidenceError, match="does not record success"):
        _consume(case)


def test_consumer_rejects_a_receipt_that_stages_a_copied_artifact(
    case: dict[str, object],
) -> None:
    _produce(case)
    receipt_path = case["output"] / ADAPTER.RECEIPT_PATH
    receipt = json.loads(receipt_path.read_bytes())
    copied = case["output"] / "artifacts/copied.bin"
    copied.parent.mkdir(parents=True)
    copied.write_bytes(b"copied artifact")
    receipt["artifacts"] = [
        {
            "path": "artifacts/copied.bin",
            "size": len(b"copied artifact"),
            "sha256": _digest(b"copied artifact"),
            "role": "desktop-user-archive",
        }
    ]
    receipt_path.write_bytes(canonical_json(receipt))

    with pytest.raises(
        ADAPTER.ArchiveEvidenceError, match="must not stage a copied large artifact"
    ):
        _consume(case)


def test_consumer_rejects_a_changed_retained_transfer(
    case: dict[str, object],
) -> None:
    _produce(case)
    manifest = case["transfer"] / ADAPTER.TRANSFER_MANIFEST_NAME
    document = json.loads(manifest.read_bytes())
    document["execution"]["run_id"] = "34276954589"
    manifest.write_bytes(canonical_json(document))

    with pytest.raises(
        ADAPTER.ArchiveEvidenceError, match="differs from its receipt-bound bytes"
    ):
        _consume(case)


def test_consumer_rejects_a_wrong_receipt_policy_check(
    case: dict[str, object],
) -> None:
    _produce(case)
    case["policy"] = replace(case["policy"], expected_check_id="desktop-other-check")

    with pytest.raises(
        ADAPTER.ArchiveEvidenceError, match="receipt binding is invalid"
    ):
        _consume(case)


def test_receipt_policy_must_allow_only_the_lifecycle_format(
    case: dict[str, object],
) -> None:
    case["policy"] = replace(
        case["policy"],
        allowed_report_formats=(
            ADAPTER.RECEIPTS.ARTIFACT_LIFECYCLE_FORMAT,
            ADAPTER.RECEIPTS.PYTEST_JUNIT_FORMAT,
        ),
    )

    with pytest.raises(
        ADAPTER.ArchiveEvidenceError, match="only artifact-lifecycle-v1"
    ):
        _produce(case)
    assert not case["output"].exists()


def test_cli_produce_and_consume_succeed(case: dict[str, object]) -> None:
    assert ADAPTER.main(_cli_arguments(case, "produce")) == 0
    assert ADAPTER.main(_cli_arguments(case, "consume")) == 0
    assert (case["output"] / ADAPTER.RECEIPT_PATH).is_file()


def test_cli_failure_is_nonzero_and_leaves_no_published_receipt(
    case: dict[str, object],
) -> None:
    (case["transfer"] / "evidence/inputs.env").write_bytes(b"TONGS_HEAD_SHA=x\n")
    _seal(case["transfer"], case["expectations"])

    assert ADAPTER.main(_cli_arguments(case, "produce")) == 1
    assert not case["output"].exists()


def test_cli_consume_failure_is_nonzero(case: dict[str, object]) -> None:
    assert ADAPTER.main(_cli_arguments(case, "produce")) == 0
    (case["output"] / ADAPTER.REPORT_PATH).unlink()

    assert ADAPTER.main(_cli_arguments(case, "consume")) == 1
