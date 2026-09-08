"""Focused tests for the bounded desktop SBOM receipt adapter."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[3]


def _load_adapter() -> ModuleType:
    path = ROOT / "tests/integration/desktop/sbom_evidence.py"
    spec = importlib.util.spec_from_file_location("focused_sbom_evidence", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ADAPTER = _load_adapter()


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _source_checkout(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "user.name", "SBOM Test")
    _git(source, "config", "user.email", "sbom@example.invalid")
    (source / "desktop").mkdir()
    (source / "desktop/package.json").write_bytes(
        _canonical({"devDependencies": {"electron": "44.2.0"}})
    )
    configuration = {
        "schema_version": 1,
        "electron_version": "44.2.0",
        "platform": "linux-x64",
        "upstream_archive": {
            "name": "electron-v44.2.0-linux-x64.zip",
            "sha256": "e" * 64,
        },
        "files": [],
    }
    configuration_path = (
        source / "packaging/desktop/archive/electron-runtime-44.2.0-linux-x64.json"
    )
    configuration_path.parent.mkdir(parents=True)
    configuration_path.write_bytes(_canonical(configuration))
    _git(source, "add", ".")
    subprocess.run(
        (
            "git",
            "-C",
            str(source),
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            "fixture",
        ),
        check=True,
    )
    return source


def _spdx(version: str = "1.0.0") -> bytes:
    return _canonical(
        {
            "SPDXID": "SPDXRef-DOCUMENT",
            "creationInfo": {
                "created": "2026-09-08T00:00:00Z",
                "creators": [f"Tool: tongs-desktop-sbom-{version}"],
            },
            "dataLicense": "CC0-1.0",
            "documentDescribes": ["SPDXRef-Package-Tongs-Desktop-Archive"],
            "documentNamespace": "https://spdx.tongs.tools/test",
            "name": "Tongs Desktop test archive SBOM",
            "packages": [{"SPDXID": "SPDXRef-Package-Tongs-Desktop-Archive"}],
            "relationships": [
                {
                    "spdxElementId": "SPDXRef-DOCUMENT",
                    "relationshipType": "DESCRIBES",
                    "relatedSpdxElement": "SPDXRef-Package-Tongs-Desktop-Archive",
                }
            ],
            "spdxVersion": "SPDX-2.3",
        }
    )


def _case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    source = _source_checkout(tmp_path)
    observed = ADAPTER._derive_source_identity(source)
    archive_receipt = _canonical({"schema_version": 1, "result": "success"})
    transfer_root = tmp_path / "transfer"
    transfer_root.mkdir()
    transfer = _canonical(
        {
            "schema_version": 1,
            "candidate": "UNPUBLISHED",
            "source": {
                "commit": observed["commit"],
                "tree": observed["tree"],
            },
            "execution": {
                "repository": "andre-motta/tongs",
                "repository_id": "1305350434",
                "repository_owner_id": "30708955",
                "ref": "refs/heads/feat/desktop-app",
                "event": "push",
                "run_id": "99999",
                "run_attempt": 2,
            },
            "files": [],
            "subjects": [],
        }
    )
    (transfer_root / "candidate-attestation-transfer-v1.json").write_bytes(transfer)
    tool_commit = _git(ROOT, "rev-parse", "HEAD")
    tool_tree = _git(ROOT, "rev-parse", "HEAD^{tree}")
    electron_configuration = (
        source / "packaging/desktop/archive/electron-runtime-44.2.0-linux-x64.json"
    ).read_bytes()
    expectations = ADAPTER.EvidenceExpectations(
        source_commit=observed["commit"],
        source_tree=observed["tree"],
        source_archive_sha256=observed["archive_sha256"],
        source_date_epoch=observed["date_epoch"],
        archive_sha256="a" * 64,
        electron_archive_sha256="e" * 64,
        generator_version="1.0.0",
        tool_commit=tool_commit,
        tool_tree=tool_tree,
        adapter_program_sha256=_digest((ROOT / ADAPTER.ADAPTER_PROGRAM).read_bytes()),
        generator_program_sha256=_digest(
            (ROOT / ADAPTER.GENERATOR_PROGRAM).read_bytes()
        ),
        receipt_reader_program_sha256=_digest(
            (ROOT / ADAPTER.RECEIPT_READER_PROGRAM).read_bytes()
        ),
        schema_sha256=_digest(ADAPTER.DEFAULT_SCHEMA.read_bytes()),
        electron_configuration_sha256=_digest(electron_configuration),
        transfer_repository="andre-motta/tongs",
        transfer_repository_id="1305350434",
        transfer_repository_owner_id="30708955",
        transfer_ref="refs/heads/feat/desktop-app",
        transfer_event="push",
        transfer_run_id="99999",
        transfer_run_attempt=2,
        archive_receipt_sha256=_digest(archive_receipt),
    )
    policy = ADAPTER.RECEIPTS.ReceiptPolicy(
        expected_commit=expectations.source_commit,
        expected_tree=expectations.source_tree,
        expected_repository="andre-motta/tongs",
        expected_run_id="12345",
        expected_attempt=1,
        expected_environment="ubuntu-24.04",
        expected_provenance="hosted",
        expected_check_id="desktop-archive-sbom",
        allowed_report_formats=(ADAPTER.RECEIPTS.ARTIFACT_LIFECYCLE_FORMAT,),
    )
    monkeypatch.setattr(
        ADAPTER.SBOM,
        "build_desktop_sbom",
        lambda *_args, **_kwargs: _spdx(),
    )
    return {
        "source": source,
        "input": transfer_root,
        "output": tmp_path / "output",
        "archive_receipt": archive_receipt,
        "expectations": expectations,
        "policy": policy,
    }


def _produce(case: dict[str, object]):
    return ADAPTER.produce_sbom_evidence(
        source_root=case["source"],
        input_root=case["input"],
        output_root=case["output"],
        archive_receipt_bytes=case["archive_receipt"],
        receipt_policy=case["policy"],
        expectations=case["expectations"],
    )


def _consume(case: dict[str, object]):
    output = case["output"]
    return ADAPTER.consume_sbom_evidence(
        evidence_root=output,
        receipt_path=output / ADAPTER.RECEIPT_PATH,
        receipt_policy=case["policy"],
        expectations=case["expectations"],
        input_root=case["input"],
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
    receipt_path.write_bytes(_canonical(receipt))


def test_producer_and_consumer_bind_deterministic_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _case(tmp_path, monkeypatch)
    produced = _produce(case)
    consumed = _consume(case)

    assert produced == consumed
    assert consumed.sbom_sha256 == _digest(_spdx())
    assert (
        consumed.archive_receipt_sha256 == case["expectations"].archive_receipt_sha256
    )
    assert sorted(
        path.relative_to(case["output"]).as_posix()
        for path in case["output"].rglob("*")
        if path.is_file()
    ) == [
        ADAPTER.SBOM_PATH,
        ADAPTER.ARCHIVE_RECEIPT_PATH,
        ADAPTER.TRANSFER_MANIFEST_PATH,
        ADAPTER.REPORT_PATH,
        ADAPTER.RECEIPT_PATH,
    ]


def test_wrong_caller_source_is_rejected_before_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _case(tmp_path, monkeypatch)
    case["expectations"] = replace(case["expectations"], source_tree="f" * 40)
    case["policy"] = replace(case["policy"], expected_tree="f" * 40)

    with pytest.raises(ADAPTER.SbomEvidenceError, match="source checkout identity"):
        _produce(case)
    assert not case["output"].exists()


def test_changed_transfer_source_cannot_publish_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _case(tmp_path, monkeypatch)
    manifest = case["input"] / "candidate-attestation-transfer-v1.json"
    document = json.loads(manifest.read_bytes())
    document["source"] = {"commit": "f" * 40, "tree": "f" * 40}
    manifest.write_bytes(_canonical(document))

    with pytest.raises(ADAPTER.SbomEvidenceError, match="candidate transfer source"):
        _produce(case)
    assert not case["output"].exists()


def test_wrong_upstream_run_cannot_be_wrapped_as_current_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _case(tmp_path, monkeypatch)
    case["expectations"] = replace(case["expectations"], transfer_run_id="88888")

    with pytest.raises(ADAPTER.SbomEvidenceError, match="transfer execution"):
        _produce(case)
    assert not case["output"].exists()


def test_transfer_replacement_during_generation_cannot_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _case(tmp_path, monkeypatch)
    manifest = case["input"] / "candidate-attestation-transfer-v1.json"
    calls = 0

    def generate(*_args: object) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 2:
            document = json.loads(manifest.read_bytes())
            document["execution"]["run_id"] = "77777"
            manifest.write_bytes(_canonical(document))
        return _spdx()

    monkeypatch.setattr(ADAPTER.SBOM, "build_desktop_sbom", generate)

    with pytest.raises(ADAPTER.SbomEvidenceError, match="changed during SBOM"):
        _produce(case)
    assert not case["output"].exists()


def test_wrong_trusted_adapter_hash_is_rejected_before_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _case(tmp_path, monkeypatch)
    case["expectations"] = replace(
        case["expectations"], adapter_program_sha256="f" * 64
    )

    with pytest.raises(ADAPTER.SbomEvidenceError, match="trusted SBOM tool"):
        _produce(case)
    assert not case["output"].exists()


def test_generator_failure_cannot_publish_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _case(tmp_path, monkeypatch)
    monkeypatch.setattr(
        ADAPTER.SBOM,
        "build_desktop_sbom",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ADAPTER.SBOM.SbomBuildError("fixture failure")
        ),
    )

    with pytest.raises(ADAPTER.SBOM.SbomBuildError, match="fixture failure"):
        _produce(case)
    assert not case["output"].exists()


def test_unequal_generator_outputs_cannot_publish_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _case(tmp_path, monkeypatch)
    outputs = iter((_spdx(), _spdx() + b" "))
    monkeypatch.setattr(
        ADAPTER.SBOM, "build_desktop_sbom", lambda *_args: next(outputs)
    )

    with pytest.raises(ADAPTER.SbomEvidenceError, match="not byte deterministic"):
        _produce(case)
    assert not case["output"].exists()


@pytest.mark.parametrize("mutation", ["missing", "failed", "extra"])
def test_consumer_rejects_incomplete_or_failed_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    case = _case(tmp_path, monkeypatch)
    _produce(case)
    report_path = case["output"] / ADAPTER.REPORT_PATH
    report = json.loads(report_path.read_bytes())
    if mutation == "missing":
        report["stages"].pop()
    elif mutation == "failed":
        report["stages"][2]["result"] = "fail"
    else:
        report["stages"].append({"name": "unconfigured", "result": "pass"})
    report_path.write_bytes(_canonical(report))
    _rebind(case["output"], ADAPTER.REPORT_PATH)

    with pytest.raises(ADAPTER.SbomEvidenceError, match="lifecycle stages"):
        _consume(case)


@pytest.mark.parametrize("relative", [ADAPTER.REPORT_PATH, ADAPTER.SBOM_PATH])
def test_consumer_rejects_replaced_bound_semantic_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
) -> None:
    case = _case(tmp_path, monkeypatch)
    _produce(case)
    target = case["output"] / relative
    replacement = target.with_suffix(".replacement")
    value = target.read_bytes()
    replacement.write_bytes(b"x" * len(value))
    os.replace(replacement, target)

    with pytest.raises(ADAPTER.SbomEvidenceError, match="receipt binding"):
        _consume(case)


def test_consumer_rejects_malformed_rebound_spdx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _case(tmp_path, monkeypatch)
    _produce(case)
    (case["output"] / ADAPTER.SBOM_PATH).write_bytes(b'{"spdxVersion":"SPDX-2.3"}\n')
    _rebind(case["output"], ADAPTER.SBOM_PATH)

    with pytest.raises(ADAPTER.SbomEvidenceError, match="creation info"):
        _consume(case)


def test_consumer_rejects_semantically_rebound_spdx_and_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _case(tmp_path, monkeypatch)
    _produce(case)
    output = case["output"]
    sbom_path = output / ADAPTER.SBOM_PATH
    document = json.loads(sbom_path.read_bytes())
    document["documentNamespace"] = "https://spdx.tongs.tools/rebound"
    document["packages"].append({"SPDXID": "SPDXRef-Package-Forged"})
    rebound_sbom = _canonical(document)
    sbom_path.write_bytes(rebound_sbom)
    _rebind(output, ADAPTER.SBOM_PATH)
    report_path = output / ADAPTER.REPORT_PATH
    report = json.loads(report_path.read_bytes())
    report["output"] = {
        "path": ADAPTER.SBOM_PATH,
        "size": len(rebound_sbom),
        "sha256": _digest(rebound_sbom),
        **ADAPTER._spdx_summary(document, case["expectations"]),
    }
    report_path.write_bytes(_canonical(report))
    _rebind(output, ADAPTER.REPORT_PATH)

    with pytest.raises(ADAPTER.SbomEvidenceError, match="semantic reproduction"):
        _consume(case)


def test_consumer_rejects_transfer_change_during_semantic_reproduction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _case(tmp_path, monkeypatch)
    _produce(case)
    manifest = case["input"] / "candidate-attestation-transfer-v1.json"

    def reproduce(*_args: object) -> bytes:
        document = json.loads(manifest.read_bytes())
        document["execution"]["run_id"] = "77777"
        manifest.write_bytes(_canonical(document))
        return _spdx()

    monkeypatch.setattr(ADAPTER.SBOM, "build_desktop_sbom", reproduce)

    with pytest.raises(ADAPTER.SbomEvidenceError, match="changed during semantic"):
        _consume(case)


def test_consumer_rejects_missing_required_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _case(tmp_path, monkeypatch)
    _produce(case)
    (case["output"] / ADAPTER.SBOM_PATH).unlink()

    with pytest.raises(ADAPTER.SbomEvidenceError, match="receipt binding"):
        _consume(case)


def test_consumer_rejects_wrong_archive_receipt_expectation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _case(tmp_path, monkeypatch)
    _produce(case)
    case["expectations"] = replace(
        case["expectations"], archive_receipt_sha256="d" * 64
    )

    with pytest.raises(ADAPTER.SbomEvidenceError, match="bound archive receipt"):
        _consume(case)


def test_consumer_rejects_structural_success_with_failed_report_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _case(tmp_path, monkeypatch)
    _produce(case)
    report_path = case["output"] / ADAPTER.REPORT_PATH
    report = json.loads(report_path.read_bytes())
    report["result"] = "fail"
    report_path.write_bytes(_canonical(report))
    _rebind(case["output"], ADAPTER.REPORT_PATH)

    with pytest.raises(ADAPTER.SbomEvidenceError, match="identity or result"):
        _consume(case)


def test_cli_failure_is_nonzero_and_leaves_no_final_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _case(tmp_path, monkeypatch)
    arguments = [
        "produce",
        "--source-root",
        str(case["source"]),
        "--input-root",
        str(case["input"]),
        "--output-root",
        str(case["output"]),
        "--archive-receipt",
        str(tmp_path / "missing-receipt.json"),
        "--expected-source-commit",
        case["expectations"].source_commit,
        "--expected-source-tree",
        case["expectations"].source_tree,
        "--expected-source-archive-sha256",
        case["expectations"].source_archive_sha256,
        "--expected-source-date-epoch",
        str(case["expectations"].source_date_epoch),
        "--expected-archive-sha256",
        case["expectations"].archive_sha256,
        "--expected-electron-archive-sha256",
        case["expectations"].electron_archive_sha256,
        "--expected-archive-receipt-sha256",
        case["expectations"].archive_receipt_sha256,
        "--generator-version",
        case["expectations"].generator_version,
        "--expected-tool-commit",
        case["expectations"].tool_commit,
        "--expected-tool-tree",
        case["expectations"].tool_tree,
        "--expected-adapter-program-sha256",
        case["expectations"].adapter_program_sha256,
        "--expected-generator-program-sha256",
        case["expectations"].generator_program_sha256,
        "--expected-receipt-reader-program-sha256",
        case["expectations"].receipt_reader_program_sha256,
        "--expected-schema-sha256",
        case["expectations"].schema_sha256,
        "--expected-electron-configuration-sha256",
        case["expectations"].electron_configuration_sha256,
        "--expected-transfer-repository",
        case["expectations"].transfer_repository,
        "--expected-transfer-repository-id",
        case["expectations"].transfer_repository_id,
        "--expected-transfer-repository-owner-id",
        case["expectations"].transfer_repository_owner_id,
        "--expected-transfer-ref",
        case["expectations"].transfer_ref,
        "--expected-transfer-event",
        case["expectations"].transfer_event,
        "--expected-transfer-run-id",
        case["expectations"].transfer_run_id,
        "--expected-transfer-run-attempt",
        str(case["expectations"].transfer_run_attempt),
        "--repository",
        "andre-motta/tongs",
        "--run-id",
        "12345",
        "--attempt",
        "1",
        "--environment",
        "ubuntu-24.04",
        "--provenance",
        "hosted",
        "--check-id",
        "desktop-archive-sbom",
    ]

    assert ADAPTER.main(arguments) == 1
    assert not case["output"].exists()
