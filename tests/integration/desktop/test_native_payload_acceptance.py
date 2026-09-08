"""Adversarial tests for the native installed-payload acceptance harness."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import select
import shutil
import signal
import struct
import subprocess
import sys
import time
import zlib
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.desktop.artifact_contract.reference_builder import (
    FILE_INPUTS,
    FIXTURE_ROOT,
    INPUT_ROOT,
    SOURCE_COMMIT,
)
from tests.desktop.native import native_payload_launcher as launcher_module
from tests.desktop.native.native_payload_launcher import verify_transient_guard
from tests.integration.desktop import (
    native_payload_acceptance as acceptance_module,
)
from tests.integration.desktop.native_payload_acceptance import (
    CoreBinding,
    GpuPolicy,
    NativeAcceptanceError,
    NativePayloadPolicy,
    NativeRunObservation,
    PriorInput,
    ProcessObservation,
    ReceiptExpectation,
    RunPolicy,
    SandboxStatus,
    capture_core_snapshot,
    capture_expected_outputs,
    capture_payload_snapshot,
    core_binding_from_report,
    parse_bound_manifests,
    verify_native_acceptance,
    verify_prior_inputs,
)
from tongs.desktop.artifact_contract import INSTALL_MANIFEST_PATH

SOURCE_TREE = "1" * 40
WHEEL_SHA256 = "2" * 64
VENDOR_ID = 0x10DE
DEVICE_ID = 0x2B85


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _record_hash(value: bytes) -> str:
    digest = hashlib.sha256(value).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _write(path: Path, value: bytes, mode: int) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(value)
    path.chmod(mode)


def _png(width: int = 1180, height: int = 780) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    pixels = zlib.compress((b"\x00" + b"\x00" * width * 4) * height)

    def chunk(name: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + name
            + body
            + struct.pack(">I", zlib.crc32(name + body) & 0xFFFFFFFF)
        )

    return (
        signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")
    )


def _fixture(tmp_path: Path) -> dict[str, Any]:
    evidence = tmp_path / "evidence"
    payload = tmp_path / "payload"
    safe_cwd = tmp_path / "safe-cwd"
    prefix = tmp_path / "core-env"
    site_packages = prefix / "lib/python3.12/site-packages"
    package_root = site_packages / "tongs"
    distribution = site_packages / "tongs-1.2.3.dist-info"
    for directory in (
        evidence,
        payload,
        safe_cwd,
        prefix / "bin",
        package_root,
        distribution,
    ):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    requested = prefix / "bin/python"
    shutil.copyfile(Path(sys.executable).resolve(strict=True), requested)
    requested.chmod(0o755)
    package_document = b'__version__ = "fixture"\n'
    generated_document = b"synthetic bytecode"
    distribution_documents = {
        "METADATA": b"Name: tongs\nVersion: 1.2.3\n",
        "WHEEL": b"Wheel-Version: 1.0\n",
        "entry_points.txt": b"[console_scripts]\ntongs = tongs.__main__:main\n",
        "licenses/LICENSE": b"synthetic license\n",
    }
    direct_url = {
        "archive_info": {"hashes": {"sha256": WHEEL_SHA256}},
        "url": "file:///trusted/tongs-1.2.3-py3-none-any.whl",
    }
    direct_document = json.dumps(direct_url, sort_keys=True).encode()
    installer_document = b"pip\n"
    requested_document = b""
    scripts = {
        "tongs": b"#!/bin/sh\nexit 0\n",
        "tongs-mcp": b"#!/bin/sh\nexit 0\n",
    }
    _write(package_root / "__init__.py", package_document, 0o644)
    _write(
        package_root / "__pycache__/__init__.fixture.pyc",
        generated_document,
        0o644,
    )
    for path, document in distribution_documents.items():
        _write(distribution / path, document, 0o644)
    _write(distribution / "INSTALLER", installer_document, 0o644)
    _write(distribution / "REQUESTED", requested_document, 0o644)
    _write(distribution / "direct_url.json", direct_document, 0o644)
    for path, document in scripts.items():
        _write(prefix / "bin" / path, document, 0o755)
    record_rows = [
        [f"../../../bin/{path}", _record_hash(document), str(len(document))]
        for path, document in scripts.items()
    ]
    record_rows.extend(
        [
            [
                "tongs-1.2.3.dist-info/INSTALLER",
                _record_hash(installer_document),
                str(len(installer_document)),
            ],
            [
                "tongs-1.2.3.dist-info/REQUESTED",
                _record_hash(requested_document),
                "0",
            ],
            [
                "tongs-1.2.3.dist-info/direct_url.json",
                _record_hash(direct_document),
                str(len(direct_document)),
            ],
            ["tongs-1.2.3.dist-info/RECORD", "", ""],
            [
                "tongs/__init__.py",
                _record_hash(package_document),
                str(len(package_document)),
            ],
            ["tongs/__pycache__/__init__.fixture.pyc", "", ""],
        ]
    )
    record_rows.extend(
        [
            [
                f"tongs-1.2.3.dist-info/{path}",
                _record_hash(document),
                str(len(document)),
            ]
            for path, document in distribution_documents.items()
        ]
    )
    record_document = "".join(",".join(row) + "\r\n" for row in record_rows).encode()
    _write(distribution / "RECORD", record_document, 0o644)
    install_document = (FIXTURE_ROOT / "desktop-install.synthetic.json").read_bytes()
    release_document = (
        FIXTURE_ROOT / "desktop-manifest-v1.synthetic.json"
    ).read_bytes()
    lifecycle_document = b'{"result":"success","structural_fixture":true}\n'
    archive_document = b"synthetic installed archive\n"
    core_report_document = b'{"synthetic":"core report"}\n'
    staged_documents = {
        "artifact-lifecycle.json": lifecycle_document,
        "desktop-archive.tar.zst": archive_document,
        "installed-core.json": core_report_document,
        "desktop-install.json": install_document,
        "desktop-release.json": release_document,
    }
    for path, document in staged_documents.items():
        _write(evidence / path, document, 0o644)
    receipt = {
        "schema_version": 1,
        "check_id": "desktop-archive",
        "source": {"commit": SOURCE_COMMIT, "tree": SOURCE_TREE},
        "execution": {
            "repository": "example/tongs",
            "run_id": "12345",
            "attempt": 1,
            "environment": "fedora-44",
            "provenance": "controlled-fixture",
        },
        "result": "success",
        "reports": [
            {
                "path": "artifact-lifecycle.json",
                "size": len(lifecycle_document),
                "sha256": _sha256(lifecycle_document),
                "format": "artifact-lifecycle-v1",
            }
        ],
        "artifacts": [
            {
                "path": "desktop-archive.tar.zst",
                "size": len(archive_document),
                "sha256": _sha256(archive_document),
                "role": "desktop-archive",
            }
        ],
        "inputs": [
            {
                "path": "installed-core.json",
                "sha256": _sha256(core_report_document),
            },
            {"path": "desktop-install.json", "sha256": _sha256(install_document)},
            {"path": "desktop-release.json", "sha256": _sha256(release_document)},
        ],
    }
    receipt_document = (
        json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    _write(evidence / "artifact-receipt.json", receipt_document, 0o600)
    prior_documents = {
        "desktop-artifact-receipt": (
            "artifact-receipt.json",
            receipt_document,
            0o600,
        ),
        "desktop-install-manifest": (
            "desktop-install.json",
            install_document,
            0o644,
        ),
        "desktop-release-manifest": (
            "desktop-release.json",
            release_document,
            0o644,
        ),
        "installed-core-report": (
            "installed-core.json",
            core_report_document,
            0o644,
        ),
    }
    prior_inputs = []
    for role, (path, document, mode) in prior_documents.items():
        prior_inputs.append(
            PriorInput(
                role=role,
                path=path,
                size=len(document),
                sha256=_sha256(document),
                mode=mode,
            )
        )
    core = CoreBinding(
        requested_executable=str(requested),
        proc_executable=str(requested),
        system_python_target=str(Path(sys.executable).resolve(strict=True)),
        base_executable=str(Path(sys.executable).resolve(strict=True)),
        sys_prefix=str(prefix),
        site_packages=(str(site_packages),),
        package_root=str(package_root),
        distribution_path=str(distribution),
        tongs_version="1.2.3",
        source_commit=SOURCE_COMMIT,
        source_tree=SOURCE_TREE,
        wheel_sha256=WHEEL_SHA256,
        direct_url=direct_url,
        editable=False,
        package_members=(
            ("tongs/__init__.py", len(package_document), _sha256(package_document)),
        ),
        archive_members=tuple(
            sorted(
                {
                    "tongs/__init__.py": _sha256(package_document),
                    **{
                        f"tongs-1.2.3.dist-info/{path}": _sha256(document)
                        for path, document in distribution_documents.items()
                    },
                    "tongs-1.2.3.dist-info/RECORD": "3" * 64,
                }.items()
            )
        ),
        generated_exclusion="only __pycache__/*.pyc",
        generated_members=(
            (
                "__pycache__/__init__.fixture.pyc",
                len(generated_document),
                _sha256(generated_document),
            ),
        ),
        installed_uid=os.geteuid(),
    )
    runs = tuple(
        RunPolicy(
            report_path=f"run-{index}.json",
            screenshot_paths=(f"run-{index}.png",),
            screenshot_dimensions=((1180, 780),),
            profile_path=f"profile-{index}",
        )
        for index in (1, 2)
    )
    policy = NativePayloadPolicy(
        source_commit=SOURCE_COMMIT,
        source_tree=SOURCE_TREE,
        artifact_id="fedora-44-x86_64-user-archive",
        install_manifest_sha256=_sha256(install_document),
        release_manifest_sha256=_sha256(release_document),
        prior_inputs=tuple(prior_inputs),
        artifact_receipt=ReceiptExpectation(
            repository="example/tongs",
            run_id="12345",
            attempt=1,
            environment="fedora-44",
            provenance="controlled-fixture",
            check_id="desktop-archive",
            allowed_report_formats=("artifact-lifecycle-v1",),
        ),
        core=core,
        runs=runs,
        gpu=GpuPolicy(
            vendor_id=VENDOR_ID,
            device_id=DEVICE_ID,
            renderer_tokens=("nvidia", "5090"),
        ),
        safe_cwd=str(safe_cwd),
        provenance="controlled-fixture",
        evidence_uid=os.geteuid(),
        payload_uid=os.geteuid(),
        payload_root_mode=0o700,
    )
    install, _ = parse_bound_manifests(install_document, release_document, policy)
    _write(payload / INSTALL_MANIFEST_PATH, install_document, 0o644)
    for relative, source in FILE_INPUTS.items():
        expected = next(item for item in install.files if item.path == relative)
        _write(
            payload / relative,
            (INPUT_ROOT / source).read_bytes(),
            0o755 if expected.executable else 0o644,
        )
    for directory in (
        *(path for path in package_root.rglob("*") if path.is_dir()),
        *(path for path in distribution.rglob("*") if path.is_dir()),
        package_root,
        distribution,
        prefix,
        *(path for path in (payload / "runtime").rglob("*") if path.is_dir()),
        payload / "runtime",
    ):
        directory.chmod(0o755)
    initial = capture_payload_snapshot(
        payload,
        install,
        expected_uid=policy.payload_uid,
        root_mode=policy.payload_root_mode,
        install_manifest_sha256=policy.install_manifest_sha256,
    )
    initial_core = capture_core_snapshot(core)
    observations = tuple(
        _observation(evidence, payload, policy, run, initial, initial_core)
        for run in runs
    )
    return {
        "evidence": evidence,
        "payload": payload,
        "policy": policy,
        "install": install,
        "initial": initial,
        "initial_core": initial_core,
        "observations": observations,
    }


def _observation(
    evidence: Path,
    payload: Path,
    policy: NativePayloadPolicy,
    run: RunPolicy,
    initial: Any,
    initial_core: Any,
) -> NativeRunObservation:
    processes = _processes(evidence, payload, policy, run)
    report = _smoke_report(policy, processes)
    _write(
        evidence / run.report_path,
        (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        0o600,
    )
    for screenshot in run.screenshot_paths:
        _write(evidence / screenshot, _png(), 0o600)
    return NativeRunObservation(
        exit_code=0,
        signal=None,
        outputs=capture_expected_outputs(evidence, run, policy.evidence_uid),
        processes=processes,
        payload_after=initial,
        core_after=initial_core,
    )


def _processes(
    evidence: Path,
    payload: Path,
    policy: NativePayloadPolicy,
    run: RunPolicy,
) -> tuple[ProcessObservation, ...]:
    launcher = str((payload / "runtime/tongs-desktop").resolve(strict=True))
    sidecar_argv = (
        policy.core.requested_executable,
        "-E",
        "-P",
        "-m",
        "tongs.desktop.sidecar",
    )
    main_argv = (
        launcher,
        "--ozone-platform=x11",
        f"--user-data-dir={evidence / run.profile_path}",
        "--tongs-python-executable",
        policy.core.requested_executable,
        "--tongs-core-version",
        policy.core.tongs_version,
        "--tongs-safe-cwd",
        policy.safe_cwd,
        "--tongs-smoke-report",
        str(evidence / run.report_path),
        "--tongs-smoke-source-commit",
        policy.source_commit,
    )
    return (
        ProcessObservation(
            100,
            1,
            100,
            1000,
            "browser",
            launcher,
            main_argv,
            policy.safe_cwd,
            SandboxStatus(0, 0, 0),
        ),
        ProcessObservation(
            101,
            100,
            100,
            1010,
            "zygote",
            launcher,
            (launcher, "--type=zygote"),
            policy.safe_cwd,
            SandboxStatus(0, 0, 0),
        ),
        ProcessObservation(
            102,
            101,
            100,
            1020,
            "gpu",
            launcher,
            (launcher, "--type=gpu-process"),
            policy.safe_cwd,
            SandboxStatus(1, 2, 1),
        ),
        ProcessObservation(
            103,
            101,
            100,
            1030,
            "renderer",
            launcher,
            (launcher, "--type=renderer"),
            policy.safe_cwd,
            SandboxStatus(1, 2, 1),
        ),
        ProcessObservation(
            104,
            101,
            100,
            1040,
            "renderer",
            launcher,
            (launcher, "--type=renderer"),
            policy.safe_cwd,
            SandboxStatus(1, 2, 1),
        ),
        ProcessObservation(
            110,
            100,
            100,
            1100,
            "python-sidecar",
            policy.core.proc_executable,
            sidecar_argv,
            policy.safe_cwd,
            SandboxStatus(0, 2, 1),
        ),
        ProcessObservation(
            111,
            100,
            100,
            1110,
            "python-sidecar",
            policy.core.proc_executable,
            sidecar_argv,
            policy.safe_cwd,
            SandboxStatus(0, 2, 1),
        ),
        ProcessObservation(
            112,
            100,
            100,
            1120,
            "python-sidecar",
            policy.core.proc_executable,
            sidecar_argv,
            policy.safe_cwd,
            SandboxStatus(0, 2, 1),
        ),
    )


def _smoke_report(
    policy: NativePayloadPolicy, processes: tuple[ProcessObservation, ...]
) -> dict[str, Any]:
    probe = {
        "processGlobal": "undefined",
        "requireGlobal": "undefined",
        "webglVendor": "NVIDIA Corporation",
        "webglRenderer": "ANGLE (NVIDIA RTX 5090)",
    }
    linux = {"noNewPrivs": 1, "seccomp": 2, "seccompFilters": 1}
    return {
        "sourceCommit": policy.source_commit,
        "electron": "44.2.0",
        "chrome": "152.0.0.0",
        "node": "24.0.0",
        "sidecarPid": 112,
        "sidecarLinuxSandbox": {"noNewPrivs": 0, "seccomp": 2, "seccompFilters": 1},
        "sessionGeneration": 3,
        "reload": {
            "initialSessionGeneration": 1,
            "finalSessionGeneration": 2,
            "initialRendererProbe": probe,
            "finalRendererProbe": probe,
        },
        "rendererCrashRecovery": {
            "initialSessionGeneration": 2,
            "finalSessionGeneration": 3,
            "injection": "SIGKILL",
            "injectedRendererPid": 103,
            "finalRendererProbe": probe,
        },
        "rendererProbe": probe,
        "uiProof": None,
        "uiProofScreenshot": None,
        "narrowUiProofScreenshot": None,
        "gpu": {
            "auxAttributes": {
                "glImplementationParts": "(gl=egl-angle,angle=opengl)",
                "inProcessGpu": False,
            },
            "gpuDevice": [
                {
                    "active": True,
                    "vendorId": VENDOR_ID,
                    "deviceId": DEVICE_ID,
                }
            ],
        },
        "gpuFeatureStatus": {
            "gpu_compositing": "enabled",
            "rasterization": "enabled",
            "opengl": "enabled_on",
            "webgl": "enabled",
        },
        "metrics": [
            {"type": "Browser", "pid": 100},
            {
                "type": "GPU",
                "pid": 102,
                "serviceName": "GPU",
                "linuxSandbox": linux,
            },
            {"type": "Tab", "pid": 104, "linuxSandbox": linux},
        ],
        "sidecarProcessHistory": [
            {"code": 0, "signal": None, "unexpected": False},
            {"code": 0, "signal": None, "unexpected": False},
        ],
        "childProcessFailures": [],
        "security": {
            "sandbox": True,
            "contextIsolation": True,
            "nodeIntegration": False,
            "webviewTag": False,
            "scheme": "tongs://app/index.html",
            "xwayland": True,
        },
    }


def _verify(fixture: dict[str, Any]) -> Any:
    return verify_native_acceptance(
        payload_root=fixture["payload"],
        evidence_root=fixture["evidence"],
        install=fixture["install"],
        policy=fixture["policy"],
        initial_payload=fixture["initial"],
        initial_core=fixture["initial_core"],
        core_observation={
            "requested_executable": fixture["policy"].core.requested_executable,
            "proc_executable": fixture["policy"].core.proc_executable,
            "system_python_target": fixture["policy"].core.system_python_target,
            "base_executable": fixture["policy"].core.base_executable,
            "sys_prefix": fixture["policy"].core.sys_prefix,
            "site_packages": list(fixture["policy"].core.site_packages),
            "package_root": fixture["policy"].core.package_root,
            "distribution_path": fixture["policy"].core.distribution_path,
            "tongs_version": fixture["policy"].core.tongs_version,
            "direct_url": fixture["policy"].core.direct_url,
            "editable": fixture["policy"].core.editable,
        },
        observations=fixture["observations"],
    )


def test_accepts_repeated_synthetic_fixture_as_structural_only(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    verify_prior_inputs(fixture["evidence"], fixture["policy"])

    result = _verify(fixture)

    assert result.validation == "controlled-fixture-structural-only"
    assert result.provenance == "controlled-fixture"
    assert result.runs == 2
    assert result.renderer == "ANGLE (NVIDIA RTX 5090)"


def test_rejects_tampered_and_extra_payload_files(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    launcher = fixture["payload"] / "runtime/tongs-desktop"
    launcher.write_bytes(b"changed")
    with pytest.raises(NativeAcceptanceError, match="differs from manifest"):
        capture_payload_snapshot(
            fixture["payload"],
            fixture["install"],
            expected_uid=fixture["policy"].payload_uid,
            root_mode=fixture["policy"].payload_root_mode,
            install_manifest_sha256=fixture["policy"].install_manifest_sha256,
        )

    fixture = _fixture(tmp_path / "extra")
    _write(fixture["payload"] / "runtime/extra", b"extra", 0o644)
    with pytest.raises(NativeAcceptanceError, match="manifest paths"):
        capture_payload_snapshot(
            fixture["payload"],
            fixture["install"],
            expected_uid=fixture["policy"].payload_uid,
            root_mode=fixture["policy"].payload_root_mode,
            install_manifest_sha256=fixture["policy"].install_manifest_sha256,
        )


def test_rejects_payload_symlink_substitution(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    target = tmp_path / "replacement"
    target.write_bytes(b"replacement")
    path = fixture["payload"] / "runtime/LICENSES.json"
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(NativeAcceptanceError, match="link or special file"):
        capture_payload_snapshot(
            fixture["payload"],
            fixture["install"],
            expected_uid=fixture["policy"].payload_uid,
            root_mode=fixture["policy"].payload_root_mode,
            install_manifest_sha256=fixture["policy"].install_manifest_sha256,
        )


@pytest.mark.parametrize("extra", ("unexpected.txt", "node_modules/package.json"))
def test_rejects_payload_root_siblings(tmp_path: Path, extra: str) -> None:
    fixture = _fixture(tmp_path)
    _write(fixture["payload"] / extra, b"undeclared", 0o644)
    with pytest.raises(NativeAcceptanceError, match="undeclared top-level"):
        capture_payload_snapshot(
            fixture["payload"],
            fixture["install"],
            expected_uid=fixture["policy"].payload_uid,
            root_mode=fixture["policy"].payload_root_mode,
            install_manifest_sha256=fixture["policy"].install_manifest_sha256,
        )


def test_rejects_changed_installed_root_manifest(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    (fixture["payload"] / INSTALL_MANIFEST_PATH).write_bytes(b"substituted")
    with pytest.raises(NativeAcceptanceError, match="root manifest differs"):
        capture_payload_snapshot(
            fixture["payload"],
            fixture["install"],
            expected_uid=fixture["policy"].payload_uid,
            root_mode=fixture["policy"].payload_root_mode,
            install_manifest_sha256=fixture["policy"].install_manifest_sha256,
        )


def test_rejects_prior_input_intermediate_symlink(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    _write(outside / "receipt.json", b"same", 0o600)
    (fixture["evidence"] / "linked").symlink_to(outside, target_is_directory=True)
    forged = PriorInput(
        "desktop-artifact-receipt", "linked/receipt.json", 4, _sha256(b"same"), 0o600
    )
    policy = replace(
        fixture["policy"], prior_inputs=(forged, *fixture["policy"].prior_inputs[1:])
    )
    with pytest.raises(NativeAcceptanceError, match="cannot be opened safely"):
        verify_prior_inputs(fixture["evidence"], policy)


def test_rejects_stale_manifest_and_core_identity(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    install_document = (FIXTURE_ROOT / "desktop-install.synthetic.json").read_bytes()
    release_document = (
        FIXTURE_ROOT / "desktop-manifest-v1.synthetic.json"
    ).read_bytes()
    stale = replace(fixture["policy"], source_commit="3" * 40)
    with pytest.raises(NativeAcceptanceError, match="core source identity"):
        parse_bound_manifests(install_document, release_document, stale)
    bad_hash = replace(fixture["policy"], install_manifest_sha256="4" * 64)
    with pytest.raises(NativeAcceptanceError, match="prior manifest identities"):
        parse_bound_manifests(install_document, release_document, bad_hash)


def test_extracts_narrow_core_binding_and_rejects_stale_report(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    core = fixture["policy"].core
    report = {
        "source": {
            "commit": SOURCE_COMMIT,
            "tree": SOURCE_TREE,
            "tracked_status": "clean",
            "package_member_sha256": {
                path: digest for path, _size, digest in core.package_members
            },
        },
        "wheel": {
            "sha256": WHEEL_SHA256,
            "package_member_sha256": {
                path: digest for path, _size, digest in core.package_members
            },
            "archive_member_sha256": dict(core.archive_members),
        },
        "installed_identity": {
            "executable": core.requested_executable,
            "proc_self_exe": core.proc_executable,
            "system_python_target": core.system_python_target,
            "base_executable": core.base_executable,
            "sys_prefix": core.sys_prefix,
            "site_packages": [*core.site_packages, *core.site_packages],
            "package_root": core.package_root,
            "distribution_path": core.distribution_path,
            "tongs_version": core.tongs_version,
            "direct_url": core.direct_url,
            "editable": False,
            "wheel_members": {
                path: {
                    "path": str(Path(core.site_packages[0]) / path),
                    "size": size,
                    "sha256": digest,
                }
                for path, size, digest in core.package_members
            },
            "generated_exclusions": {
                "policy": core.generated_exclusion,
                "files": {
                    path: {
                        "path": str(Path(core.package_root) / path),
                        "size": size,
                        "sha256": digest,
                    }
                    for path, size, digest in core.generated_members
                },
            },
            "invalid_generated": [],
        },
    }
    document = (json.dumps(report, sort_keys=True) + "\n").encode()

    assert (
        core_binding_from_report(
            document,
            expected_report_sha256=_sha256(document),
            expected_source_commit=SOURCE_COMMIT,
            expected_source_tree=SOURCE_TREE,
            expected_wheel_sha256=WHEEL_SHA256,
            expected_installed_uid=os.geteuid(),
        )
        == core
    )

    report["source"]["tree"] = "7" * 40
    stale = (json.dumps(report, sort_keys=True) + "\n").encode()
    with pytest.raises(NativeAcceptanceError, match="source identity is stale"):
        core_binding_from_report(
            stale,
            expected_report_sha256=_sha256(stale),
            expected_source_commit=SOURCE_COMMIT,
            expected_source_tree=SOURCE_TREE,
            expected_wheel_sha256=WHEEL_SHA256,
            expected_installed_uid=os.geteuid(),
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda report: report.update(sourceCommit="5" * 40), "source commit is stale"),
        (
            lambda report: report["rendererProbe"].update(webglRenderer="SwiftShader"),
            "software GPU",
        ),
        (
            lambda report: report["gpuFeatureStatus"].update(webgl="disabled"),
            "not hardware enabled",
        ),
        (
            lambda report: report["childProcessFailures"].append({"type": "boom"}),
            "unexpected child failure",
        ),
        (lambda report: report["gpu"].update(gpuDevice=[]), "not uniquely active"),
    ],
)
def test_rejects_false_gpu_and_smoke_evidence(
    tmp_path: Path, mutation: Any, message: str
) -> None:
    fixture = _fixture(tmp_path)
    report_path = fixture["evidence"] / fixture["policy"].runs[0].report_path
    report = json.loads(report_path.read_bytes())
    mutation(report)
    _write(report_path, (json.dumps(report) + "\n").encode(), 0o600)
    changed = replace(
        fixture["observations"][0],
        outputs=capture_expected_outputs(
            fixture["evidence"],
            fixture["policy"].runs[0],
            fixture["policy"].evidence_uid,
        ),
    )
    fixture["observations"] = (changed, fixture["observations"][1])
    with pytest.raises(NativeAcceptanceError, match=message):
        _verify(fixture)


def test_rejects_missing_process_sandbox_and_launch_binding(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    first = fixture["observations"][0]
    without_gpu = replace(
        first, processes=tuple(item for item in first.processes if item.role != "gpu")
    )
    fixture["observations"] = (without_gpu, fixture["observations"][1])
    with pytest.raises(NativeAcceptanceError, match="metric PID lacks"):
        _verify(fixture)

    fixture = _fixture(tmp_path / "sandbox")
    first = fixture["observations"][0]
    processes = tuple(
        replace(item, sandbox=replace(item.sandbox, seccomp=0))
        if item.role == "gpu"
        else item
        for item in first.processes
    )
    fixture["observations"] = (
        replace(first, processes=processes),
        fixture["observations"][1],
    )
    with pytest.raises(NativeAcceptanceError, match="sandbox evidence"):
        _verify(fixture)

    fixture = _fixture(tmp_path / "inherited")
    first = fixture["observations"][0]
    processes = tuple(
        replace(item, sandbox=SandboxStatus(1, 2, 1)) if item.role == "zygote" else item
        for item in first.processes
    )
    fixture["observations"] = (
        replace(first, processes=processes),
        fixture["observations"][1],
    )
    with pytest.raises(NativeAcceptanceError, match="inherited runner filters"):
        _verify(fixture)

    fixture = _fixture(tmp_path / "argv")
    first = fixture["observations"][0]
    processes = tuple(
        replace(item, argv=(*item.argv, "--no-sandbox"))
        if item.role == "browser"
        else item
        for item in first.processes
    )
    fixture["observations"] = (
        replace(first, processes=processes),
        fixture["observations"][1],
    )
    with pytest.raises(NativeAcceptanceError, match="launch policy"):
        _verify(fixture)

    fixture = _fixture(tmp_path / "executable")
    first = fixture["observations"][0]
    processes = tuple(
        replace(
            item,
            executable="/usr/bin/false",
            argv=("/usr/bin/false", *item.argv[1:]),
            raw_argv=("/usr/bin/false", *item.argv[1:]),
        )
        if item.role == "gpu"
        else item
        for item in first.processes
    )
    fixture["observations"] = (
        replace(first, processes=processes),
        fixture["observations"][1],
    )
    with pytest.raises(NativeAcceptanceError, match="executable is undeclared"):
        _verify(fixture)


def test_accepts_exact_raw_zygote_argv_compaction_in_final_verifier(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    observations = []
    for observation in fixture["observations"]:
        processes = tuple(
            replace(
                process,
                raw_argv=(f"{process.executable} --type=zygote",),
            )
            if process.role == "zygote"
            else process
            for process in observation.processes
        )
        observations.append(replace(observation, processes=processes))
    fixture["observations"] = tuple(observations)

    assert _verify(fixture).validation == "controlled-fixture-structural-only"


@pytest.mark.parametrize(
    ("role", "process_type"),
    [
        ("zygote", "zygote"),
        ("gpu", "gpu-process"),
        ("renderer", "renderer"),
        ("utility", "utility"),
    ],
)
def test_final_verifier_accepts_exact_chromium_raw_argv_compaction(
    tmp_path: Path, role: str, process_type: str
) -> None:
    fixture = _fixture(tmp_path)
    base = fixture["observations"][0].processes[1]
    canonical = (
        base.executable,
        f"--type={process_type}",
        "--retained=value with space",
    )
    process = replace(
        base,
        role=role,
        argv=canonical,
        raw_argv=(" ".join(canonical),),
    )

    acceptance_module._verify_raw_process_argv(process)


@pytest.mark.parametrize(
    "type_arguments",
    [
        (),
        ("--type=zygote", "--type=zygote"),
        ("--type=zygote", "--type=renderer"),
    ],
)
def test_final_verifier_rejects_missing_duplicate_or_conflicting_type_in_raw_title(
    tmp_path: Path, type_arguments: tuple[str, ...]
) -> None:
    fixture = _fixture(tmp_path)
    base = fixture["observations"][0].processes[1]
    canonical = (base.executable, *type_arguments)
    process = replace(
        base,
        argv=canonical,
        raw_argv=(" ".join(canonical),),
    )

    with pytest.raises(
        NativeAcceptanceError,
        match="process role differs from canonical type arguments",
    ):
        acceptance_module._verify_raw_process_argv(process)


@pytest.mark.parametrize(
    "raw",
    [
        ("{exe}  --type=zygote",),
        ("{exe}\t--type=zygote",),
        ("{exe} --type=renderer",),
        ("{exe} --type=zygote --extra",),
        ("--type=zygote {exe}",),
        ("{exe}",),
    ],
)
def test_final_verifier_rejects_other_raw_argv_claims(
    tmp_path: Path, raw: tuple[str, ...]
) -> None:
    fixture = _fixture(tmp_path)
    first = fixture["observations"][0]
    processes = tuple(
        replace(
            process,
            raw_argv=tuple(value.format(exe=process.executable) for value in raw),
        )
        if process.role == "zygote"
        else process
        for process in first.processes
    )
    fixture["observations"] = (
        replace(first, processes=processes),
        fixture["observations"][1],
    )

    with pytest.raises(
        NativeAcceptanceError,
        match="raw process arguments differ from canonical arguments",
    ):
        _verify(fixture)


def test_final_verifier_accepts_browser_argv_compaction_end_to_end(
    tmp_path: Path,
) -> None:
    """Failed native attempt 7: the browser title rewrote in the second run."""

    fixture = _fixture(tmp_path)
    first, second = fixture["observations"]
    processes = tuple(
        replace(process, raw_argv=(" ".join(process.argv),))
        if process.role == "browser"
        else process
        for process in second.processes
    )
    fixture["observations"] = (first, replace(second, processes=processes))

    assert _verify(fixture).validation == "controlled-fixture-structural-only"


@pytest.mark.parametrize(
    "title",
    [
        "{joined} --extra",
        "{joined}x",
        "{joined} ",
        " {joined}",
        "{proc_self_exe} {tail}",
    ],
)
def test_final_verifier_rejects_browser_compaction_differing_by_any_token(
    tmp_path: Path, title: str
) -> None:
    fixture = _fixture(tmp_path)
    first, second = fixture["observations"]
    browser = second.processes[0]
    claimed = (
        title.format(
            joined=" ".join(browser.argv),
            proc_self_exe=acceptance_module.CHROMIUM_ZYGOTE_ARGV0,
            tail=" ".join(browser.argv[1:]),
        ),
    )
    processes = tuple(
        replace(process, raw_argv=claimed) if process.role == "browser" else process
        for process in second.processes
    )
    fixture["observations"] = (first, replace(second, processes=processes))

    with pytest.raises(
        NativeAcceptanceError,
        match="raw process arguments differ from canonical arguments",
    ):
        _verify(fixture)


def test_final_verifier_rejects_browser_canonical_argv_from_proc_self_exe(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    browser = fixture["observations"][0].processes[0]
    canonical = (acceptance_module.CHROMIUM_ZYGOTE_ARGV0, *browser.argv[1:])

    with pytest.raises(
        NativeAcceptanceError,
        match="process role differs from canonical type arguments",
    ):
        acceptance_module._verify_raw_process_argv(
            replace(browser, argv=canonical, raw_argv=canonical)
        )


def test_final_verifier_rejects_browser_observed_only_as_a_compact_title(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    browser = fixture["observations"][0].processes[0]
    compacted = (" ".join(browser.argv),)

    with pytest.raises(
        NativeAcceptanceError,
        match="process role differs from canonical type arguments",
    ):
        acceptance_module._verify_raw_process_argv(
            replace(browser, argv=compacted, raw_argv=compacted)
        )

    observations = tuple(
        replace(
            observation,
            processes=tuple(
                replace(process, argv=compacted, raw_argv=compacted)
                if process.role == "browser"
                else process
                for process in observation.processes
            ),
        )
        for observation in fixture["observations"]
    )
    fixture["observations"] = observations

    with pytest.raises(
        NativeAcceptanceError,
        match="browser argv does not name the installed launcher",
    ):
        _verify(fixture)


def test_final_verifier_rejects_first_observed_compacted_helper_with_hidden_switch(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    first = fixture["observations"][0]
    processes = tuple(
        replace(
            process,
            role="helper",
            argv=(f"{process.executable} --type=gpu-process --no-sandbox",),
            raw_argv=(f"{process.executable} --type=gpu-process --no-sandbox",),
        )
        if process.role == "gpu"
        else process
        for process in first.processes
    )
    fixture["observations"] = (
        replace(first, processes=processes),
        fixture["observations"][1],
    )

    with pytest.raises(
        NativeAcceptanceError,
        match="Electron canonical argv does not name its executable",
    ):
        _verify(fixture)


def _fixture_with_zygote_forked_utility(
    tmp_path: Path,
    *,
    argv0: str,
    later_raw: tuple[str, ...] | None,
) -> dict[str, Any]:
    """Add one zygote-forked network-service utility to both fixture runs."""

    fixture = _fixture(tmp_path)
    first, second = fixture["observations"]
    zygote = first.processes[1]
    canonical = (argv0, *ATTEMPT_SIX_UTILITY_TAIL)
    base = replace(
        zygote,
        pid=105,
        ppid=zygote.pid,
        start_time_ticks=1050,
        role="utility",
        argv=canonical,
        raw_argv=canonical,
        sandbox=SandboxStatus(1, 2, 1),
    )
    later = base if later_raw is None else replace(base, raw_argv=later_raw)
    fixture["observations"] = (
        replace(first, processes=(*first.processes, base)),
        replace(second, processes=(*second.processes, later)),
    )
    return fixture


def _compacted_title(executable: str, tail: tuple[str, ...]) -> tuple[str, ...]:
    return (" ".join((executable, *tail)),)


def test_final_verifier_accepts_zygote_forked_utility_compaction_end_to_end(
    tmp_path: Path,
) -> None:
    reference = _fixture(tmp_path)
    executable = reference["observations"][0].processes[1].executable
    fixture = _fixture_with_zygote_forked_utility(
        tmp_path,
        argv0=acceptance_module.CHROMIUM_ZYGOTE_ARGV0,
        later_raw=_compacted_title(executable, ATTEMPT_SIX_UTILITY_TAIL),
    )

    assert _verify(fixture).validation == "controlled-fixture-structural-only"


def test_final_verifier_accepts_zygote_forked_gpu_compaction_end_to_end(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    tail = ("--type=gpu-process", "--gpu-preferences=value with space")
    observations = []
    for index, observation in enumerate(fixture["observations"]):
        processes = tuple(
            replace(
                process,
                argv=(acceptance_module.CHROMIUM_ZYGOTE_ARGV0, *tail),
                raw_argv=(
                    _compacted_title(process.executable, tail)
                    if index
                    else (acceptance_module.CHROMIUM_ZYGOTE_ARGV0, *tail)
                ),
            )
            if process.role == "gpu"
            else process
            for process in observation.processes
        )
        observations.append(replace(observation, processes=processes))
    fixture["observations"] = tuple(observations)

    assert _verify(fixture).validation == "controlled-fixture-structural-only"


def test_final_verifier_rejects_compaction_whose_leading_path_is_not_the_executable(
    tmp_path: Path,
) -> None:
    fixture = _fixture_with_zygote_forked_utility(
        tmp_path,
        argv0=acceptance_module.CHROMIUM_ZYGOTE_ARGV0,
        later_raw=_compacted_title(
            acceptance_module.CHROMIUM_ZYGOTE_ARGV0, ATTEMPT_SIX_UTILITY_TAIL
        ),
    )

    with pytest.raises(
        NativeAcceptanceError,
        match="raw process arguments differ from canonical arguments",
    ):
        _verify(fixture)


def test_final_verifier_rejects_zygote_forked_utility_with_unknown_argv0(
    tmp_path: Path,
) -> None:
    fixture = _fixture_with_zygote_forked_utility(
        tmp_path,
        argv0="/proc/self/exe.bak",
        later_raw=None,
    )

    with pytest.raises(
        NativeAcceptanceError,
        match="process role differs from canonical type arguments",
    ):
        _verify(fixture)


def test_final_verifier_rejects_utility_observed_only_as_a_compact_title(
    tmp_path: Path,
) -> None:
    reference = _fixture(tmp_path)
    executable = reference["observations"][0].processes[1].executable
    compacted = _compacted_title(executable, ATTEMPT_SIX_UTILITY_TAIL)
    fixture = _fixture_with_zygote_forked_utility(
        tmp_path,
        argv0=compacted[0],
        later_raw=None,
    )
    observations = tuple(
        replace(
            observation,
            processes=tuple(
                replace(process, argv=compacted, raw_argv=compacted)
                if process.role == "utility"
                else process
                for process in observation.processes
            ),
        )
        for observation in fixture["observations"]
    )
    fixture["observations"] = observations

    with pytest.raises(
        NativeAcceptanceError,
        match="process role differs from canonical type arguments",
    ):
        _verify(fixture)


def test_rejects_editable_or_wrong_wheel_core(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    editable = replace(fixture["policy"].core, editable=True)
    fixture["policy"] = replace(fixture["policy"], core=editable)
    with pytest.raises(NativeAcceptanceError, match="editable"):
        _verify(fixture)


def test_rejects_static_generated_and_distribution_core_tampering(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    core = fixture["policy"].core
    (Path(core.package_root) / "__init__.py").write_bytes(b"substituted source\n")
    with pytest.raises(NativeAcceptanceError, match="installed core member"):
        capture_core_snapshot(core)

    fixture = _fixture(tmp_path / "generated")
    core = fixture["policy"].core
    generated = Path(core.package_root) / core.generated_members[0][0]
    generated.write_bytes(b"substituted bytecode")
    with pytest.raises(NativeAcceptanceError, match="generated core member"):
        capture_core_snapshot(core)

    fixture = _fixture(tmp_path / "extra")
    core = fixture["policy"].core
    _write(Path(core.package_root) / "__pycache__/extra.pyc", b"extra", 0o644)
    with pytest.raises(NativeAcceptanceError, match="member set changed"):
        capture_core_snapshot(core)

    fixture = _fixture(tmp_path / "distribution")
    core = fixture["policy"].core
    (Path(core.distribution_path) / "METADATA").write_bytes(b"Name: other\n")
    with pytest.raises(NativeAcceptanceError, match="distribution member"):
        capture_core_snapshot(core)

    fixture = _fixture(tmp_path / "wheel")
    direct_url = {
        "archive_info": {"hashes": {"sha256": "9" * 64}},
        "url": "file:///other.whl",
    }
    wrong = replace(fixture["policy"].core, direct_url=direct_url)
    fixture["policy"] = replace(fixture["policy"], core=wrong)
    with pytest.raises(NativeAcceptanceError, match="digest disagrees"):
        _verify(fixture)


def test_rejects_incomplete_repeats_changed_output_and_payload(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["observations"] = fixture["observations"][:1]
    with pytest.raises(NativeAcceptanceError, match="count is incomplete"):
        _verify(fixture)

    fixture = _fixture(tmp_path / "output")
    screenshot = fixture["evidence"] / fixture["policy"].runs[0].screenshot_paths[0]
    screenshot.write_bytes(_png(900, 700))
    with pytest.raises(NativeAcceptanceError, match="dimensions differ from policy"):
        _verify(fixture)

    fixture = _fixture(tmp_path / "after")
    bad_snapshot = replace(
        fixture["initial"], root_inode=fixture["initial"].root_inode + 1
    )
    fixture["observations"] = (
        replace(fixture["observations"][0], payload_after=bad_snapshot),
        fixture["observations"][1],
    )
    with pytest.raises(NativeAcceptanceError, match="changed during native run"):
        _verify(fixture)


def test_guard_requires_cgroup_and_heap_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = tmp_path / "proc-cgroup"
    root = tmp_path / "cgroup"
    scope = root / "user.slice/native.scope"
    scope.mkdir(parents=True)
    proc.write_text("0::/user.slice/native.scope\n", encoding="utf-8")
    (scope / "memory.max").write_text(str(1024 * 1024 * 1024), encoding="ascii")
    (scope / "memory.swap.max").write_text("0", encoding="ascii")
    (scope / "pids.max").write_text("64", encoding="ascii")
    monkeypatch.setenv("NODE_OPTIONS", "--max-old-space-size=512")
    assert verify_transient_guard(proc_cgroup=proc, cgroup_root=root) == scope

    (scope / "memory.max").write_text("max", encoding="ascii")
    with pytest.raises(NativeAcceptanceError, match="unbounded"):
        verify_transient_guard(proc_cgroup=proc, cgroup_root=root)
    (scope / "memory.max").write_text(str(1024 * 1024 * 1024), encoding="ascii")
    monkeypatch.setenv("NODE_OPTIONS", "--max-old-space-size=1024")
    with pytest.raises(NativeAcceptanceError, match="512 MiB"):
        verify_transient_guard(proc_cgroup=proc, cgroup_root=root)


def _rewrite_receipt(fixture: dict[str, Any], mutate: Any) -> None:
    path = fixture["evidence"] / "artifact-receipt.json"
    receipt = json.loads(path.read_bytes())
    mutate(receipt)
    document = (
        json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    _write(path, document, 0o600)
    values = tuple(
        replace(item, size=len(document), sha256=_sha256(document))
        if item.role == "desktop-artifact-receipt"
        else item
        for item in fixture["policy"].prior_inputs
    )
    fixture["policy"] = replace(fixture["policy"], prior_inputs=values)


def test_receipt_requires_success_execution_and_candidate_files(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _rewrite_receipt(fixture, lambda value: value.update(result="failure"))
    with pytest.raises(NativeAcceptanceError, match="does not report success"):
        verify_prior_inputs(fixture["evidence"], fixture["policy"])

    fixture = _fixture(tmp_path / "execution")
    _rewrite_receipt(
        fixture,
        lambda value: value["execution"].update(run_id="99999"),
    )
    with pytest.raises(NativeAcceptanceError, match="receipt or its staged files"):
        verify_prior_inputs(fixture["evidence"], fixture["policy"])

    fixture = _fixture(tmp_path / "staged")
    (fixture["evidence"] / "desktop-install.json").write_bytes(b"substituted")
    with pytest.raises(NativeAcceptanceError, match="differs from consumer identity"):
        verify_prior_inputs(fixture["evidence"], fixture["policy"])


def test_png_requires_complete_crc_bound_image(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    run = fixture["policy"].runs[0]
    screenshot = fixture["evidence"] / run.screenshot_paths[0]

    screenshot.write_bytes(_png()[:33])
    changed = replace(
        fixture["observations"][0],
        outputs=capture_expected_outputs(
            fixture["evidence"], run, fixture["policy"].evidence_uid
        ),
    )
    fixture["observations"] = (changed, fixture["observations"][1])
    with pytest.raises(NativeAcceptanceError, match="complete PNG"):
        _verify(fixture)

    fixture = _fixture(tmp_path / "crc")
    run = fixture["policy"].runs[0]
    screenshot = fixture["evidence"] / run.screenshot_paths[0]
    document = bytearray(_png())
    document[-1] ^= 1
    screenshot.write_bytes(document)
    changed = replace(
        fixture["observations"][0],
        outputs=capture_expected_outputs(
            fixture["evidence"], run, fixture["policy"].evidence_uid
        ),
    )
    fixture["observations"] = (changed, fixture["observations"][1])
    with pytest.raises(NativeAcceptanceError, match="checksum"):
        _verify(fixture)

    fixture = _fixture(tmp_path / "trailing")
    run = fixture["policy"].runs[0]
    screenshot = fixture["evidence"] / run.screenshot_paths[0]
    screenshot.write_bytes(_png() + b"trailing")
    changed = replace(
        fixture["observations"][0],
        outputs=capture_expected_outputs(
            fixture["evidence"], run, fixture["policy"].evidence_uid
        ),
    )
    fixture["observations"] = (changed, fixture["observations"][1])
    with pytest.raises(NativeAcceptanceError, match="trailing"):
        _verify(fixture)

    fixture = _fixture(tmp_path / "compressed")
    run = fixture["policy"].runs[0]
    screenshot = fixture["evidence"] / run.screenshot_paths[0]
    document = _png()
    idat = document.find(b"IDAT")
    length = struct.unpack(">I", document[idat - 4 : idat])[0]
    body = b"not-zlib" + document[idat + 4 + len(b"not-zlib") : idat + 4 + length]
    crc = struct.pack(">I", zlib.crc32(b"IDAT" + body) & 0xFFFFFFFF)
    screenshot.write_bytes(
        document[: idat + 4] + body + crc + document[idat + 8 + length :]
    )
    changed = replace(
        fixture["observations"][0],
        outputs=capture_expected_outputs(
            fixture["evidence"], run, fixture["policy"].evidence_uid
        ),
    )
    fixture["observations"] = (changed, fixture["observations"][1])
    with pytest.raises(NativeAcceptanceError, match="image data"):
        _verify(fixture)


def test_launch_environment_drops_injection_credentials_and_proxy() -> None:
    source = {
        "DISPLAY": ":1",
        "LC_ALL": "C.UTF-8",
        "PYTHONPATH": "/tmp/inject",
        "PYTHONHOME": "/tmp/python",
        "NODE_PATH": "/tmp/node",
        "ELECTRON_RUN_AS_NODE": "1",
        "ELECTRON_NO_ASAR": "1",
        "LD_PRELOAD": "/tmp/hook.so",
        "LD_LIBRARY_PATH": "/tmp/lib",
        "GH_TOKEN": "secret",
        "GITHUB_TOKEN": "secret",
        "GITLAB_TOKEN": "secret",
        "HTTPS_PROXY": "https://proxy.invalid",
        "SSL_CERT_FILE": "/tmp/cert",
    }
    result = launcher_module.native_environment(source)
    assert result == {
        "DISPLAY": ":1",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "NODE_OPTIONS": "--max-old-space-size=512",
    }


def test_installed_probe_uses_the_same_sanitized_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    core = fixture["policy"].core
    observed = {
        "requested_executable": core.requested_executable,
        "proc_executable": core.proc_executable,
        "system_python_target": core.system_python_target,
        "base_executable": core.base_executable,
        "sys_prefix": core.sys_prefix,
        "site_packages": list(core.site_packages),
        "package_root": core.package_root,
        "distribution_path": core.distribution_path,
        "tongs_version": core.tongs_version,
        "direct_url": core.direct_url,
        "editable": False,
    }
    captured: dict[str, Any] = {}

    class Completed:
        returncode = 0
        stdout = json.dumps(observed).encode()
        stderr = b""

    def run(*_args: Any, **kwargs: Any) -> Completed:
        captured.update(kwargs)
        return Completed()

    monkeypatch.setattr(launcher_module.subprocess, "run", run)
    assert launcher_module.probe_installed_core(core, fixture["policy"]) == observed
    assert captured["env"] == launcher_module.native_environment()


def test_rejects_sidecar_working_directory_and_unsafe_roots(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    first = fixture["observations"][0]
    changed_processes = tuple(
        replace(item, cwd=str(tmp_path)) if item.role == "python-sidecar" else item
        for item in first.processes
    )
    fixture["observations"] = (
        replace(first, processes=changed_processes),
        fixture["observations"][1],
    )
    with pytest.raises(NativeAcceptanceError, match="working directory"):
        _verify(fixture)

    fixture = _fixture(tmp_path / "payload-mode")
    fixture["payload"].chmod(0o777)
    with pytest.raises(NativeAcceptanceError, match="root ownership or mode"):
        capture_payload_snapshot(
            fixture["payload"],
            fixture["install"],
            expected_uid=fixture["policy"].payload_uid,
            root_mode=fixture["policy"].payload_root_mode,
            install_manifest_sha256=fixture["policy"].install_manifest_sha256,
        )

    fixture = _fixture(tmp_path / "core-mode")
    Path(fixture["policy"].core.package_root).chmod(0o777)
    with pytest.raises(NativeAcceptanceError, match="directory ownership"):
        capture_core_snapshot(fixture["policy"].core)


def test_rejects_rpm_layout_in_archive_verifier(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    install_value = json.loads(
        (FIXTURE_ROOT / "desktop-install.synthetic.json").read_bytes()
    )
    install_value.update(package_kind="rpm", ownership="system")
    install_document = (
        json.dumps(install_value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    release_value = json.loads(
        (FIXTURE_ROOT / "desktop-manifest-v1.synthetic.json").read_bytes()
    )
    release_value["artifacts"][0].update(
        name="tongs-desktop-1.2.3-fedora44-x86_64.synthetic.rpm",
        package_kind="rpm",
        ownership="system",
    )
    release_document = (
        json.dumps(release_value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    policy = replace(
        fixture["policy"],
        install_manifest_sha256=_sha256(install_document),
        release_manifest_sha256=_sha256(release_document),
        prior_inputs=tuple(
            replace(item, sha256=_sha256(install_document), size=len(install_document))
            if item.role == "desktop-install-manifest"
            else replace(
                item,
                sha256=_sha256(release_document),
                size=len(release_document),
            )
            if item.role == "desktop-release-manifest"
            else item
            for item in fixture["policy"].prior_inputs
        ),
        payload_uid=0,
        payload_root_mode=0o755,
    )

    with pytest.raises(NativeAcceptanceError, match="only per-user archive"):
        parse_bound_manifests(install_document, release_document, policy)


def test_profile_cleanup_on_success_failure_and_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    run = fixture["policy"].runs[0]
    expected = fixture["observations"][0]
    monkeypatch.setattr(
        launcher_module, "_launch_native_run", lambda **_kwargs: expected
    )
    result = launcher_module.launch_native_run(
        payload_root=fixture["payload"],
        evidence_root=fixture["evidence"],
        install=fixture["install"],
        policy=fixture["policy"],
        run=run,
        initial_payload=fixture["initial"],
        initial_core=fixture["initial_core"],
    )
    assert result == expected
    assert not (fixture["evidence"] / run.profile_path).exists()

    fixture = _fixture(tmp_path / "failure")
    run = fixture["policy"].runs[0]

    def fail(**_kwargs: Any) -> None:
        raise NativeAcceptanceError("synthetic launch failure")

    monkeypatch.setattr(launcher_module, "_launch_native_run", fail)
    with pytest.raises(NativeAcceptanceError, match="synthetic launch failure"):
        launcher_module.launch_native_run(
            payload_root=fixture["payload"],
            evidence_root=fixture["evidence"],
            install=fixture["install"],
            policy=fixture["policy"],
            run=run,
            initial_payload=fixture["initial"],
            initial_core=fixture["initial_core"],
        )
    assert not (fixture["evidence"] / run.profile_path).exists()

    fixture = _fixture(tmp_path / "replacement")
    run = fixture["policy"].runs[0]

    def replace_profile(**_kwargs: Any) -> Any:
        profile = fixture["evidence"] / run.profile_path
        profile.rmdir()
        profile.mkdir(mode=0o700)
        return fixture["observations"][0]

    monkeypatch.setattr(launcher_module, "_launch_native_run", replace_profile)
    with pytest.raises(NativeAcceptanceError, match="replacement preserved"):
        launcher_module.launch_native_run(
            payload_root=fixture["payload"],
            evidence_root=fixture["evidence"],
            install=fixture["install"],
            policy=fixture["policy"],
            run=run,
            initial_payload=fixture["initial"],
            initial_core=fixture["initial_core"],
        )
    assert (fixture["evidence"] / run.profile_path).is_dir()


def test_profile_setup_failures_close_and_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    run = fixture["policy"].runs[0]
    original_open = launcher_module.os.open
    open_calls = 0

    def fail_open_once(*args: Any, **kwargs: Any) -> int:
        nonlocal open_calls
        open_calls += 1
        if open_calls == 1:
            raise OSError("synthetic open failure")
        return original_open(*args, **kwargs)

    monkeypatch.setattr(launcher_module.os, "open", fail_open_once)
    with pytest.raises(NativeAcceptanceError, match="descriptor setup failed"):
        launcher_module.launch_native_run(
            payload_root=fixture["payload"],
            evidence_root=fixture["evidence"],
            install=fixture["install"],
            policy=fixture["policy"],
            run=run,
            initial_payload=fixture["initial"],
            initial_core=fixture["initial_core"],
        )
    assert not (fixture["evidence"] / run.profile_path).exists()

    fixture = _fixture(tmp_path / "fstat")
    run = fixture["policy"].runs[0]
    original_fstat = launcher_module.os.fstat
    opened: list[int] = []
    fstat_calls = 0

    def record_open(*args: Any, **kwargs: Any) -> int:
        descriptor = original_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def fail_fstat_once(descriptor: int) -> os.stat_result:
        nonlocal fstat_calls
        fstat_calls += 1
        if fstat_calls == 1:
            raise OSError("synthetic fstat failure")
        return original_fstat(descriptor)

    monkeypatch.setattr(launcher_module.os, "open", record_open)
    monkeypatch.setattr(
        launcher_module.os,
        "fstat",
        fail_fstat_once,
    )
    with pytest.raises(NativeAcceptanceError, match="descriptor setup failed"):
        launcher_module.launch_native_run(
            payload_root=fixture["payload"],
            evidence_root=fixture["evidence"],
            install=fixture["install"],
            policy=fixture["policy"],
            run=run,
            initial_payload=fixture["initial"],
            initial_core=fixture["initial_core"],
        )
    assert not (fixture["evidence"] / run.profile_path).exists()
    assert opened
    with pytest.raises(OSError):
        original_fstat(opened[0])


def test_process_identity_and_group_states_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    observed = fixture["observations"][0].processes[0]
    observations = {observed.pid: observed}

    monkeypatch.setattr(launcher_module.os, "killpg", lambda _pgid, _signal: None)
    monkeypatch.setattr(launcher_module, "_same_process", lambda _value: True)
    assert launcher_module._owned_group_state(observed.pid, observations) == "owned"
    monkeypatch.setattr(launcher_module, "_same_process", lambda _value: False)
    assert launcher_module._owned_group_state(observed.pid, observations) == "unknown"

    def absent(_pgid: int, _signal: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(launcher_module.os, "killpg", absent)
    assert launcher_module._owned_group_state(observed.pid, observations) == "absent"

    replacement = replace(observed, start_time_ticks=observed.start_time_ticks + 1)
    monkeypatch.setattr(
        launcher_module, "_observe_process", lambda _pid, _root: replacement
    )
    monkeypatch.setattr(launcher_module, "_child_pids", lambda _pid: ())
    with pytest.raises(NativeAcceptanceError, match="PID identity changed") as raised:
        launcher_module._collect_owned_tree(observed.pid, observations)
    for label in ("pid=", "start=", "pgid=", "ppid="):
        assert label in str(raised.value)

    replacement = replace(observed, process_group=observed.process_group + 1)
    monkeypatch.setattr(
        launcher_module, "_observe_process", lambda _pid, _root: replacement
    )
    with pytest.raises(NativeAcceptanceError, match="PID identity changed"):
        launcher_module._collect_owned_tree(observed.pid, observations)

    replacement = replace(observed, ppid=observed.ppid + 1)
    monkeypatch.setattr(
        launcher_module, "_observe_process", lambda _pid, _root: replacement
    )
    with pytest.raises(NativeAcceptanceError, match="PID identity changed"):
        launcher_module._collect_owned_tree(observed.pid, observations)

    class RunningProcess:
        pid = observed.pid

        def poll(self) -> None:
            return None

    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        launcher_module,
        "_owned_group_state",
        lambda _group, _values: "unknown",
    )
    monkeypatch.setattr(
        launcher_module.os,
        "killpg",
        lambda group, sent_signal: signals.append((group, sent_signal)),
    )
    with pytest.raises(
        NativeAcceptanceError, match="without a matching owned identity"
    ):
        launcher_module._stop_owned_group(RunningProcess(), observations)
    assert signals == []


@pytest.mark.parametrize(
    ("role", "process_type"),
    [
        ("zygote", "zygote"),
        ("gpu", "gpu-process"),
        ("renderer", "renderer"),
        ("utility", "utility"),
    ],
)
def test_process_refresh_normalizes_exact_chromium_argv_storage_compaction(
    tmp_path: Path, role: str, process_type: str
) -> None:
    fixture = _fixture(tmp_path)
    browser, zygote, *_rest = fixture["observations"][0].processes
    executable = zygote.executable
    if role == "utility":
        executable = f"{executable} with space"
    canonical = (executable, f"--type={process_type}", "--retained=value with space")
    previous = replace(
        zygote,
        role=role,
        executable=executable,
        argv=canonical,
        raw_argv=canonical,
    )
    compacted_raw = (" ".join(canonical),)
    compacted = replace(
        previous,
        role="helper",
        argv=compacted_raw,
        raw_argv=compacted_raw,
    )

    normalized = launcher_module._validate_process_refresh(
        previous,
        compacted,
        {browser.pid: browser, previous.pid: previous},
    )

    assert normalized.role == role
    assert normalized.argv == canonical
    assert normalized.raw_argv == compacted_raw
    assert normalized.executable == executable
    assert (
        normalized.pid,
        normalized.start_time_ticks,
        normalized.process_group,
        normalized.ppid,
    ) == (
        previous.pid,
        previous.start_time_ticks,
        previous.process_group,
        previous.ppid,
    )


# Exact canonical tail of the network-service utility process from failed native
# attempt 6 (`.worktrees/evidence/desktop-125-ac90ce9-local-inputs/
# failed-native-attempt-6.md`), trimmed to the arguments the journal recorded in
# full. Chromium forked it from the zygote, so canonical argv[0] is the literal
# `/proc/self/exe` while the later compacted title starts with the resolved
# executable.
ATTEMPT_SIX_UTILITY_TAIL = (
    "--type=utility",
    "--utility-sub-type=network.mojom.NetworkService",
    "--lang=en-US",
    "--service-sandbox-type=none",
    "--enable-crash-reporter=4c35a100-cf37-40d1-9460-3f34de5e5ea6,no_channel",
    "--standard-schemes=tongs",
    "--shared-files=v8_context_snapshot_data:100",
    "--field-trial-handle=3,i,16729955238146300512,10406605718793136162,262144",
    "--variations-seed-version",
)


def _zygote_forked_pair(
    tmp_path: Path, role: str, tail: tuple[str, ...]
) -> tuple[ProcessObservation, ProcessObservation, dict[int, ProcessObservation]]:
    """Build one canonical observation and its exact Chromium compaction."""

    fixture = _fixture(tmp_path)
    browser, zygote, *_rest = fixture["observations"][0].processes
    canonical = (acceptance_module.CHROMIUM_ZYGOTE_ARGV0, *tail)
    previous = replace(zygote, role=role, argv=canonical, raw_argv=canonical)
    compacted = (" ".join((previous.executable, *tail)),)
    current = replace(previous, role="helper", argv=compacted, raw_argv=compacted)
    return previous, current, {browser.pid: browser, previous.pid: previous}


def test_process_refresh_carries_utility_role_across_attempt_six_compaction(
    tmp_path: Path,
) -> None:
    previous, current, observations = _zygote_forked_pair(
        tmp_path, "utility", ATTEMPT_SIX_UTILITY_TAIL
    )

    normalized = launcher_module._validate_process_refresh(
        previous, current, observations
    )

    assert normalized.role == "utility"
    assert normalized.argv == previous.argv
    assert normalized.argv[0] == acceptance_module.CHROMIUM_ZYGOTE_ARGV0
    assert normalized.raw_argv == current.argv
    assert normalized.executable == previous.executable
    assert (
        normalized.pid,
        normalized.start_time_ticks,
        normalized.process_group,
        normalized.ppid,
    ) == (
        previous.pid,
        previous.start_time_ticks,
        previous.process_group,
        previous.ppid,
    )


def test_process_refresh_carries_gpu_role_across_zygote_forked_compaction(
    tmp_path: Path,
) -> None:
    previous, current, observations = _zygote_forked_pair(
        tmp_path, "gpu", ("--type=gpu-process", "--gpu-preferences=value with space")
    )

    normalized = launcher_module._validate_process_refresh(
        previous, current, observations
    )

    assert normalized.role == "gpu"
    assert normalized.argv == previous.argv
    assert normalized.raw_argv == current.argv


@pytest.mark.parametrize(
    "compacted",
    [
        "{exe} --type=utility",
        "{exe} --type=utility --lang=en-US --extra",
        "{exe} --type=utility --lang=en-GB",
        "{exe} --type=renderer --lang=en-US",
        "{argv0} --type=utility --lang=en-US",
        "{exe}  --type=utility --lang=en-US",
        "{exe}\t--type=utility --lang=en-US",
        "--type=utility --lang=en-US {exe}",
        "{exe} --type=utility --lang=en-US ",
    ],
)
def test_process_refresh_rejects_compaction_differing_by_any_token(
    tmp_path: Path, compacted: str
) -> None:
    previous, _current, observations = _zygote_forked_pair(
        tmp_path, "utility", ("--type=utility", "--lang=en-US")
    )
    claimed = (
        compacted.format(
            exe=previous.executable, argv0=acceptance_module.CHROMIUM_ZYGOTE_ARGV0
        ),
    )
    current = replace(previous, role="helper", argv=claimed, raw_argv=claimed)

    with pytest.raises(NativeAcceptanceError, match="PID identity changed"):
        launcher_module._validate_process_refresh(previous, current, observations)


@pytest.mark.parametrize(
    "change",
    [
        {"pid": 909},
        {"start_time_ticks": 9090},
        {"process_group": 909},
        {"ppid": 909},
        {"executable": "/usr/bin/false"},
    ],
)
def test_process_refresh_rejects_compaction_with_changed_kernel_identity(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    previous, current, observations = _zygote_forked_pair(
        tmp_path, "utility", ATTEMPT_SIX_UTILITY_TAIL
    )

    with pytest.raises(NativeAcceptanceError, match="PID identity changed"):
        launcher_module._validate_process_refresh(
            previous, replace(current, **change), observations
        )


def test_process_refresh_does_not_promote_a_compact_first_observation(
    tmp_path: Path,
) -> None:
    previous, current, observations = _zygote_forked_pair(
        tmp_path, "utility", ATTEMPT_SIX_UTILITY_TAIL
    )
    compact_first = replace(
        previous, role="helper", argv=current.argv, raw_argv=current.argv
    )

    unchanged = launcher_module._validate_process_refresh(
        compact_first, compact_first, observations
    )

    assert unchanged.role == "helper"
    assert unchanged.argv == current.argv

    with pytest.raises(NativeAcceptanceError, match="PID identity changed"):
        launcher_module._validate_process_refresh(compact_first, previous, observations)


def _command_line_permuted_title(process: ProcessObservation) -> str:
    """Mirror ``base::CommandLine::argv()`` order: program, switches, arguments.

    See ``.worktrees/desktop-125-attempt8-analysis.md`` and the retained
    ``command_line.cc`` lines 433 to 464 and 640 to 665.
    """

    switches = []
    arguments = []
    parse_switches = True
    for token in process.argv[1:]:
        parse_switches &= token != "--"
        prefix = 2 if token.startswith("--") else (1 if token.startswith("-") else 0)
        if parse_switches and prefix and prefix != len(token):
            switches.append(token)
        else:
            arguments.append(token)
    return " ".join((process.executable, *switches, *arguments))


def test_process_refresh_accepts_the_attempt_nine_permuted_browser_title(
    tmp_path: Path,
) -> None:
    """Confirmed native attempt 9: switches first, then positional arguments."""

    fixture = _fixture(tmp_path)
    browser = fixture["observations"][0].processes[0]
    permuted = (_command_line_permuted_title(browser),)
    plain = " ".join((browser.executable, *browser.argv[1:]))
    assert permuted[0] != plain
    assert len(permuted[0]) == len(plain)
    assert sorted(permuted[0].split(" ")) == sorted(plain.split(" "))

    normalized = launcher_module._validate_process_refresh(
        browser,
        replace(browser, argv=permuted, raw_argv=permuted),
        {},
    )

    assert normalized.role == "browser"
    assert normalized.argv == browser.argv
    assert normalized.raw_argv == permuted


def test_final_verifier_accepts_the_attempt_nine_permuted_browser_title(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    first, second = fixture["observations"]
    processes = tuple(
        replace(process, raw_argv=(_command_line_permuted_title(process),))
        if process.role == "browser"
        else process
        for process in second.processes
    )
    fixture["observations"] = (first, replace(second, processes=processes))

    assert _verify(fixture).validation == "controlled-fixture-structural-only"


def test_process_refresh_accepts_the_identity_permutation_for_a_switch_only_argv(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    browser = fixture["observations"][0].processes[0]
    canonical = (browser.executable, "--ozone-platform=x11", "--user-data-dir=/tmp/p")
    previous = replace(browser, argv=canonical, raw_argv=canonical)
    plain = (" ".join(canonical),)
    assert _command_line_permuted_title(previous) == plain[0]

    normalized = launcher_module._validate_process_refresh(
        previous, replace(previous, argv=plain, raw_argv=plain), {}
    )

    assert normalized.argv == canonical
    assert normalized.raw_argv == plain


def test_process_refresh_accepts_either_deterministic_browser_title(
    tmp_path: Path,
) -> None:
    """Both titles are functions of the same validated canonical argv."""

    fixture = _fixture(tmp_path)
    browser = fixture["observations"][0].processes[0]
    for title in (
        " ".join((browser.executable, *browser.argv[1:])),
        _command_line_permuted_title(browser),
    ):
        claimed = (title,)
        normalized = launcher_module._validate_process_refresh(
            browser, replace(browser, argv=claimed, raw_argv=claimed), {}
        )
        assert normalized.argv == browser.argv


@pytest.mark.parametrize(
    "mutate",
    [
        "swap_switches",
        "swap_arguments",
        "drop_switch",
        "drop_argument",
        "duplicate_switch",
        "alter_argument",
        "arguments_before_switches",
    ],
)
def test_process_refresh_rejects_a_mistaken_browser_permutation(
    tmp_path: Path, mutate: str
) -> None:
    fixture = _fixture(tmp_path)
    browser = fixture["observations"][0].processes[0]
    switches = [token for token in browser.argv[1:] if token.startswith("--")]
    arguments = [token for token in browser.argv[1:] if not token.startswith("--")]
    if mutate == "swap_switches":
        switches[0], switches[1] = switches[1], switches[0]
    elif mutate == "swap_arguments":
        arguments[0], arguments[1] = arguments[1], arguments[0]
    elif mutate == "drop_switch":
        switches.pop()
    elif mutate == "drop_argument":
        arguments.pop()
    elif mutate == "duplicate_switch":
        switches.append(switches[0])
    elif mutate == "alter_argument":
        arguments[0] = f"{arguments[0]}x"
    else:
        switches, arguments = arguments, switches
    claimed = (" ".join((browser.executable, *switches, *arguments)),)

    with pytest.raises(NativeAcceptanceError, match="PID identity changed"):
        launcher_module._validate_process_refresh(
            browser, replace(browser, argv=claimed, raw_argv=claimed), {}
        )


def test_process_refresh_keeps_tokens_after_a_switch_terminator_as_arguments(
    tmp_path: Path,
) -> None:
    """A bare ``--`` stops switch parsing, mirroring command_line.cc 640-660."""

    fixture = _fixture(tmp_path)
    browser = fixture["observations"][0].processes[0]
    canonical = (browser.executable, "argument", "--switch", "--", "--after")
    previous = replace(browser, argv=canonical, raw_argv=canonical)
    accepted = (f"{browser.executable} --switch argument -- --after",)
    hoisted = (f"{browser.executable} --switch --after argument --",)

    normalized = launcher_module._validate_process_refresh(
        previous, replace(previous, argv=accepted, raw_argv=accepted), {}
    )
    assert normalized.argv == canonical

    with pytest.raises(NativeAcceptanceError, match="PID identity changed"):
        launcher_module._validate_process_refresh(
            previous, replace(previous, argv=hoisted, raw_argv=hoisted), {}
        )


@pytest.mark.parametrize(
    ("canonical_tail", "title_tail"),
    [
        (("argument", "-x"), ("-x", "argument")),
        (("argument", "-"), ("argument", "-")),
    ],
)
def test_process_refresh_mirrors_the_posix_switch_prefixes(
    tmp_path: Path,
    canonical_tail: tuple[str, ...],
    title_tail: tuple[str, ...],
) -> None:
    """``-x`` is a switch and a bare ``-`` is not, per command_line.cc 60-65."""

    fixture = _fixture(tmp_path)
    browser = fixture["observations"][0].processes[0]
    canonical = (browser.executable, *canonical_tail)
    previous = replace(browser, argv=canonical, raw_argv=canonical)
    claimed = (" ".join((browser.executable, *title_tail)),)

    normalized = launcher_module._validate_process_refresh(
        previous, replace(previous, argv=claimed, raw_argv=claimed), {}
    )

    assert normalized.argv == canonical


def test_child_roles_do_not_accept_the_permutation(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    zygote = fixture["observations"][0].processes[1]
    canonical = (zygote.executable, "argument", "--type=zygote")
    previous = replace(zygote, argv=canonical, raw_argv=canonical)
    permuted = (f"{zygote.executable} --type=zygote argument",)

    with pytest.raises(NativeAcceptanceError, match="PID identity changed"):
        launcher_module._validate_process_refresh(
            previous,
            replace(previous, role="helper", argv=permuted, raw_argv=permuted),
            {},
        )


def test_final_verifier_rejects_a_compact_first_permuted_browser_title(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    browser = fixture["observations"][0].processes[0]
    permuted = (_command_line_permuted_title(browser),)

    with pytest.raises(
        NativeAcceptanceError,
        match="process role differs from canonical type arguments",
    ):
        acceptance_module._verify_raw_process_argv(
            replace(browser, argv=permuted, raw_argv=permuted)
        )


def test_refresh_rejection_reports_both_expected_browser_titles(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    browser = fixture["observations"][0].processes[0]
    permuted = _command_line_permuted_title(browser)
    plain = " ".join((browser.executable, *browser.argv[1:]))
    claimed = (f"{permuted}x",)

    with pytest.raises(NativeAcceptanceError) as raised:
        launcher_module._validate_process_refresh(
            browser, replace(browser, argv=claimed, raw_argv=claimed), {}
        )

    message = str(raised.value)
    assert "precondition='argv join equality'" in message
    assert "current_fields=1" in message
    assert f"compact_len={len(claimed[0].encode())}" in message
    assert f"expected_len={len(plain.encode())}" in message
    assert f"command_line_expected_len={len(permuted.encode())}" in message
    assert "expected_command_line=" in message
    assert "command_line_first_difference=" in message
    assert "command_line_compact_window=" in message
    assert "command_line_expected_window=" in message
    assert plain in message
    assert permuted in message


@pytest.mark.parametrize(
    ("previous_change", "current_change", "precondition"),
    [
        ({}, {"executable": "/usr/bin/false"}, "executable equality"),
        ({}, {"role": "helper"}, "role transition"),
        (
            {"argv": (acceptance_module.CHROMIUM_ZYGOTE_ARGV0,)},
            {},
            "previous argv[0] is not the executable",
        ),
        ({}, {"raw_argv": ("other",)}, "current raw_argv consistency"),
        ({"raw_argv": ("arbitrary prior raw",)}, {}, "previous raw_argv consistency"),
    ],
)
def test_refresh_rejection_names_each_failed_browser_precondition(
    tmp_path: Path,
    previous_change: dict[str, Any],
    current_change: dict[str, Any],
    precondition: str,
) -> None:
    fixture = _fixture(tmp_path)
    browser = fixture["observations"][0].processes[0]
    previous = replace(browser, **previous_change)
    compacted = (" ".join(previous.argv),)
    current_values: dict[str, Any] = {"argv": compacted, "raw_argv": compacted}
    current_values.update(current_change)
    current = replace(previous, **current_values)

    with pytest.raises(NativeAcceptanceError) as raised:
        launcher_module._validate_process_refresh(previous, current, {})

    assert f"precondition={precondition!r}" in str(raised.value)


def test_refresh_rejection_names_a_failed_child_type_token_precondition(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    zygote = fixture["observations"][0].processes[1]
    canonical = (zygote.executable, "--type=zygote", "--type=renderer")
    previous = replace(zygote, argv=canonical, raw_argv=canonical)
    compacted = (" ".join(canonical),)

    with pytest.raises(NativeAcceptanceError) as raised:
        launcher_module._validate_process_refresh(
            previous,
            replace(previous, role="helper", argv=compacted, raw_argv=compacted),
            {},
        )

    assert "precondition='--type token count'" in str(raised.value)


def test_refresh_rejection_bounds_every_diagnostic_value(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    browser = fixture["observations"][0].processes[0]
    oversized = (
        " ".join(browser.argv) + "x" * (acceptance_module.MAX_ARGUMENT_BYTES + 64),
    )

    with pytest.raises(NativeAcceptanceError) as raised:
        launcher_module._validate_process_refresh(
            browser,
            replace(browser, argv=oversized, raw_argv=oversized),
            {},
        )

    message = str(raised.value)
    assert "more bytes)" in message
    assert oversized[0] not in message
    assert "x" * acceptance_module.MAX_ARGUMENT_BYTES not in message


def test_refresh_rejection_keeps_the_generic_listing_for_other_changes(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    browser = fixture["observations"][0].processes[0]
    changed = (*browser.argv, "--extra")

    with pytest.raises(NativeAcceptanceError) as raised:
        launcher_module._validate_process_refresh(
            browser,
            replace(browser, argv=changed, raw_argv=changed),
            {},
        )

    message = str(raised.value)
    assert "precondition=" not in message
    assert "owned process PID identity changed" in message


def test_refresh_rejection_reports_a_changed_kernel_identity(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    browser = fixture["observations"][0].processes[0]
    compacted = (" ".join(browser.argv),)

    with pytest.raises(NativeAcceptanceError) as raised:
        launcher_module._validate_process_refresh(
            browser,
            replace(browser, start_time_ticks=99, argv=compacted, raw_argv=compacted),
            {},
        )

    assert "precondition='kernel identity'" in str(raised.value)


def test_process_refresh_carries_browser_role_across_attempt_seven_compaction(
    tmp_path: Path,
) -> None:
    """Failed native attempt 7: the browser argv storage compacted in place."""

    fixture = _fixture(tmp_path)
    browser = fixture["observations"][0].processes[0]
    compacted = (" ".join(browser.argv),)
    current = replace(browser, argv=compacted, raw_argv=compacted)

    normalized = launcher_module._validate_process_refresh(
        browser, current, {browser.pid: browser}
    )

    assert normalized.role == "browser"
    assert normalized.argv == browser.argv
    assert normalized.argv[0] == browser.executable
    assert normalized.raw_argv == compacted
    assert (
        normalized.pid,
        normalized.start_time_ticks,
        normalized.process_group,
        normalized.ppid,
    ) == (
        browser.pid,
        browser.start_time_ticks,
        browser.process_group,
        browser.ppid,
    )


@pytest.mark.parametrize(
    "title",
    [
        "{joined} --extra",
        "{joined}x",
        "{joined} ",
        " {joined}",
        "{proc_self_exe} {tail}",
    ],
)
def test_process_refresh_rejects_browser_compaction_differing_by_any_token(
    tmp_path: Path, title: str
) -> None:
    fixture = _fixture(tmp_path)
    browser = fixture["observations"][0].processes[0]
    claimed = (
        title.format(
            joined=" ".join(browser.argv),
            proc_self_exe=acceptance_module.CHROMIUM_ZYGOTE_ARGV0,
            tail=" ".join(browser.argv[1:]),
        ),
    )
    current = replace(browser, argv=claimed, raw_argv=claimed)

    with pytest.raises(NativeAcceptanceError, match="PID identity changed"):
        launcher_module._validate_process_refresh(browser, current, {})


def test_process_refresh_rejects_browser_canonical_argv_from_proc_self_exe(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    browser = fixture["observations"][0].processes[0]
    canonical = (acceptance_module.CHROMIUM_ZYGOTE_ARGV0, *browser.argv[1:])
    previous = replace(browser, argv=canonical, raw_argv=canonical)
    compacted = (" ".join((previous.executable, *canonical[1:])),)

    with pytest.raises(NativeAcceptanceError, match="PID identity changed"):
        launcher_module._validate_process_refresh(
            previous,
            replace(previous, argv=compacted, raw_argv=compacted),
            {},
        )


def test_process_refresh_rejects_a_compact_first_browser_observation(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    browser = fixture["observations"][0].processes[0]
    compacted = (" ".join(browser.argv),)
    compact_first = replace(browser, argv=compacted, raw_argv=compacted)

    with pytest.raises(NativeAcceptanceError, match="PID identity changed"):
        launcher_module._validate_process_refresh(compact_first, browser, {})


def test_final_verifier_rejects_first_observed_compacted_utility_title(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    base = fixture["observations"][0].processes[1]
    compacted = (f"{base.executable} --type=utility --lang=en-US",)

    with pytest.raises(
        NativeAcceptanceError,
        match="process role differs from canonical type arguments",
    ):
        acceptance_module._verify_raw_process_argv(
            replace(base, role="utility", argv=compacted, raw_argv=compacted)
        )

    first = fixture["observations"][0]
    processes = tuple(
        replace(process, role="helper", argv=compacted, raw_argv=compacted)
        if process.role == "gpu"
        else process
        for process in first.processes
    )
    fixture["observations"] = (
        replace(first, processes=processes),
        fixture["observations"][1],
    )

    with pytest.raises(
        NativeAcceptanceError,
        match="Electron canonical argv does not name its executable",
    ):
        _verify(fixture)


@pytest.mark.parametrize(
    ("previous_change", "current_change"),
    [
        ({"role": "renderer"}, {}),
        ({}, {"role": "renderer"}),
        ({}, {"argv": "{exe}  --type=zygote"}),
        ({}, {"argv": "{exe}\t--type=zygote"}),
        ({}, {"argv": "{exe} --type=zygote --extra"}),
        ({}, {"argv": "{exe}"}),
        ({}, {"argv": "--type=zygote {exe}"}),
        (
            {
                "argv": (
                    "{exe}",
                    "--type=zygote",
                    "--type=renderer",
                )
            },
            {},
        ),
        ({}, {"executable": "/usr/bin/false"}),
    ],
)
def test_process_refresh_rejects_other_argv_mutations(
    tmp_path: Path,
    previous_change: dict[str, Any],
    current_change: dict[str, Any],
) -> None:
    fixture = _fixture(tmp_path)
    zygote = fixture["observations"][0].processes[1]
    previous_values = {
        key: (
            tuple(value.format(exe=zygote.executable) for value in item)
            if key == "argv" and isinstance(item := value, tuple)
            else value
        )
        for key, value in previous_change.items()
    }
    previous = replace(zygote, **previous_values, raw_argv=None)
    raw_value = current_change.get("argv", f"{previous.executable} --type=zygote")
    assert isinstance(raw_value, str)
    raw = (raw_value.format(exe=zygote.executable),)
    executable = current_change.get("executable", previous.executable)
    current = replace(
        previous,
        role=current_change.get("role", "helper"),
        executable=executable,
        argv=raw,
        raw_argv=raw,
    )

    with pytest.raises(NativeAcceptanceError, match="PID identity changed"):
        launcher_module._validate_process_refresh(previous, current, {})


def test_process_refresh_rejects_second_compacted_argv_mutation(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    zygote = fixture["observations"][0].processes[1]
    compacted_raw = (f"{zygote.executable} --type=zygote",)
    normalized = launcher_module._validate_process_refresh(
        zygote,
        replace(
            zygote,
            role="helper",
            argv=compacted_raw,
            raw_argv=compacted_raw,
        ),
        {},
    )
    changed_raw = (f"{zygote.executable} --type=renderer",)

    with pytest.raises(NativeAcceptanceError, match="PID identity changed"):
        launcher_module._validate_process_refresh(
            normalized,
            replace(
                normalized,
                role="helper",
                argv=changed_raw,
                raw_argv=changed_raw,
            ),
            {},
        )


def test_process_refresh_rejects_compacted_to_canonical_reversal(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    zygote = fixture["observations"][0].processes[1]
    compacted_raw = (" ".join(zygote.argv),)
    normalized = replace(zygote, raw_argv=compacted_raw)

    with pytest.raises(NativeAcceptanceError, match="PID identity changed"):
        launcher_module._validate_process_refresh(
            normalized,
            replace(zygote, raw_argv=zygote.argv),
            {},
        )


def test_process_refresh_rejects_arbitrary_prior_raw_state(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    zygote = fixture["observations"][0].processes[1]
    compacted_raw = (" ".join(zygote.argv),)
    arbitrary_previous = replace(zygote, raw_argv=("arbitrary prior raw",))

    with pytest.raises(NativeAcceptanceError, match="PID identity changed"):
        launcher_module._validate_process_refresh(
            arbitrary_previous,
            replace(
                zygote,
                role="helper",
                argv=compacted_raw,
                raw_argv=compacted_raw,
            ),
            {},
        )


def test_injected_renderer_failure_reports_bounded_process_evidence(
    tmp_path: Path,
) -> None:
    """Attempt 10: the injected PID was observed, but never with role renderer."""

    fixture = _fixture(tmp_path)
    observations = []
    for observation in fixture["observations"]:
        processes = tuple(
            replace(process, role="helper") if process.pid == 103 else process
            for process in observation.processes
        )
        observations.append(replace(observation, processes=processes))
    fixture["observations"] = tuple(observations)

    with pytest.raises(NativeAcceptanceError) as raised:
        _verify(fixture)

    message = str(raised.value)
    assert "deliberately crashed renderer lacks live process evidence" in message
    assert "observed=103,role='helper'" in message
    assert "sandbox=(1,2,1)" in message
    assert "argv_fields=" in message
    assert "roster=(" in message
    assert "103:helper" in message


def test_injected_renderer_failure_reports_an_absent_pid(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    report_path = fixture["evidence"] / fixture["policy"].runs[0].report_path
    report = json.loads(report_path.read_bytes())
    report["rendererCrashRecovery"]["injectedRendererPid"] = 4242
    _write(report_path, (json.dumps(report) + "\n").encode(), 0o600)
    fixture["observations"] = (
        replace(
            fixture["observations"][0],
            outputs=capture_expected_outputs(
                fixture["evidence"],
                fixture["policy"].runs[0],
                fixture["policy"].evidence_uid,
            ),
        ),
        fixture["observations"][1],
    )

    with pytest.raises(NativeAcceptanceError) as raised:
        _verify(fixture)

    message = str(raised.value)
    assert "deliberately crashed renderer lacks live process evidence" in message
    assert "observed=4242,absent" in message
    assert "roster=(" in message


def test_metric_pid_failure_reports_bounded_process_evidence(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    observations = []
    for observation in fixture["observations"]:
        processes = tuple(
            replace(process, role="helper") if process.role == "gpu" else process
            for process in observation.processes
        )
        observations.append(replace(observation, processes=processes))
    fixture["observations"] = tuple(observations)

    with pytest.raises(NativeAcceptanceError) as raised:
        _verify(fixture)

    message = str(raised.value)
    assert "smoke metric PID lacks owned process evidence" in message
    assert "expected_role='gpu'" in message
    assert "observed=102,role='helper'" in message
    assert "roster=(" in message


def test_collect_owned_tree_retains_a_process_observed_once() -> None:
    """A child seen in one poll stays in the evidence after it exits."""

    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    script = """
import os
import sys

ready = int(sys.argv[1])
release = int(sys.argv[2])
child = os.fork()
if child == 0:
    os.write(ready, f"{os.getpid()}\\n".encode())
    os.read(release, 1)
    os._exit(0)
os.waitpid(child, 0)
os.read(release, 1)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(ready_write), str(release_read)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        pass_fds=(ready_write, release_read),
        start_new_session=True,
    )
    os.close(ready_write)
    os.close(release_read)
    try:
        readable, _, _ = select.select((ready_read,), (), (), 2)
        assert readable
        child_pid = int(os.read(ready_read, 64).strip())
        observations: dict[int, ProcessObservation] = {}
        launcher_module._collect_owned_tree(process.pid, observations)
        assert child_pid in observations
        captured = observations[child_pid]

        os.write(release_write, b"x")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if not Path(f"/proc/{child_pid}").exists():
                break
            time.sleep(launcher_module.POLL_SECONDS)
        else:
            pytest.fail("short-lived child did not exit")

        launcher_module._collect_owned_tree(process.pid, observations)
        assert observations[child_pid] == captured
    finally:
        os.write(release_write, b"x")
        os.close(ready_read)
        os.close(release_write)
        process.wait(timeout=5)


def test_collect_owned_tree_fails_closed_on_too_many_retained_observations(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    template = fixture["observations"][0].processes[0]
    observations = {
        pid: replace(template, pid=pid)
        for pid in range(900_000, 900_000 + acceptance_module.MAX_PROCESSES)
    }
    assert len(observations) == acceptance_module.MAX_PROCESSES

    with pytest.raises(
        NativeAcceptanceError, match="retained owned process evidence exceeds its bound"
    ):
        launcher_module._collect_owned_tree(os.getpid(), observations)


def test_collect_owned_tree_accepts_one_inherited_fork_exec_transition() -> None:
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    script = """
import os
import sys

ready = int(sys.argv[1])
release = int(sys.argv[2])
child = os.fork()
if child == 0:
    os.write(ready, f"{os.getpid()}\\n".encode())
    os.read(release, 1)
    os.execv("/usr/bin/sleep", ("/usr/bin/sleep", "30"))
os.waitpid(child, 0)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(ready_write), str(release_read)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        pass_fds=(ready_write, release_read),
        start_new_session=True,
    )
    os.close(ready_write)
    os.close(release_read)
    try:
        readable, _, _ = select.select((ready_read,), (), (), 2)
        assert readable
        child_pid = int(os.read(ready_read, 64).strip())
        observations: dict[int, ProcessObservation] = {}
        launcher_module._collect_owned_tree(process.pid, observations)
        before = observations[child_pid]
        parent = observations[process.pid]
        assert before.ppid == parent.pid
        assert before.executable == parent.executable
        assert before.argv == parent.argv

        os.write(release_write, b"x")
        sleep_executable = str(Path("/usr/bin/sleep").resolve(strict=True))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                current_executable = str(
                    Path(f"/proc/{child_pid}/exe").resolve(strict=True)
                )
            except FileNotFoundError:
                current_executable = ""
            if current_executable == sleep_executable:
                break
            time.sleep(0.01)
        else:
            pytest.fail("forked child did not exec the declared test executable")

        launcher_module._collect_owned_tree(process.pid, observations)
        after = observations[child_pid]
        assert (after.pid, after.start_time_ticks, after.process_group, after.ppid) == (
            before.pid,
            before.start_time_ticks,
            before.process_group,
            before.ppid,
        )
        assert after.executable == sleep_executable
        assert after.argv == ("/usr/bin/sleep", "30")

        arbitrary = replace(after, executable="/usr/bin/false", argv=("false",))
        with pytest.raises(NativeAcceptanceError, match="PID identity changed"):
            launcher_module._validate_process_refresh(after, arbitrary, observations)
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)
        os.close(ready_read)
        os.close(release_write)


def test_core_snapshot_precedes_any_installed_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    events: list[str] = []
    payload_snapshot = fixture["initial"]
    core_snapshot = fixture["initial_core"]

    monkeypatch.setattr(
        launcher_module, "verify_transient_guard", lambda: Path("/guard")
    )
    monkeypatch.setattr(
        launcher_module,
        "parse_bound_manifests",
        lambda *_args: (fixture["install"], object()),
    )
    monkeypatch.setattr(launcher_module, "verify_prior_inputs", lambda *_args: None)

    def capture_payload(*_args: Any, **_kwargs: Any) -> Any:
        events.append("payload")
        return payload_snapshot

    def capture_core(_binding: CoreBinding) -> Any:
        events.append("core")
        return core_snapshot

    def probe(_binding: CoreBinding, _policy: NativePayloadPolicy) -> dict[str, Any]:
        assert events == ["payload", "core"]
        events.append("probe")
        return {}

    monkeypatch.setattr(launcher_module, "capture_payload_snapshot", capture_payload)
    monkeypatch.setattr(launcher_module, "capture_core_snapshot", capture_core)
    monkeypatch.setattr(launcher_module, "probe_installed_core", probe)
    monkeypatch.setattr(
        launcher_module,
        "launch_native_run",
        lambda **_kwargs: fixture["observations"][0],
    )
    expected_result = object()
    monkeypatch.setattr(
        launcher_module,
        "verify_native_acceptance",
        lambda **_kwargs: expected_result,
    )

    result, _observations = launcher_module.run_native_acceptance(
        payload_root=fixture["payload"],
        evidence_root=fixture["evidence"],
        install_document=b"install",
        release_document=b"release",
        policy=fixture["policy"],
    )
    assert result is expected_result
    assert events[:3] == ["payload", "core", "probe"]
    assert events[3] == "core"


def test_run_rejects_payload_and_evidence_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    monkeypatch.setattr(
        launcher_module, "verify_transient_guard", lambda: Path("/guard")
    )
    with pytest.raises(NativeAcceptanceError, match="must not overlap"):
        launcher_module.run_native_acceptance(
            payload_root=fixture["payload"],
            evidence_root=fixture["payload"],
            install_document=b"unused",
            release_document=b"unused",
            policy=fixture["policy"],
        )


def test_post_exit_output_bound_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    run = fixture["policy"].runs[0]
    for path in (run.report_path, *run.screenshot_paths):
        (fixture["evidence"] / path).unlink()
    (fixture["evidence"] / run.profile_path).mkdir(mode=0o700)

    class CompletedProcess:
        pid = 100

        def __init__(self, *_args: Any, stdout: Any, env: Any, **_kwargs: Any) -> None:
            assert env == launcher_module.native_environment()
            stdout.write(b"x" * (launcher_module.MAX_LOG_BYTES + 1))
            stdout.flush()

        def poll(self) -> int:
            return 0

        def wait(self, timeout: int) -> int:
            return 0

    monkeypatch.setattr(
        launcher_module, "verify_transient_guard", lambda: Path("/guard")
    )
    monkeypatch.setattr(launcher_module.subprocess, "Popen", CompletedProcess)
    monkeypatch.setattr(
        launcher_module,
        "_collect_owned_tree",
        lambda _pid, values: values.setdefault(
            100, fixture["observations"][0].processes[0]
        ),
    )
    monkeypatch.setattr(launcher_module, "_stop_owned_group", lambda *_args: None)
    with pytest.raises(NativeAcceptanceError, match="output exceeded"):
        launcher_module._launch_native_run(
            payload_root=fixture["payload"],
            evidence_root=fixture["evidence"],
            install=fixture["install"],
            policy=fixture["policy"],
            run=run,
            initial_payload=fixture["initial"],
            initial_core=fixture["initial_core"],
        )


def test_unobserved_fresh_group_escalates_before_reaping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Process:
        pid = 4321

        def wait(self, timeout: int) -> int:
            assert timeout == 2
            events.append("reap")
            return -9

    monkeypatch.setattr(
        launcher_module.os,
        "killpg",
        lambda _group, sent_signal: events.append(
            "term" if sent_signal == launcher_module.signal.SIGTERM else "kill"
        ),
    )
    monkeypatch.setattr(
        launcher_module.time, "sleep", lambda _seconds: events.append("grace")
    )
    monkeypatch.setattr(
        launcher_module,
        "_wait_raw_group_exit",
        lambda _group, _seconds: events.append("absent") or True,
    )

    launcher_module._stop_unobserved_new_group(Process())

    assert events == ["term", "grace", "kill", "reap", "absent"]


def test_initial_observation_retries_transient_proc_miss_without_resetting_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    run = fixture["policy"].runs[0]
    for path in (run.report_path, *run.screenshot_paths):
        (fixture["evidence"] / path).unlink()
    (fixture["evidence"] / run.profile_path).mkdir(mode=0o700)
    collect_count = 0
    sleeps: list[float] = []

    class Process:
        pid = 100

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def poll(self) -> int:
            return 0

        def wait(self, timeout: int) -> int:
            assert timeout == 2
            return 0

    def collect(_pid: int, observations: dict[int, ProcessObservation]) -> None:
        nonlocal collect_count
        collect_count += 1
        if collect_count >= 2:
            observations[100] = fixture["observations"][0].processes[0]

    monotonic_values = iter((10.0, 10.0, 10.0, 10.0))
    monkeypatch.setattr(
        launcher_module, "verify_transient_guard", lambda: Path("/guard")
    )
    monkeypatch.setattr(launcher_module.subprocess, "Popen", Process)
    monkeypatch.setattr(launcher_module, "_collect_owned_tree", collect)
    monkeypatch.setattr(
        launcher_module, "_peek_unreaped_returncode", lambda _process: None
    )
    monkeypatch.setattr(
        launcher_module.time, "monotonic", lambda: next(monotonic_values)
    )
    monkeypatch.setattr(
        launcher_module.time, "sleep", lambda seconds: sleeps.append(seconds)
    )
    monkeypatch.setattr(launcher_module, "_wait_owned_group_exit", lambda *_args: True)
    monkeypatch.setattr(launcher_module, "capture_expected_outputs", lambda *_args: ())

    observation = launcher_module._launch_native_run(
        payload_root=fixture["payload"],
        evidence_root=fixture["evidence"],
        install=fixture["install"],
        policy=fixture["policy"],
        run=run,
        initial_payload=fixture["initial"],
        initial_core=fixture["initial_core"],
    )

    assert collect_count == 3
    assert sleeps == [launcher_module.POLL_SECONDS]
    assert observation.exit_code == 0
    assert observation.processes[0].pid == 100


def test_never_observed_browser_uses_fresh_group_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    run = fixture["policy"].runs[0]
    for path in (run.report_path, *run.screenshot_paths):
        (fixture["evidence"] / path).unlink()
    (fixture["evidence"] / run.profile_path).mkdir(mode=0o700)

    events: list[str] = []

    class EarlyProcess:
        pid = 100
        returncode = 7

        def __init__(
            self, *_args: Any, stdout: Any, stderr: Any, **_kwargs: Any
        ) -> None:
            stdout.write(b"known stdout\n")
            stdout.flush()
            stderr.write(
                b"discarded-prefix"
                + b"x" * launcher_module.MAX_FAILURE_DIAGNOSTIC_BYTES
                + b"\x1b[31mknown stderr\n\xff"
            )
            stderr.flush()

        def poll(self) -> int:
            events.append("capture")
            return self.returncode

    called: list[int] = []
    monkeypatch.setattr(
        launcher_module, "verify_transient_guard", lambda: Path("/guard")
    )
    monkeypatch.setattr(launcher_module.subprocess, "Popen", EarlyProcess)
    monkeypatch.setattr(launcher_module, "_collect_owned_tree", lambda *_args: None)
    monkeypatch.setattr(
        launcher_module,
        "_peek_unreaped_returncode",
        lambda _process: events.append("peek") or 7,
    )
    monkeypatch.setattr(
        launcher_module,
        "_stop_unobserved_new_group",
        lambda process: (events.append("cleanup"), called.append(process.pid)),
    )
    with pytest.raises(
        NativeAcceptanceError, match="exited before observation"
    ) as raised:
        launcher_module._launch_native_run(
            payload_root=fixture["payload"],
            evidence_root=fixture["evidence"],
            install=fixture["install"],
            policy=fixture["policy"],
            run=run,
            initial_payload=fixture["initial"],
            initial_core=fixture["initial_core"],
        )
    assert called == [100]
    assert events == ["peek", "cleanup", "capture"]
    message = str(raised.value)
    assert "returncode=7" in message
    assert "known stdout\\n" in message
    assert "truncated=True" in message
    assert "\\x1b[31mknown stderr\\n\\\\xff" in message
    assert "discarded-prefix" not in message
    assert "\x1b" not in message


@pytest.mark.parametrize("failure", ["deadline", "output", "profile"])
def test_initial_observation_enforces_running_bounds_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    failure: str,
) -> None:
    fixture = _fixture(tmp_path)
    run = fixture["policy"].runs[0]
    for path in (run.report_path, *run.screenshot_paths):
        (fixture["evidence"] / path).unlink()
    (fixture["evidence"] / run.profile_path).mkdir(mode=0o700)
    events: list[str] = []

    class Process:
        pid = 100

        def __init__(self, *_args: Any, stdout: Any, **_kwargs: Any) -> None:
            if failure == "output":
                stdout.write(b"x" * (launcher_module.MAX_LOG_BYTES + 1))
                stdout.flush()

        def poll(self) -> None:
            events.append("poll")

    monotonic_values = iter(
        {
            "deadline": (0.0, 0.0, fixture["policy"].deadline_seconds),
            "output": (0.0, 0.0),
            "profile": (0.0, 0.0, 0.0),
        }[failure]
    )
    monkeypatch.setattr(
        launcher_module, "verify_transient_guard", lambda: Path("/guard")
    )
    monkeypatch.setattr(launcher_module.subprocess, "Popen", Process)
    monkeypatch.setattr(launcher_module, "_collect_owned_tree", lambda *_args: None)
    monkeypatch.setattr(
        launcher_module, "_peek_unreaped_returncode", lambda _process: None
    )
    monkeypatch.setattr(
        launcher_module.time, "monotonic", lambda: next(monotonic_values)
    )
    monkeypatch.setattr(launcher_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        launcher_module,
        "_verify_profile_bounds",
        lambda _profile: (
            (_ for _ in ()).throw(NativeAcceptanceError("profile bound sentinel"))
            if failure == "profile"
            else None
        ),
    )
    monkeypatch.setattr(
        launcher_module,
        "_stop_unobserved_new_group",
        lambda *_args: events.append("cleanup"),
    )
    monkeypatch.setattr(
        launcher_module,
        "_process_failure_diagnostic",
        lambda *_args: events.append("capture") or "diagnostic sentinel",
    )

    expected = {
        "deadline": "exceeded its deadline",
        "output": "output exceeded",
        "profile": "profile bound sentinel",
    }[failure]
    with pytest.raises(NativeAcceptanceError, match=expected):
        launcher_module._launch_native_run(
            payload_root=fixture["payload"],
            evidence_root=fixture["evidence"],
            install=fixture["install"],
            policy=fixture["policy"],
            run=run,
            initial_payload=fixture["initial"],
            initial_core=fixture["initial_core"],
        )

    assert events[-2:] == ["cleanup", "capture"]


def test_initial_observation_at_deadline_cleans_observed_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    run = fixture["policy"].runs[0]
    for path in (run.report_path, *run.screenshot_paths):
        (fixture["evidence"] / path).unlink()
    (fixture["evidence"] / run.profile_path).mkdir(mode=0o700)
    events: list[str] = []

    class Process:
        pid = 100

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

    def collect(_pid: int, observations: dict[int, ProcessObservation]) -> None:
        observations[100] = fixture["observations"][0].processes[0]

    monotonic_values = iter((0.0, 0.0, fixture["policy"].deadline_seconds))
    monkeypatch.setattr(
        launcher_module, "verify_transient_guard", lambda: Path("/guard")
    )
    monkeypatch.setattr(launcher_module.subprocess, "Popen", Process)
    monkeypatch.setattr(launcher_module, "_collect_owned_tree", collect)
    monkeypatch.setattr(
        launcher_module.time, "monotonic", lambda: next(monotonic_values)
    )
    monkeypatch.setattr(
        launcher_module,
        "_stop_owned_group",
        lambda *_args: events.append("owned-cleanup"),
    )
    monkeypatch.setattr(
        launcher_module,
        "_process_failure_diagnostic",
        lambda *_args: events.append("capture") or "diagnostic sentinel",
    )

    with pytest.raises(NativeAcceptanceError, match="exceeded its deadline"):
        launcher_module._launch_native_run(
            payload_root=fixture["payload"],
            evidence_root=fixture["evidence"],
            install=fixture["install"],
            policy=fixture["policy"],
            run=run,
            initial_payload=fixture["initial"],
            initial_core=fixture["initial_core"],
        )

    assert events == ["owned-cleanup", "capture"]


def test_unobserved_exit_peek_uses_waitid_without_reaping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        pid = 4321

    class WaitResult:
        si_pid = 4321
        si_code = os.CLD_EXITED
        si_status = 7

    calls: list[tuple[int, int, int]] = []

    def waitid(id_type: int, pid: int, flags: int) -> WaitResult:
        calls.append((id_type, pid, flags))
        return WaitResult()

    monkeypatch.setattr(launcher_module.os, "waitid", waitid)

    assert launcher_module._peek_unreaped_returncode(Process()) == 7
    assert calls == [
        (
            os.P_PID,
            4321,
            os.WEXITED | os.WNOHANG | os.WNOWAIT,
        )
    ]


def test_lost_unobserved_process_authority_never_signals_numeric_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    run = fixture["policy"].runs[0]
    for path in (run.report_path, *run.screenshot_paths):
        (fixture["evidence"] / path).unlink()
    (fixture["evidence"] / run.profile_path).mkdir(mode=0o700)
    events: list[str] = []

    class Process:
        pid = 100

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

    monkeypatch.setattr(
        launcher_module, "verify_transient_guard", lambda: Path("/guard")
    )
    monkeypatch.setattr(launcher_module.subprocess, "Popen", Process)
    monkeypatch.setattr(launcher_module, "_collect_owned_tree", lambda *_args: None)
    monkeypatch.setattr(
        launcher_module.os,
        "waitid",
        lambda *_args: (_ for _ in ()).throw(ChildProcessError()),
    )
    monkeypatch.setattr(
        launcher_module.os,
        "killpg",
        lambda *_args: pytest.fail("lost-authority branch must not signal a PGID"),
    )
    monkeypatch.setattr(
        launcher_module,
        "_stop_unobserved_new_group",
        lambda *_args: pytest.fail("lost-authority branch must not invoke cleanup"),
    )
    monkeypatch.setattr(
        launcher_module,
        "_observe_untrusted_group_state",
        lambda group: events.append(f"observe:{group}") or "unknown-existing",
    )
    monkeypatch.setattr(
        launcher_module,
        "_process_failure_diagnostic",
        lambda *_args: events.append("capture") or "diagnostic sentinel",
    )

    with pytest.raises(NativeAcceptanceError) as raised:
        launcher_module._launch_native_run(
            payload_root=fixture["payload"],
            evidence_root=fixture["evidence"],
            install=fixture["install"],
            policy=fixture["policy"],
            run=run,
            initial_payload=fixture["initial"],
            initial_core=fixture["initial_core"],
        )

    assert events == ["observe:100", "capture"]
    message = str(raised.value)
    assert "identity was lost before cleanup" in message
    assert "group-state=unknown-existing" in message


@pytest.mark.parametrize("root_observed", [False, True])
def test_initial_collection_failure_cleans_partial_state_before_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    root_observed: bool,
) -> None:
    fixture = _fixture(tmp_path)
    run = fixture["policy"].runs[0]
    for path in (run.report_path, *run.screenshot_paths):
        (fixture["evidence"] / path).unlink()
    (fixture["evidence"] / run.profile_path).mkdir(mode=0o700)
    events: list[str] = []

    class Process:
        pid = 100

        def __init__(self, *_args: Any, stderr: Any, **_kwargs: Any) -> None:
            stderr.write(b"initial collection failed")
            stderr.flush()

        def poll(self) -> int:
            events.append("capture")
            return 19

    def fail_collection(_pid: int, observations: dict[int, ProcessObservation]) -> None:
        if root_observed:
            observations[100] = fixture["observations"][0].processes[0]
        raise NativeAcceptanceError("initial collection sentinel")

    monkeypatch.setattr(
        launcher_module, "verify_transient_guard", lambda: Path("/guard")
    )
    monkeypatch.setattr(launcher_module.subprocess, "Popen", Process)
    monkeypatch.setattr(launcher_module, "_collect_owned_tree", fail_collection)
    monkeypatch.setattr(
        launcher_module,
        "_stop_owned_group",
        lambda *_args: events.append("owned-cleanup"),
    )
    monkeypatch.setattr(
        launcher_module,
        "_stop_unobserved_new_group",
        lambda *_args: events.append("unobserved-cleanup"),
    )

    with pytest.raises(NativeAcceptanceError) as raised:
        launcher_module._launch_native_run(
            payload_root=fixture["payload"],
            evidence_root=fixture["evidence"],
            install=fixture["install"],
            policy=fixture["policy"],
            run=run,
            initial_payload=fixture["initial"],
            initial_core=fixture["initial_core"],
        )

    expected_cleanup = "owned-cleanup" if root_observed else "unobserved-cleanup"
    assert events == [expected_cleanup, "capture"]
    message = str(raised.value)
    assert message.startswith("initial collection sentinel;")
    assert "returncode=19" in message
    assert "initial collection failed" in message


def test_initial_collection_base_exception_is_preserved_after_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    run = fixture["policy"].runs[0]
    for path in (run.report_path, *run.screenshot_paths):
        (fixture["evidence"] / path).unlink()
    (fixture["evidence"] / run.profile_path).mkdir(mode=0o700)
    events: list[str] = []

    class Process:
        pid = 100

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

    def interrupt_collection(
        _pid: int, observations: dict[int, ProcessObservation]
    ) -> None:
        observations[100] = fixture["observations"][0].processes[0]
        raise KeyboardInterrupt

    monkeypatch.setattr(
        launcher_module, "verify_transient_guard", lambda: Path("/guard")
    )
    monkeypatch.setattr(launcher_module.subprocess, "Popen", Process)
    monkeypatch.setattr(launcher_module, "_collect_owned_tree", interrupt_collection)
    monkeypatch.setattr(
        launcher_module,
        "_stop_owned_group",
        lambda *_args: events.append("owned-cleanup"),
    )

    with pytest.raises(KeyboardInterrupt):
        launcher_module._launch_native_run(
            payload_root=fixture["payload"],
            evidence_root=fixture["evidence"],
            install=fixture["install"],
            policy=fixture["policy"],
            run=run,
            initial_payload=fixture["initial"],
            initial_core=fixture["initial_core"],
        )

    assert events == ["owned-cleanup"]


def test_failure_diagnostic_read_error_does_not_mask_original_failure() -> None:
    class FailedProcess:
        def poll(self) -> int:
            return 9

    class BrokenStream:
        def flush(self) -> None:
            raise OSError("diagnostic read failed")

    diagnostic = launcher_module._process_failure_diagnostic(
        FailedProcess(),
        BrokenStream(),
        BrokenStream(),  # type: ignore[arg-type]
    )

    assert diagnostic == "diagnostic-unavailable=OSError"
