"""Focused failure cases for installed-core composition evidence."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration.desktop.installed_core_composition import (
    _entrypoint_interpreter,
    sha256_file,
    validate_audit_records,
    validate_inputs,
)
from tests.integration.desktop.installed_core_sidecar_fixture import (
    validate_prior_ledger,
)


def _arguments(tmp_path: Path, source_root: Path, wheel: Path) -> argparse.Namespace:
    return argparse.Namespace(
        core_wheel=wheel,
        environment_root=tmp_path / "candidate-venv",
        evidence_root=tmp_path / "evidence",
        expected_core_version="0.4.2.dev1",
        expected_source_commit="1" * 40,
        expected_wheel_sha256=sha256_file(wheel) if wheel.exists() else "2" * 64,
        source_root=source_root,
    )


def _source_fixture(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    destination = source / "tests/integration/desktop"
    destination.mkdir(parents=True)
    checkout = Path(__file__).resolve().parents[3]
    for name in (
        "draft_acceptance_sidecar.py",
        "installed_core_audit_sitecustomize.py",
        "installed_core_sidecar_fixture.py",
    ):
        shutil.copyfile(
            checkout / "tests/integration/desktop" / name, destination / name
        )
    return source


def _wheel(tmp_path: Path) -> Path:
    path = tmp_path / "tongs.whl"
    path.write_bytes(b"wheel fixture")
    return path


def _audit_records(
    *, source_root: Path, package_root: Path, audit_root: Path, python: Path
) -> list[dict[str, object]]:
    return [
        {
            "event": "audit_started",
            "executable": str(python),
            "mcp_available": False,
            "sys_path": [str(audit_root), str(package_root.parent)],
        },
        {
            "event": "audit_finished",
            "blocked": False,
            "loaded_forbidden_modules": [],
            "tongs_module_origins": {"tongs": str(package_root / "__init__.py")},
        },
    ]


def test_input_contract_rejects_missing_wheel_and_source_mapping(
    tmp_path: Path,
) -> None:
    source = _source_fixture(tmp_path)
    missing = tmp_path / "missing.whl"
    with pytest.raises(RuntimeError, match="core wheel must identify an existing file"):
        validate_inputs(_arguments(tmp_path, source, missing))

    wheel = _wheel(tmp_path)
    arguments = _arguments(tmp_path, source, wheel)
    arguments.environment_root = source / "candidate-venv"
    with pytest.raises(RuntimeError, match="outside the source checkout"):
        validate_inputs(arguments)


def test_input_contract_rejects_changed_wheel_hash(tmp_path: Path) -> None:
    source = _source_fixture(tmp_path)
    wheel = _wheel(tmp_path)
    arguments = _arguments(tmp_path, source, wheel)
    arguments.expected_wheel_sha256 = "f" * 64
    with pytest.raises(RuntimeError, match="SHA-256 does not match"):
        validate_inputs(arguments)


def test_entrypoint_provenance_uses_exact_versioned_shebang(tmp_path: Path) -> None:
    environment = tmp_path / "candidate"
    binary = environment / "bin/python3.14"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"python")
    entrypoint = environment / "bin/tongs"
    entrypoint.write_text(f"#!{binary}\n")
    assert _entrypoint_interpreter(entrypoint, environment) == binary.resolve()

    outside = tmp_path / "outside-python"
    outside.write_bytes(b"python")
    entrypoint.write_text(f"#!{outside}\n")
    with pytest.raises(RuntimeError, match="escaped the candidate environment"):
        _entrypoint_interpreter(entrypoint, environment)


def test_audit_validation_rejects_forbidden_attempt_and_checkout_origin(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    package = tmp_path / "venv/site-packages/tongs"
    audit = tmp_path / "audit"
    python = tmp_path / "venv/bin/python"
    records = _audit_records(
        source_root=source,
        package_root=package,
        audit_root=audit,
        python=python,
    )
    records.insert(1, {"event": "blocked", "audit_event": "socket.connect"})
    with pytest.raises(RuntimeError, match="attempted forbidden activity"):
        validate_audit_records(
            records,
            expected_python=python,
            installed_package_root=package,
            source_root=source,
            audit_root=audit,
        )

    records = _audit_records(
        source_root=source,
        package_root=package,
        audit_root=audit,
        python=python,
    )
    records[-1]["tongs_module_origins"] = {
        "tongs": str(source / "src/tongs/__init__.py")
    }
    with pytest.raises(RuntimeError, match="outside the installed package"):
        validate_audit_records(
            records,
            expected_python=python,
            installed_package_root=package,
            source_root=source,
            audit_root=audit,
        )


def test_checked_in_audit_hook_blocks_subprocess_and_marks_evidence(
    tmp_path: Path,
) -> None:
    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    helper = Path(__file__).with_name("installed_core_audit_sitecustomize.py")
    shutil.copyfile(helper, audit_root / "sitecustomize.py")
    audit_path = tmp_path / "audit.jsonl"
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(audit_root),
        "TONGS_INSTALLED_CORE_AUDIT_PATH": str(audit_path),
        "TONGS_INSTALLED_CORE_EXPECTED_PYTHON": str(Path(sys.executable).resolve()),
        "TONGS_INSTALLED_CORE_SOURCE_ROOT": str(Path(__file__).resolve().parents[3]),
    }
    completed = subprocess.run(
        [sys.executable, "-c", "import subprocess; subprocess.run(['true'])"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode != 0
    records = [json.loads(line) for line in audit_path.read_text().splitlines()]
    blocked = [record for record in records if record.get("event") == "blocked"]
    assert [record["audit_event"] for record in blocked] == ["subprocess.Popen"]


def test_restart_ledger_guard_rejects_missing_or_replayed_call(tmp_path: Path) -> None:
    ledger = tmp_path / "mock-forge-ledger.jsonl"
    with pytest.raises(RuntimeError, match="missing, changed, or replayed"):
        validate_prior_ledger(ledger, "acknowledge")

    record = {
        "action": "add_comment",
        "body": "one controlled installed-core mutation",
        "project": "acceptance/shared-drafts",
        "remote_id": "remote-comment-106",
        "review_number": 106,
        "sequence": 1,
        "stage": "remote_accepted_before_ack",
    }
    ledger.write_text(json.dumps(record) + "\n" + json.dumps(record) + "\n")
    with pytest.raises(RuntimeError, match="missing, changed, or replayed"):
        validate_prior_ledger(ledger, "acknowledge")

    ledger.write_text(json.dumps(record) + "\n")
    validate_prior_ledger(ledger, "acknowledge")
