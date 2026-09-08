"""Per-user archive and physical-GPU acceptance mechanics for the final desktop gate.

This test-only harness validates consumer-bound inputs and observations. A passing
synthetic fixture proves the harness, not a physical GPU or a release candidate.
Issue #55 supplies and retains the final native evidence. Installed RPM paths use
the separate source-bound #52 contract and are deliberately rejected here rather
than being interpreted as the per-user archive layout.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import importlib.util
import io
import json
import os
import re
import stat
import struct
import sys
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

from tongs.desktop.artifact_contract import (
    FIXED_APP_ASAR_PATH,
    FIXED_LAUNCHER_PATH,
    FIXED_LICENSE_INVENTORY_PATH,
    INSTALL_MANIFEST_PATH,
    DesktopInstallManifest,
    DesktopReleaseManifest,
    PackageKind,
    parse_install_manifest,
    parse_release_manifest,
    validate_manifest_pair,
)

MAX_REPORT_BYTES = 2 * 1024 * 1024
MAX_PRIOR_INPUT_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
HASH_CHUNK_BYTES = 1024 * 1024
MAX_PROCESSES = 128
MAX_ARGUMENTS = 128
MAX_ARGUMENT_BYTES = 4096
MAX_JSON_DEPTH = 32
MAX_JSON_VALUES = 50_000
MAX_CORE_MEMBERS = 1024
MAX_CORE_FILE_BYTES = 8 * 1024 * 1024
MAX_PNG_DECODED_BYTES = 128 * 1024 * 1024
COMPACTED_PROCESS_ROLE_TYPES = {
    "zygote": "zygote",
    "gpu": "gpu-process",
    "renderer": "renderer",
    "utility": "utility",
}
# Chromium 152 ``zygote_linux.cc`` forks its children with this literal argv[0]
# while ``SetProcessTitleFromCommandLine`` rewrites the title using the resolved
# ``/proc/self/exe`` target, so a zygote-forked child legitimately reports a
# canonical argv[0] that differs from its resolved executable. See
# ``.worktrees/desktop-125-chromium-process-title-audit.md``.
CHROMIUM_ZYGOTE_ARGV0 = "/proc/self/exe"
SOFTWARE_RENDERERS = (
    "swiftshader",
    "llvmpipe",
    "lavapipe",
    "software rasterizer",
    "mesa offscreen",
)
FORBIDDEN_SWITCHES = frozenset(
    {
        "--app",
        "--disable-gpu",
        "--disable-gpu-sandbox",
        "--disable-seccomp-filter-sandbox",
        "--disable-setuid-sandbox",
        "--no-sandbox",
        "--inspect",
        "--inspect-brk",
        "--js-flags",
        "--remote-debugging-address",
        "--remote-debugging-port",
    }
)
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,199}$")


def _load_receipt_validator() -> Any:
    path = Path(__file__).parents[3] / ".github/scripts/verify_desktop_production.py"
    spec = importlib.util.spec_from_file_location(
        "native_payload_receipt_validator", path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("desktop receipt validator cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_RECEIPTS = _load_receipt_validator()


class NativeAcceptanceError(ValueError):
    """Raised when native evidence cannot satisfy the acceptance contract."""


@dataclass(frozen=True, slots=True)
class PriorInput:
    """Consumer-owned identity captured before native launch."""

    role: str
    path: str
    size: int
    sha256: str
    mode: int


@dataclass(frozen=True, slots=True)
class ReceiptExpectation:
    """Consumer-owned #110 expectations for the artifact receipt."""

    repository: str
    run_id: str
    attempt: int
    environment: str
    provenance: str
    check_id: str
    allowed_report_formats: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CoreBinding:
    """Expected installed-core identity extracted from issue #123 evidence."""

    requested_executable: str
    proc_executable: str
    system_python_target: str
    base_executable: str
    sys_prefix: str
    site_packages: tuple[str, ...]
    package_root: str
    distribution_path: str
    tongs_version: str
    source_commit: str
    source_tree: str
    wheel_sha256: str
    direct_url: Mapping[str, Any] | None
    editable: bool
    package_members: tuple[tuple[str, int, str], ...]
    archive_members: tuple[tuple[str, str], ...]
    generated_exclusion: str
    generated_members: tuple[tuple[str, int, str], ...]
    installed_uid: int


@dataclass(frozen=True, slots=True)
class RunPolicy:
    """Paths and output count fixed by the consumer before a native run."""

    report_path: str
    screenshot_paths: tuple[str, ...]
    screenshot_dimensions: tuple[tuple[int, int], ...]
    profile_path: str
    review_number: int | None = None


@dataclass(frozen=True, slots=True)
class GpuPolicy:
    """Physical device and feature expectations supplied by the final gate."""

    vendor_id: int
    device_id: int
    renderer_tokens: tuple[str, ...]
    feature_status: tuple[tuple[str, str], ...] = (
        ("gpu_compositing", "enabled"),
        ("rasterization", "enabled"),
        ("opengl", "enabled_on"),
        ("webgl", "enabled"),
    )


@dataclass(frozen=True, slots=True)
class NativePayloadPolicy:
    """All independent expectations for one exact installed candidate."""

    source_commit: str
    source_tree: str
    artifact_id: str
    install_manifest_sha256: str
    release_manifest_sha256: str
    prior_inputs: tuple[PriorInput, ...]
    artifact_receipt: ReceiptExpectation
    core: CoreBinding
    runs: tuple[RunPolicy, ...]
    gpu: GpuPolicy
    safe_cwd: str
    provenance: str
    evidence_uid: int
    payload_uid: int
    payload_root_mode: int
    deadline_seconds: int = 90


@dataclass(frozen=True, slots=True)
class FileIdentity:
    """One safely opened file identity."""

    path: str
    size: int
    sha256: str
    mode: int
    device: int
    inode: int
    uid: int


@dataclass(frozen=True, slots=True)
class PayloadSnapshot:
    """Complete installed per-user archive identity at one observation point."""

    root_device: int
    root_inode: int
    root_uid: int
    root_mode: int
    files: tuple[FileIdentity, ...]


@dataclass(frozen=True, slots=True)
class CoreSnapshot:
    """Installed package and distribution identities excluding generated bytecode."""

    roots: tuple[tuple[str, int, int, int, int], ...]
    files: tuple[FileIdentity, ...]


@dataclass(frozen=True, slots=True)
class CapturedOutput:
    """Output identity frozen immediately after trusted creation."""

    path: str
    size: int
    sha256: str
    mode: int
    device: int
    inode: int
    uid: int


@dataclass(frozen=True, slots=True)
class SandboxStatus:
    no_new_privs: int
    seccomp: int
    seccomp_filters: int


@dataclass(frozen=True, slots=True)
class ProcessObservation:
    """Public-safe observation of one process in the launched descendant tree."""

    pid: int
    ppid: int
    process_group: int
    start_time_ticks: int
    role: str
    executable: str
    argv: tuple[str, ...]
    cwd: str | None
    sandbox: SandboxStatus
    raw_argv: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.raw_argv is None:
            object.__setattr__(self, "raw_argv", self.argv)


@dataclass(frozen=True, slots=True)
class NativeRunObservation:
    """One bounded launch result and its independently collected evidence."""

    exit_code: int | None
    signal: int | None
    outputs: tuple[CapturedOutput, ...]
    processes: tuple[ProcessObservation, ...]
    payload_after: PayloadSnapshot
    core_after: CoreSnapshot


@dataclass(frozen=True, slots=True)
class NativeAcceptanceResult:
    """Validated structural result. Only real issue #55 inputs prove hardware."""

    provenance: str
    validation: str
    package_kind: str
    runs: int
    payload_files: int
    gpu_vendor_id: int
    gpu_device_id: int
    renderer: str
    gpu_diagnostics: tuple[str, ...]


def validate_policy(policy: NativePayloadPolicy) -> None:
    """Reject incomplete or ambiguous consumer expectations."""

    _require_sha1(policy.source_commit, "source commit")
    _require_sha1(policy.source_tree, "source tree")
    _require_text(policy.artifact_id, "artifact ID")
    _require_sha256(policy.install_manifest_sha256, "install manifest")
    _require_sha256(policy.release_manifest_sha256, "release manifest")
    if len(policy.prior_inputs) < 4 or len(policy.prior_inputs) > 16:
        raise NativeAcceptanceError("artifact and core prior inputs are required")
    roles: set[str] = set()
    paths: set[str] = set()
    for value in policy.prior_inputs:
        _require_text(value.role, "prior input role")
        _require_relative_path(value.path, "prior input path")
        _require_positive_size(value.size, "prior input size")
        _require_sha256(value.sha256, "prior input")
        if value.mode not in {0o600, 0o644}:
            raise NativeAcceptanceError("prior input mode must be 0600 or 0644")
        if value.role in roles or value.path.casefold() in paths:
            raise NativeAcceptanceError("prior input roles and paths must be unique")
        roles.add(value.role)
        paths.add(value.path.casefold())
    if (
        not {
            "desktop-artifact-receipt",
            "desktop-install-manifest",
            "desktop-release-manifest",
            "installed-core-report",
        }
        <= roles
    ):
        raise NativeAcceptanceError("required artifact and core bindings are missing")
    by_role = {item.role: item for item in policy.prior_inputs}
    if (
        by_role["desktop-install-manifest"].sha256 != policy.install_manifest_sha256
        or by_role["desktop-release-manifest"].sha256 != policy.release_manifest_sha256
    ):
        raise NativeAcceptanceError(
            "prior manifest identities differ from the candidate policy"
        )
    try:
        _RECEIPTS.ReceiptPolicy(
            expected_commit=policy.source_commit,
            expected_tree=policy.source_tree,
            expected_repository=policy.artifact_receipt.repository,
            expected_run_id=policy.artifact_receipt.run_id,
            expected_attempt=policy.artifact_receipt.attempt,
            expected_environment=policy.artifact_receipt.environment,
            expected_provenance=policy.artifact_receipt.provenance,
            expected_check_id=policy.artifact_receipt.check_id,
            allowed_report_formats=policy.artifact_receipt.allowed_report_formats,
        )
    except _RECEIPTS.ReceiptValidationError as error:
        raise NativeAcceptanceError("artifact receipt policy is invalid") from error
    _validate_core_binding(policy.core, policy)
    if len(policy.runs) < 2 or len(policy.runs) > 8:
        raise NativeAcceptanceError(
            "native startup repeat count must be between 2 and 8"
        )
    outputs = {value.path.casefold() for value in policy.prior_inputs}
    for run in policy.runs:
        _require_relative_path(run.report_path, "smoke report path")
        if not run.screenshot_paths:
            raise NativeAcceptanceError("every native run requires a screenshot")
        if len(run.screenshot_dimensions) != len(run.screenshot_paths):
            raise NativeAcceptanceError("screenshot dimensions are incomplete")
        for dimensions in run.screenshot_dimensions:
            if (
                not isinstance(dimensions, tuple)
                or len(dimensions) != 2
                or any(type(value) is not int for value in dimensions)
                or dimensions[0] < 640
                or dimensions[1] < 480
                or dimensions[0] > 16_384
                or dimensions[1] > 16_384
            ):
                raise NativeAcceptanceError("screenshot dimensions are invalid")
        if run.review_number is not None and (
            type(run.review_number) is not int or run.review_number <= 0
        ):
            raise NativeAcceptanceError("smoke review number must be positive")
        expected_screenshots = _derived_screenshot_paths(run)
        if run.screenshot_paths != expected_screenshots:
            raise NativeAcceptanceError(
                "screenshot paths do not match production smoke outputs"
            )
        names = (run.report_path, *run.screenshot_paths)
        if len(names) != len({name.casefold() for name in names}):
            raise NativeAcceptanceError("native run output paths must be unique")
        for name in names:
            _require_relative_path(name, "native output path")
            if len(PurePosixPath(name).parts) != 1:
                raise NativeAcceptanceError(
                    "native output paths must be direct evidence children"
                )
            folded = name.casefold()
            if folded in outputs:
                raise NativeAcceptanceError("native output paths repeat across runs")
            outputs.add(folded)
        _require_relative_path(run.profile_path, "native profile path")
        if len(PurePosixPath(run.profile_path).parts) != 1:
            raise NativeAcceptanceError(
                "native profile path must be a direct evidence child"
            )
        folded_profile = run.profile_path.casefold()
        if folded_profile in outputs:
            raise NativeAcceptanceError("native profile path collides with an output")
        outputs.add(folded_profile)
    if policy.gpu.vendor_id <= 0 or policy.gpu.device_id <= 0:
        raise NativeAcceptanceError("physical GPU vendor and device IDs are required")
    if not policy.gpu.renderer_tokens or any(
        not token or len(token.encode("utf-8")) > 256
        for token in policy.gpu.renderer_tokens
    ):
        raise NativeAcceptanceError("physical GPU renderer tokens are invalid")
    features = dict(policy.gpu.feature_status)
    if len(features) != len(policy.gpu.feature_status):
        raise NativeAcceptanceError("GPU feature expectations contain duplicates")
    required_features = {
        "gpu_compositing": "enabled",
        "rasterization": "enabled",
        "opengl": "enabled_on",
        "webgl": "enabled",
    }
    if features != required_features:
        raise NativeAcceptanceError("GPU feature expectations are not hardware enabled")
    _require_absolute_directory(policy.safe_cwd, "safe working directory")
    if policy.provenance not in {"controlled-fixture", "native-installed"}:
        raise NativeAcceptanceError("native evidence provenance is invalid")
    for value in (policy.evidence_uid, policy.payload_uid):
        if type(value) is not int or value < 0:
            raise NativeAcceptanceError("native ownership policy is invalid")
    if policy.evidence_uid != os.geteuid():
        raise NativeAcceptanceError("evidence must be owned by the current consumer")
    if policy.payload_root_mode not in {0o700, 0o755}:
        raise NativeAcceptanceError("installed payload root mode policy is invalid")
    if (
        type(policy.deadline_seconds) is not int
        or policy.deadline_seconds < 10
        or policy.deadline_seconds > 120
    ):
        raise NativeAcceptanceError(
            "native deadline must be between 10 and 120 seconds"
        )


def parse_bound_manifests(
    install_bytes: bytes,
    release_bytes: bytes,
    policy: NativePayloadPolicy,
) -> tuple[DesktopInstallManifest, DesktopReleaseManifest]:
    """Parse and bind manifests for an installed per-user archive candidate."""

    validate_policy(policy)
    if _digest(install_bytes) != policy.install_manifest_sha256:
        raise NativeAcceptanceError("install manifest digest does not match policy")
    if _digest(release_bytes) != policy.release_manifest_sha256:
        raise NativeAcceptanceError("release manifest digest does not match policy")
    try:
        install = parse_install_manifest(install_bytes)
        release = parse_release_manifest(release_bytes)
    except ValueError as error:
        raise NativeAcceptanceError("desktop artifact manifest is invalid") from error
    artifacts = tuple(
        item for item in release.artifacts if item.artifact_id == policy.artifact_id
    )
    if len(artifacts) != 1:
        raise NativeAcceptanceError("expected desktop artifact is missing or ambiguous")
    try:
        validate_manifest_pair(release, artifacts[0], install)
    except ValueError as error:
        raise NativeAcceptanceError("desktop artifact manifests disagree") from error
    if release.source_commit != policy.source_commit:
        raise NativeAcceptanceError("release manifest source commit is stale")
    if install.package_kind is not PackageKind.USER_ARCHIVE:
        raise NativeAcceptanceError(
            "native payload verifier supports only per-user archive payloads; "
            "RPM installed paths require the source-bound RPM contract"
        )
    _validate_payload_ownership(install, policy)
    return install, release


def _validate_payload_ownership(
    install: DesktopInstallManifest, policy: NativePayloadPolicy
) -> None:
    if install.package_kind is not PackageKind.USER_ARCHIVE:
        raise NativeAcceptanceError(
            "native payload verifier supports only per-user archive payloads"
        )
    if (
        install.ownership.value != "per-user"
        or policy.payload_uid != os.geteuid()
        or policy.payload_root_mode != 0o700
    ):
        raise NativeAcceptanceError(
            "per-user payload ownership does not match the manifest"
        )


def core_binding_from_report(
    document: bytes,
    *,
    expected_report_sha256: str,
    expected_source_commit: str,
    expected_source_tree: str,
    expected_wheel_sha256: str,
    expected_installed_uid: int,
) -> CoreBinding:
    """Extract the narrow #123 identity contract from a consumer-bound report."""

    _require_sha256(expected_report_sha256, "installed core report")
    _require_sha1(expected_source_commit, "expected core source commit")
    _require_sha1(expected_source_tree, "expected core source tree")
    _require_sha256(expected_wheel_sha256, "expected core wheel")
    if type(expected_installed_uid) is not int or expected_installed_uid < 0:
        raise NativeAcceptanceError("expected installed core owner is invalid")
    if _digest(document) != expected_report_sha256:
        raise NativeAcceptanceError(
            "installed core report digest does not match policy"
        )
    report = _decode_json(document, "installed core report")
    source = _mapping(report.get("source"), "installed core source")
    wheel = _mapping(report.get("wheel"), "installed core wheel")
    identity = _mapping(report.get("installed_identity"), "installed core identity")
    if (
        source.get("commit") != expected_source_commit
        or source.get("tree") != expected_source_tree
    ):
        raise NativeAcceptanceError("installed core report source identity is stale")
    if source.get("tracked_status") != "clean":
        raise NativeAcceptanceError("installed core report source was not clean")
    if wheel.get("sha256") != expected_wheel_sha256:
        raise NativeAcceptanceError("installed core report wheel identity disagrees")
    package_hashes = _sha256_map(
        wheel.get("package_member_sha256"), "wheel package members"
    )
    if package_hashes != _sha256_map(
        source.get("package_member_sha256"), "source package members"
    ):
        raise NativeAcceptanceError("source and wheel package members disagree")
    archive_hashes = _sha256_map(
        wheel.get("archive_member_sha256"), "wheel archive members"
    )
    installed_members = _mapping(
        identity.get("wheel_members"), "installed wheel members"
    )
    if set(installed_members) != set(package_hashes):
        raise NativeAcceptanceError("installed package member set is incomplete")
    package_members = []
    for path, digest in sorted(package_hashes.items()):
        _require_relative_path(path, "installed package member")
        if not path.startswith("tongs/"):
            raise NativeAcceptanceError("installed package member is outside Tongs")
        member = _mapping(installed_members[path], "installed wheel member")
        if set(member) != {"path", "sha256", "size"} or member["sha256"] != digest:
            raise NativeAcceptanceError("installed package member identity disagrees")
        size = member["size"]
        if type(size) is not int or size < 0 or size > MAX_CORE_FILE_BYTES:
            raise NativeAcceptanceError("installed package member size exceeds limits")
        _require_absolute_path(member["path"], "installed package member path")
        package_members.append((path, size, digest))
    exclusions = _mapping(identity.get("generated_exclusions"), "generated exclusions")
    if exclusions.get("policy") != "only __pycache__/*.pyc" or set(exclusions) != {
        "files",
        "policy",
    }:
        raise NativeAcceptanceError("installed generated exclusion policy is invalid")
    if identity.get("invalid_generated") != []:
        raise NativeAcceptanceError(
            "installed core report contains invalid generated files"
        )
    generated_files = _mapping(exclusions["files"], "generated exclusion files")
    if len(generated_files) > MAX_CORE_MEMBERS:
        raise NativeAcceptanceError("generated exclusion file count exceeds limits")
    generated_members = []
    for path, raw_member in sorted(generated_files.items()):
        _require_relative_path(path, "generated exclusion path")
        if not _is_generated_bytecode(path):
            raise NativeAcceptanceError("generated exclusion path is not bytecode")
        member = _mapping(raw_member, "generated exclusion member")
        if set(member) != {"path", "sha256", "size"}:
            raise NativeAcceptanceError("generated exclusion member fields are invalid")
        size = member["size"]
        if type(size) is not int or size < 1 or size > MAX_CORE_FILE_BYTES:
            raise NativeAcceptanceError(
                "generated exclusion member size exceeds limits"
            )
        _require_sha256(member["sha256"], "generated exclusion member")
        _require_absolute_path(member["path"], "generated exclusion member path")
        generated_members.append((path, size, member["sha256"]))
    site_values = identity.get("site_packages")
    if not isinstance(site_values, list) or not site_values:
        raise NativeAcceptanceError("installed core report lacks site-packages")
    sites: list[str] = []
    for value in site_values:
        _require_absolute_path(value, "reported site-packages")
        if value not in sites:
            sites.append(value)
    direct_url = identity.get("direct_url")
    if direct_url is not None and not isinstance(direct_url, dict):
        raise NativeAcceptanceError("installed core direct_url must be an object")
    binding = CoreBinding(
        requested_executable=_require_text(
            identity.get("executable"), "reported requested interpreter"
        ),
        proc_executable=_require_text(
            identity.get("proc_self_exe"), "reported process interpreter"
        ),
        system_python_target=_require_text(
            identity.get("system_python_target"), "reported system Python target"
        ),
        base_executable=_require_text(
            identity.get("base_executable"), "reported base Python executable"
        ),
        sys_prefix=_require_text(identity.get("sys_prefix"), "reported sys.prefix"),
        site_packages=tuple(sites),
        package_root=_require_text(
            identity.get("package_root"), "reported Tongs package root"
        ),
        distribution_path=_require_text(
            identity.get("distribution_path"), "reported Tongs distribution path"
        ),
        tongs_version=_require_text(
            identity.get("tongs_version"), "reported Tongs version"
        ),
        source_commit=expected_source_commit,
        source_tree=expected_source_tree,
        wheel_sha256=expected_wheel_sha256,
        direct_url=direct_url,
        editable=identity.get("editable"),
        package_members=tuple(package_members),
        archive_members=tuple(sorted(archive_hashes.items())),
        generated_exclusion="only __pycache__/*.pyc",
        generated_members=tuple(generated_members),
        installed_uid=expected_installed_uid,
    )
    if type(binding.editable) is not bool:
        raise NativeAcceptanceError("installed core editable status is not Boolean")
    return binding


def _sha256_map(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or not value or len(value) > MAX_CORE_MEMBERS:
        raise NativeAcceptanceError(f"{label} is invalid")
    result = {}
    for path, digest in value.items():
        _require_relative_path(path, label)
        _require_sha256(digest, label)
        result[path] = digest
    return result


def verify_prior_inputs(evidence_root: Path, policy: NativePayloadPolicy) -> None:
    """Recompute artifact/core input identities fixed before launch."""

    validate_policy(policy)
    root = _private_evidence_root(evidence_root, policy.evidence_uid)
    receipt_path: Path | None = None
    for expected in policy.prior_inputs:
        actual = _read_identity(
            root,
            expected.path,
            maximum_size=MAX_PRIOR_INPUT_BYTES,
            expected_mode=expected.mode,
            expected_uid=policy.evidence_uid,
        )
        if actual.size != expected.size or actual.sha256 != expected.sha256:
            raise NativeAcceptanceError(
                f"prior input {expected.path!r} differs from consumer identity"
            )
        if expected.role == "desktop-artifact-receipt":
            receipt_path = root / expected.path
    assert receipt_path is not None
    try:
        receipt_policy = _RECEIPTS.ReceiptPolicy(
            expected_commit=policy.source_commit,
            expected_tree=policy.source_tree,
            expected_repository=policy.artifact_receipt.repository,
            expected_run_id=policy.artifact_receipt.run_id,
            expected_attempt=policy.artifact_receipt.attempt,
            expected_environment=policy.artifact_receipt.environment,
            expected_provenance=policy.artifact_receipt.provenance,
            expected_check_id=policy.artifact_receipt.check_id,
            allowed_report_formats=policy.artifact_receipt.allowed_report_formats,
        )
        validation = _RECEIPTS.validate_receipt_file(
            receipt_path, evidence_root=root, policy=receipt_policy
        )
        if validation.receipt.get("result") != "success":
            raise NativeAcceptanceError("artifact receipt does not report success")
        required_paths = {
            item.path
            for item in policy.prior_inputs
            if item.role != "desktop-artifact-receipt"
        }
        bound_paths = {item.path for item in validation.bound_files}
        if not required_paths <= bound_paths:
            raise NativeAcceptanceError(
                "artifact receipt does not bind all required candidate inputs"
            )
    except _RECEIPTS.ReceiptValidationError as error:
        raise NativeAcceptanceError(
            "artifact receipt or its staged files are invalid"
        ) from error


def capture_payload_snapshot(
    payload_root: Path,
    install: DesktopInstallManifest,
    *,
    expected_uid: int,
    root_mode: int,
    install_manifest_sha256: str,
) -> PayloadSnapshot:
    """Hash the exact declared runtime and reject substitution or extra files."""

    root = _absolute_directory(payload_root, "installed payload root")
    if "node_modules" in root.parts:
        raise NativeAcceptanceError("installed payload must not use node_modules")
    root_status = root.stat(follow_symlinks=False)
    if (
        root_status.st_uid != expected_uid
        or stat.S_IMODE(root_status.st_mode) != root_mode
    ):
        raise NativeAcceptanceError(
            "installed payload root ownership or mode is invalid"
        )
    try:
        with os.scandir(root) as iterator:
            top_level = tuple(iterator)
    except OSError as error:
        raise NativeAcceptanceError(
            "installed payload root cannot be enumerated"
        ) from error
    by_name = {entry.name: entry for entry in top_level}
    if set(by_name) != {INSTALL_MANIFEST_PATH, "runtime"} or not by_name[
        "runtime"
    ].is_dir(follow_symlinks=False):
        raise NativeAcceptanceError(
            "installed payload root contains undeclared top-level entries"
        )
    actual_paths, actual_directories = _runtime_paths(
        root,
        install.extraction_limits.max_entries,
        expected_uid=expected_uid,
    )
    declared_paths = {item.path for item in install.files}
    declared_directories = {"runtime"}
    for path in declared_paths:
        parent = PurePosixPath(path).parent
        while parent.as_posix() != ".":
            declared_directories.add(parent.as_posix())
            parent = parent.parent
    if actual_directories != declared_directories:
        raise NativeAcceptanceError(
            "installed runtime contains undeclared or missing directories"
        )
    if actual_paths != declared_paths:
        missing = sorted(declared_paths - actual_paths)
        extra = sorted(actual_paths - declared_paths)
        raise NativeAcceptanceError(
            f"installed runtime does not match manifest paths: missing={missing}, extra={extra}"
        )
    installed_manifest = _read_identity(
        root,
        INSTALL_MANIFEST_PATH,
        maximum_size=MAX_PRIOR_INPUT_BYTES,
        expected_mode=0o644,
        expected_uid=expected_uid,
    )
    if installed_manifest.sha256 != install_manifest_sha256:
        raise NativeAcceptanceError(
            "installed root manifest differs from the verified manifest"
        )
    files = [installed_manifest]
    for expected in sorted(install.files, key=lambda item: item.path):
        identity = _read_identity(
            root,
            expected.path,
            maximum_size=install.extraction_limits.max_file_bytes,
            expected_mode=0o755 if expected.executable else 0o644,
            expected_uid=expected_uid,
        )
        if identity.size != expected.byte_count or identity.sha256 != expected.sha256:
            raise NativeAcceptanceError(
                f"installed payload file {expected.path!r} differs from manifest"
            )
        files.append(identity)
    required = {
        FIXED_LAUNCHER_PATH,
        FIXED_APP_ASAR_PATH,
        FIXED_LICENSE_INVENTORY_PATH,
    }
    if not required <= {item.path for item in files}:
        raise NativeAcceptanceError("installed payload lacks mandatory runtime files")
    after = root.stat(follow_symlinks=False)
    if (root_status.st_dev, root_status.st_ino) != (after.st_dev, after.st_ino):
        raise NativeAcceptanceError(
            "installed payload root changed during verification"
        )
    return PayloadSnapshot(
        root_status.st_dev,
        root_status.st_ino,
        root_status.st_uid,
        stat.S_IMODE(root_status.st_mode),
        tuple(files),
    )


def verify_payload_unchanged(
    expected: PayloadSnapshot,
    payload_root: Path,
    install: DesktopInstallManifest,
    policy: NativePayloadPolicy,
) -> PayloadSnapshot:
    """Rehash the runtime after launch and require the original file identities."""

    actual = capture_payload_snapshot(
        payload_root,
        install,
        expected_uid=policy.payload_uid,
        root_mode=policy.payload_root_mode,
        install_manifest_sha256=policy.install_manifest_sha256,
    )
    if actual != expected:
        raise NativeAcceptanceError("installed payload changed after native launch")
    return actual


def capture_core_snapshot(binding: CoreBinding) -> CoreSnapshot:
    """Rehash installed wheel files and reject unrecorded source substitution."""

    package_root = _absolute_directory(Path(binding.package_root), "Tongs package root")
    distribution_root = _absolute_directory(
        Path(binding.distribution_path), "Tongs distribution root"
    )
    prefix = _absolute_directory(Path(binding.sys_prefix), "installed Python prefix")
    roots = []
    for path in (prefix, package_root, distribution_root):
        status = path.stat(follow_symlinks=False)
        if status.st_uid != binding.installed_uid or stat.S_IMODE(
            status.st_mode
        ) not in {
            0o700,
            0o755,
        }:
            raise NativeAcceptanceError("installed core directory ownership is invalid")
        roots.append(
            (
                str(path),
                status.st_dev,
                status.st_ino,
                status.st_uid,
                stat.S_IMODE(status.st_mode),
            )
        )
    expected_package = {
        path: (size, digest) for path, size, digest in binding.package_members
    }
    package_files = _enumerate_tree(
        package_root, MAX_CORE_MEMBERS * 2, expected_uid=binding.installed_uid
    )
    expected_relative = {path.removeprefix("tongs/") for path in expected_package}
    expected_generated = {
        path: (size, digest) for path, size, digest in binding.generated_members
    }
    if package_files != expected_relative | set(expected_generated):
        raise NativeAcceptanceError("installed Tongs package member set changed")
    identities = []
    site_root = next(
        (
            Path(site)
            for site in binding.site_packages
            if _is_strictly_within(Path(site), package_root)
        ),
        None,
    )
    if site_root is None:
        raise NativeAcceptanceError("Tongs package has no bound site-packages root")
    for path, (size, digest) in sorted(expected_package.items()):
        identity = _read_identity(
            site_root,
            path,
            maximum_size=MAX_CORE_FILE_BYTES,
            expected_mode=0o644,
            allow_empty=True,
            expected_uid=binding.installed_uid,
        )
        if identity.size != size or identity.sha256 != digest:
            raise NativeAcceptanceError(f"installed core member {path!r} changed")
        identities.append(identity)
    for path, (size, digest) in sorted(expected_generated.items()):
        identity = _read_identity(
            package_root,
            path,
            maximum_size=MAX_CORE_FILE_BYTES,
            expected_mode=0o644,
            expected_uid=binding.installed_uid,
        )
        if identity.size != size or identity.sha256 != digest:
            raise NativeAcceptanceError(f"generated core member {path!r} changed")
        identities.append(identity)
    distribution_name = distribution_root.name
    archive_members = dict(binding.archive_members)
    immutable_distribution = {
        path.removeprefix(f"{distribution_name}/"): digest
        for path, digest in archive_members.items()
        if path.startswith(f"{distribution_name}/")
        and path != f"{distribution_name}/RECORD"
    }
    actual_distribution = set(
        _enumerate_tree(
            distribution_root, MAX_CORE_MEMBERS, expected_uid=binding.installed_uid
        )
    )
    generated_distribution = {"INSTALLER", "RECORD", "REQUESTED", "direct_url.json"}
    if actual_distribution != set(immutable_distribution) | generated_distribution:
        raise NativeAcceptanceError("installed distribution member set changed")
    distribution_records = {}
    for path, digest in sorted(immutable_distribution.items()):
        identity = _read_identity(
            distribution_root,
            path,
            maximum_size=MAX_CORE_FILE_BYTES,
            expected_mode=0o644,
            allow_empty=True,
            expected_uid=binding.installed_uid,
        )
        if identity.sha256 != digest:
            raise NativeAcceptanceError(
                f"installed distribution member {path!r} changed"
            )
        identities.append(identity)
        distribution_records[path] = (identity.size, identity.sha256)
    generated_documents = {}
    for path in sorted(generated_distribution):
        result = _read_identity(
            distribution_root,
            path,
            maximum_size=MAX_CORE_FILE_BYTES,
            expected_mode=0o644,
            return_bytes=True,
            allow_empty=True,
            expected_uid=binding.installed_uid,
        )
        assert isinstance(result, tuple)
        identity, document = result
        identities.append(identity)
        generated_documents[path] = document
    if (
        generated_documents["INSTALLER"] != b"pip\n"
        or generated_documents["REQUESTED"] != b""
    ):
        raise NativeAcceptanceError(
            "installed distribution provenance files are invalid"
        )
    if (
        _decode_json(generated_documents["direct_url.json"], "installed direct_url")
        != binding.direct_url
    ):
        raise NativeAcceptanceError("installed direct_url differs from core binding")
    script_identities = _verify_installed_record(
        generated_documents["RECORD"],
        prefix,
        binding.installed_uid,
        distribution_name,
        expected_package,
        set(expected_generated),
        distribution_records,
        {
            name: value
            for name, value in generated_documents.items()
            if name != "RECORD"
        },
    )
    identities.extend(script_identities)
    for path, device, inode, uid, mode in roots:
        status = Path(path).stat(follow_symlinks=False)
        if (
            status.st_dev,
            status.st_ino,
            status.st_uid,
            stat.S_IMODE(status.st_mode),
        ) != (device, inode, uid, mode):
            raise NativeAcceptanceError("installed core directory changed during read")
    return CoreSnapshot(
        tuple(roots), tuple(sorted(identities, key=lambda item: item.path))
    )


def _verify_installed_record(
    document: bytes,
    prefix: Path,
    installed_uid: int,
    distribution_name: str,
    package_members: Mapping[str, tuple[int, str]],
    generated_members: set[str],
    distribution_members: Mapping[str, tuple[int, str]],
    generated_documents: Mapping[str, bytes],
) -> tuple[FileIdentity, ...]:
    try:
        rows = list(csv.reader(io.StringIO(document.decode("utf-8", errors="strict"))))
    except (UnicodeDecodeError, csv.Error) as error:
        raise NativeAcceptanceError("installed RECORD is invalid") from error
    if (
        not rows
        or len(rows) > MAX_CORE_MEMBERS * 3
        or any(len(row) != 3 for row in rows)
    ):
        raise NativeAcceptanceError("installed RECORD shape is invalid")
    by_path = {row[0]: row[1:] for row in rows}
    if len(by_path) != len(rows):
        raise NativeAcceptanceError("installed RECORD contains duplicate paths")
    required: dict[str, tuple[int, str]] = dict(package_members)
    for path, identity in distribution_members.items():
        member = f"{distribution_name}/{path}"
        required[member] = identity
    for path, value in generated_documents.items():
        required[f"{distribution_name}/{path}"] = (len(value), _digest_unbounded(value))
    record_path = f"{distribution_name}/RECORD"
    if by_path.get(record_path) != ["", ""]:
        raise NativeAcceptanceError("installed RECORD does not identify itself safely")
    for path, (size, digest) in required.items():
        fields = by_path.get(path)
        if fields != [_record_hash(digest), str(size)]:
            raise NativeAcceptanceError(
                f"installed RECORD identity for {path!r} disagrees"
            )
    allowed = set(required) | {record_path}
    scripts = {
        "../../../bin/tongs": "bin/tongs",
        "../../../bin/tongs-mcp": "bin/tongs-mcp",
    }
    identities = []
    for record_path_value, prefix_path in scripts.items():
        fields = by_path.get(record_path_value)
        if (
            fields is None
            or not fields[0].startswith("sha256=")
            or not fields[1].isdecimal()
        ):
            raise NativeAcceptanceError(
                "installed console script RECORD entry is invalid"
            )
        identity = _read_identity(
            prefix,
            prefix_path,
            maximum_size=MAX_CORE_FILE_BYTES,
            expected_mode=0o755,
            expected_uid=installed_uid,
        )
        if fields != [_record_hash(identity.sha256), str(identity.size)]:
            raise NativeAcceptanceError("installed console script differs from RECORD")
        identities.append(identity)
        allowed.add(record_path_value)
    extras = set(by_path) - allowed
    expected_extras = {f"tongs/{path}" for path in generated_members}
    if extras != expected_extras or any(by_path[path] != ["", ""] for path in extras):
        raise NativeAcceptanceError("installed RECORD contains an unapproved member")
    return tuple(identities)


def _record_hash(digest: str) -> str:
    return "sha256=" + base64.urlsafe_b64encode(bytes.fromhex(digest)).rstrip(
        b"="
    ).decode("ascii")


def _digest_unbounded(document: bytes) -> str:
    return hashlib.sha256(document).hexdigest()


def _is_generated_bytecode(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return len(parts) >= 2 and "__pycache__" in parts and path.endswith(".pyc")


def verify_core_observation(
    binding: CoreBinding,
    observation: Mapping[str, Any],
    policy: NativePayloadPolicy,
) -> None:
    """Bind the exact requested interpreter to installed, non-editable Tongs."""

    _validate_core_binding(binding, policy)
    _require_exact_keys(
        observation,
        {
            "requested_executable",
            "proc_executable",
            "system_python_target",
            "base_executable",
            "sys_prefix",
            "site_packages",
            "package_root",
            "distribution_path",
            "tongs_version",
            "direct_url",
            "editable",
        },
        "installed Python observation",
    )
    expected = {
        "requested_executable": binding.requested_executable,
        "proc_executable": binding.proc_executable,
        "system_python_target": binding.system_python_target,
        "base_executable": binding.base_executable,
        "sys_prefix": binding.sys_prefix,
        "site_packages": list(binding.site_packages),
        "package_root": binding.package_root,
        "distribution_path": binding.distribution_path,
        "tongs_version": binding.tongs_version,
        "direct_url": binding.direct_url,
        "editable": binding.editable,
    }
    if dict(observation) != expected:
        raise NativeAcceptanceError(
            "installed Python observation differs from core binding"
        )
    prefix = Path(binding.sys_prefix)
    requested = Path(binding.requested_executable)
    _require_within(prefix, requested, "requested interpreter")
    sites = tuple(Path(value) for value in binding.site_packages)
    for site_packages in sites:
        _require_within(prefix, site_packages, "site-packages")
    if not any(_is_strictly_within(site, Path(binding.package_root)) for site in sites):
        raise NativeAcceptanceError("Tongs package is outside installed site-packages")
    if not any(
        _is_strictly_within(site, Path(binding.distribution_path)) for site in sites
    ):
        raise NativeAcceptanceError(
            "Tongs distribution is outside installed site-packages"
        )
    if binding.editable:
        raise NativeAcceptanceError(
            "installed core must not be an editable source import"
        )
    _verify_direct_url(binding.direct_url, binding.wheel_sha256)
    try:
        requested_target = requested.resolve(strict=True)
    except OSError as error:
        raise NativeAcceptanceError(
            "requested installed interpreter does not exist"
        ) from error
    if str(requested_target) != binding.proc_executable:
        raise NativeAcceptanceError(
            "Python /proc executable differs from requested installed interpreter"
        )
    if binding.base_executable != binding.system_python_target:
        raise NativeAcceptanceError("Python base executable differs from system target")
    for value, kind in (
        (binding.proc_executable, "file"),
        (binding.system_python_target, "file"),
        (binding.sys_prefix, "directory"),
        (binding.package_root, "directory"),
        (binding.distribution_path, "directory"),
        *((site, "directory") for site in binding.site_packages),
    ):
        path = Path(value)
        try:
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise NativeAcceptanceError("installed core path does not exist") from error
        if (
            str(resolved) != value
            or (kind == "file" and not path.is_file())
            or (kind == "directory" and not path.is_dir())
        ):
            raise NativeAcceptanceError("installed core path identity is invalid")
    if not os.access(requested, os.X_OK):
        raise NativeAcceptanceError("requested installed interpreter is not executable")


def capture_expected_outputs(
    evidence_root: Path,
    run: RunPolicy,
    expected_uid: int,
) -> tuple[CapturedOutput, ...]:
    """Freeze fresh policy-owned output identities after trusted creation."""

    root = _private_evidence_root(evidence_root, expected_uid)
    values = []
    for relative in (run.report_path, *run.screenshot_paths):
        maximum = MAX_REPORT_BYTES if relative == run.report_path else MAX_OUTPUT_BYTES
        item = _read_identity(
            root,
            relative,
            maximum_size=maximum,
            expected_mode=0o600,
            expected_uid=expected_uid,
        )
        values.append(_captured_output(item))
    return tuple(values)


def verify_native_acceptance(
    *,
    payload_root: Path,
    evidence_root: Path,
    install: DesktopInstallManifest,
    policy: NativePayloadPolicy,
    initial_payload: PayloadSnapshot,
    initial_core: CoreSnapshot,
    core_observation: Mapping[str, Any],
    observations: Sequence[NativeRunObservation],
) -> NativeAcceptanceResult:
    """Validate repeated installed production smoke and physical GPU evidence."""

    validate_policy(policy)
    verify_core_observation(policy.core, core_observation, policy)
    if len(observations) != len(policy.runs):
        raise NativeAcceptanceError("native run observation count is incomplete")
    expected_files = {item.path: item for item in initial_payload.files}
    allowed_executables = {
        str((payload_root / path).resolve(strict=True))
        for path, item in expected_files.items()
        if item.mode == 0o755
    }
    renderer = ""
    gpu_diagnostics: set[str] = set()
    for index, (run_policy, observed) in enumerate(
        zip(policy.runs, observations, strict=True), start=1
    ):
        if observed.exit_code != 0 or observed.signal is not None:
            raise NativeAcceptanceError(f"native run {index} did not exit successfully")
        if len(observed.outputs) != 1 + len(run_policy.screenshot_paths):
            raise NativeAcceptanceError(
                f"native run {index} output count is incomplete"
            )
        expected_output_paths = (run_policy.report_path, *run_policy.screenshot_paths)
        if tuple(item.path for item in observed.outputs) != expected_output_paths:
            raise NativeAcceptanceError(f"native run {index} output paths disagree")
        _verify_captured_outputs(
            evidence_root, run_policy, observed.outputs, policy.evidence_uid
        )
        report_bytes = _read_identity(
            _private_evidence_root(evidence_root, policy.evidence_uid),
            run_policy.report_path,
            maximum_size=MAX_REPORT_BYTES,
            expected_mode=0o600,
            return_bytes=True,
            expected_uid=policy.evidence_uid,
        )
        assert isinstance(report_bytes, tuple)
        report_identity, document = report_bytes
        if report_identity.sha256 != observed.outputs[0].sha256:
            raise NativeAcceptanceError("smoke report differs from frozen capture")
        report = _decode_json(document, "production smoke report")
        renderer, diagnostics = _verify_smoke_report(
            report, install, policy, run_policy, evidence_root, observed.processes
        )
        gpu_diagnostics.update(diagnostics)
        _verify_process_observations(
            observed.processes,
            report,
            payload_root,
            evidence_root,
            allowed_executables,
            policy,
            run_policy,
        )
        if observed.payload_after != initial_payload:
            raise NativeAcceptanceError(
                f"installed payload changed during native run {index}"
            )
        if observed.core_after != initial_core:
            raise NativeAcceptanceError(
                f"installed core changed during native run {index}"
            )
        verify_payload_unchanged(initial_payload, payload_root, install, policy)
        if capture_core_snapshot(policy.core) != initial_core:
            raise NativeAcceptanceError("installed core changed after native launch")
    return NativeAcceptanceResult(
        provenance=policy.provenance,
        validation=(
            "controlled-fixture-structural-only"
            if policy.provenance == "controlled-fixture"
            else "native-installed-observation-validated"
        ),
        package_kind=install.package_kind.value,
        runs=len(observations),
        payload_files=len(initial_payload.files),
        gpu_vendor_id=policy.gpu.vendor_id,
        gpu_device_id=policy.gpu.device_id,
        renderer=renderer,
        gpu_diagnostics=tuple(sorted(gpu_diagnostics)),
    )


def _verify_smoke_report(
    report: Mapping[str, Any],
    install: DesktopInstallManifest,
    policy: NativePayloadPolicy,
    run_policy: RunPolicy,
    evidence_root: Path,
    processes: Sequence[ProcessObservation],
) -> tuple[str, tuple[str, ...]]:
    required = {
        "sourceCommit",
        "electron",
        "chrome",
        "node",
        "sidecarPid",
        "sidecarLinuxSandbox",
        "sessionGeneration",
        "reload",
        "rendererCrashRecovery",
        "rendererProbe",
        "uiProof",
        "uiProofScreenshot",
        "narrowUiProofScreenshot",
        "gpu",
        "gpuFeatureStatus",
        "metrics",
        "sidecarProcessHistory",
        "childProcessFailures",
        "security",
    }
    if not required <= report.keys():
        raise NativeAcceptanceError("production smoke report is incomplete")
    if report["sourceCommit"] != policy.source_commit:
        raise NativeAcceptanceError("production smoke source commit is stale")
    if report["electron"] != install.electron_version:
        raise NativeAcceptanceError("production smoke Electron version disagrees")
    for name in ("chrome", "node"):
        _require_text(report[name], f"production smoke {name} version")
    security = _mapping(report["security"], "security")
    expected_security = {
        "sandbox": True,
        "contextIsolation": True,
        "nodeIntegration": False,
        "webviewTag": False,
        "scheme": "tongs://app/index.html",
        "xwayland": True,
    }
    if security != expected_security:
        raise NativeAcceptanceError("production smoke security settings are unsafe")
    expected_screenshots = tuple(
        str((evidence_root / path).resolve(strict=False))
        for path in run_policy.screenshot_paths
    )
    if run_policy.review_number is None:
        if (
            len(expected_screenshots) != 1
            or report["uiProof"] is not None
            or report["uiProofScreenshot"] is not None
            or report["narrowUiProofScreenshot"] is not None
        ):
            raise NativeAcceptanceError("production smoke output set is unexpected")
    else:
        if len(expected_screenshots) != 3 or not isinstance(report["uiProof"], dict):
            raise NativeAcceptanceError("production smoke UI proof is incomplete")
        if report["uiProofScreenshot"] != expected_screenshots[1] or (
            report["narrowUiProofScreenshot"] != expected_screenshots[2]
        ):
            raise NativeAcceptanceError("production smoke UI output paths disagree")
        _verify_ui_proof(
            _mapping(report["uiProof"], "production smoke UI proof"),
            run_policy.review_number,
        )
    reload = _mapping(report["reload"], "reload")
    crash = _mapping(report["rendererCrashRecovery"], "renderer crash recovery")
    reload_initial = _positive_int(
        reload.get("initialSessionGeneration"), "reload initial"
    )
    reload_final = _positive_int(reload.get("finalSessionGeneration"), "reload final")
    crash_initial = _positive_int(
        crash.get("initialSessionGeneration"), "crash initial"
    )
    crash_final = _positive_int(crash.get("finalSessionGeneration"), "crash final")
    if not (
        reload_final == reload_initial + 1
        and crash_initial == reload_final
        and crash_final == crash_initial + 1
        and report["sessionGeneration"] == crash_final
        and crash.get("injection") == "SIGKILL"
    ):
        raise NativeAcceptanceError(
            "renderer reload and crash recovery sequence is invalid"
        )
    injected_pid = _positive_int(
        crash.get("injectedRendererPid"), "injected renderer PID"
    )
    if not any(
        item.pid == injected_pid and item.role == "renderer" for item in processes
    ):
        raise NativeAcceptanceError(
            "deliberately crashed renderer lacks live process evidence"
        )
    probes = (
        _mapping(reload.get("initialRendererProbe"), "initial renderer probe"),
        _mapping(reload.get("finalRendererProbe"), "reloaded renderer probe"),
        _mapping(crash.get("finalRendererProbe"), "recovered renderer probe"),
        _mapping(report["rendererProbe"], "final renderer probe"),
    )
    renderers = {_verify_renderer_probe(value, policy.gpu) for value in probes}
    if len(renderers) != 1:
        raise NativeAcceptanceError("renderer GPU identity changed across recovery")
    if report["childProcessFailures"] != []:
        raise NativeAcceptanceError(
            "production smoke contains an unexpected child failure"
        )
    history = report["sidecarProcessHistory"]
    if not isinstance(history, list) or len(history) != 2:
        raise NativeAcceptanceError("sidecar restart history is incomplete")
    for item in history:
        record = _mapping(item, "sidecar history")
        if (
            record.get("code") != 0
            or record.get("signal") is not None
            or record.get("unexpected") is not False
        ):
            raise NativeAcceptanceError("sidecar restart history contains a failure")
    gpu = _mapping(report["gpu"], "GPU information")
    diagnostics = _gpu_auxiliary_diagnostics(gpu.get("auxAttributes"))
    devices = gpu.get("gpuDevice")
    if not isinstance(devices, list):
        raise NativeAcceptanceError("production smoke GPU device list is missing")
    matches = [
        item
        for item in devices
        if isinstance(item, dict)
        and item.get("active") is True
        and item.get("vendorId") == policy.gpu.vendor_id
        and item.get("deviceId") == policy.gpu.device_id
    ]
    if len(matches) != 1:
        raise NativeAcceptanceError("expected physical GPU is not uniquely active")
    features = _mapping(report["gpuFeatureStatus"], "GPU feature status")
    for name, expected in policy.gpu.feature_status:
        if features.get(name) != expected:
            raise NativeAcceptanceError(f"GPU feature {name!r} is not hardware enabled")
    metrics = report["metrics"]
    if not isinstance(metrics, list):
        raise NativeAcceptanceError("production smoke process metrics are missing")
    types = [item.get("type") for item in metrics if isinstance(item, dict)]
    if types.count("GPU") != 1 or types.count("Tab") < 1 or types.count("Browser") != 1:
        raise NativeAcceptanceError(
            "production smoke lacks distinct GPU/renderer/browser metrics"
        )
    gpu_metric = next(
        item for item in metrics if isinstance(item, dict) and item.get("type") == "GPU"
    )
    if gpu_metric.get("serviceName") != "GPU":
        raise NativeAcceptanceError(
            "production smoke GPU process identity is incomplete"
        )
    return next(iter(renderers)), diagnostics


def _gpu_auxiliary_diagnostics(value: Any) -> tuple[str, ...]:
    if not isinstance(value, dict):
        return ("auxiliary-gpu-attributes-unavailable",)
    diagnostics = []
    implementation = value.get("glImplementationParts")
    if not isinstance(implementation, str):
        diagnostics.append("auxiliary-gl-implementation-unavailable")
    elif (
        "gl=none" in implementation.casefold()
        or "angle=none" in implementation.casefold()
    ):
        diagnostics.append("auxiliary-gl-implementation-reports-none")
    if value.get("inProcessGpu") is True:
        diagnostics.append("auxiliary-in-process-gpu-contradicts-live-process")
    if value.get("sandboxed") is False:
        diagnostics.append("auxiliary-sandbox-flag-contradicts-proc-status")
    return tuple(diagnostics)


def _verify_ui_proof(proof: Mapping[str, Any], review_number: int) -> None:
    _require_exact_keys(
        proof,
        {
            "provenance",
            "reviewNumber",
            "listedTitle",
            "selectedFile",
            "revision",
            "selectedAnchor",
            "unifiedSourceSample",
            "splitSourceSample",
        },
        "production smoke UI proof",
    )
    if proof["reviewNumber"] != review_number:
        raise NativeAcceptanceError("production smoke opened the wrong review")
    _require_text(proof["provenance"], "production smoke UI provenance")
    _require_text(proof["listedTitle"], "production smoke review title")
    _require_text(proof["selectedFile"], "production smoke selected file")
    revision = _mapping(proof["revision"], "production smoke revision")
    _require_exact_keys(
        revision, {"head_sha", "base_sha", "start_sha"}, "review revision"
    )
    _require_sha1(revision["head_sha"], "review head SHA")
    _require_sha1(revision["base_sha"], "review base SHA")
    if revision["start_sha"] is not None:
        _require_sha1(revision["start_sha"], "review start SHA")
    anchor = _mapping(proof["selectedAnchor"], "production smoke selected anchor")
    _require_exact_keys(
        anchor, {"side", "old_line", "new_line"}, "production smoke selected anchor"
    )
    if anchor["side"] not in {"old", "new"}:
        raise NativeAcceptanceError("production smoke selected anchor side is invalid")
    lines = (anchor["old_line"], anchor["new_line"])
    if all(value is None for value in lines) or any(
        value is not None and (not isinstance(value, str) or not value.isdecimal())
        for value in lines
    ):
        raise NativeAcceptanceError(
            "production smoke selected anchor lines are invalid"
        )
    for name in ("unifiedSourceSample", "splitSourceSample"):
        sample = proof[name]
        if (
            not isinstance(sample, list)
            or not sample
            or len(sample) > 12
            or any(not isinstance(value, str) for value in sample)
        ):
            raise NativeAcceptanceError("production smoke source sample is invalid")


def _verify_renderer_probe(probe: Mapping[str, Any], gpu: GpuPolicy) -> str:
    if (
        probe.get("processGlobal") != "undefined"
        or probe.get("requireGlobal") != "undefined"
    ):
        raise NativeAcceptanceError("renderer isolation globals are exposed")
    vendor = probe.get("webglVendor")
    renderer = probe.get("webglRenderer")
    if not isinstance(vendor, str) or not isinstance(renderer, str):
        raise NativeAcceptanceError("renderer WebGL physical identity is missing")
    combined = f"{vendor} {renderer}".casefold()
    if any(token in combined for token in SOFTWARE_RENDERERS):
        raise NativeAcceptanceError("renderer selected a software GPU")
    if any(token.casefold() not in combined for token in gpu.renderer_tokens):
        raise NativeAcceptanceError(
            "renderer does not identify the expected physical GPU"
        )
    return renderer


def _verify_raw_process_argv(process: ProcessObservation) -> None:
    raw = process.raw_argv
    if raw is None:
        raise NativeAcceptanceError("raw process arguments are missing")
    if len(raw) > MAX_ARGUMENTS or any(
        len(item.encode("utf-8")) > MAX_ARGUMENT_BYTES for item in raw
    ):
        raise NativeAcceptanceError("raw process arguments exceed bounded limits")
    process_type = COMPACTED_PROCESS_ROLE_TYPES.get(process.role)
    expected_type = None if process_type is None else f"--type={process_type}"
    type_arguments = tuple(
        argument for argument in process.argv if argument.startswith("--type=")
    )
    if process_type is not None and (
        not process.argv
        or process.argv[0] != process.executable
        or type_arguments != (expected_type,)
    ):
        raise NativeAcceptanceError(
            "process role differs from canonical type arguments"
        )
    if raw == process.argv:
        return
    expected_raw = (" ".join(process.argv),)
    if expected_type is None or raw != expected_raw:
        raise NativeAcceptanceError(
            "raw process arguments differ from canonical arguments"
        )


def _verify_process_observations(
    processes: Sequence[ProcessObservation],
    report: Mapping[str, Any],
    payload_root: Path,
    evidence_root: Path,
    allowed_executables: set[str],
    policy: NativePayloadPolicy,
    run_policy: RunPolicy,
) -> None:
    if not processes or len(processes) > MAX_PROCESSES:
        raise NativeAcceptanceError("owned process observation count is invalid")
    by_pid = {item.pid: item for item in processes}
    if len(by_pid) != len(processes):
        raise NativeAcceptanceError("owned process observations contain duplicate PIDs")
    if any(
        type(item.pid) is not int
        or item.pid <= 0
        or type(item.ppid) is not int
        or item.ppid < 0
        or type(item.process_group) is not int
        or item.process_group <= 0
        or type(item.start_time_ticks) is not int
        or item.start_time_ticks <= 0
        or any(
            type(value) is not int or value < 0
            for value in (
                item.sandbox.no_new_privs,
                item.sandbox.seccomp,
                item.sandbox.seccomp_filters,
            )
        )
        for item in processes
    ):
        raise NativeAcceptanceError("owned process observations contain invalid values")
    main = [item for item in processes if item.role == "browser"]
    sidecars = [item for item in processes if item.role == "python-sidecar"]
    if len(main) != 1 or len(sidecars) < 3:
        raise NativeAcceptanceError(
            "browser or repeated sidecar process evidence is incomplete"
        )
    if any(item.process_group != main[0].pid for item in processes):
        raise NativeAcceptanceError("owned process escaped the launch process group")
    _verify_owned_ancestry(by_pid, main[0].pid)
    launcher = str((payload_root / FIXED_LAUNCHER_PATH).resolve(strict=True))
    if main[0].executable != launcher:
        raise NativeAcceptanceError(
            "browser executable is outside the installed payload"
        )
    if not main[0].argv or main[0].argv[0] != launcher:
        raise NativeAcceptanceError("browser argv does not name the installed launcher")
    if main[0].cwd != str(Path(policy.safe_cwd).resolve(strict=True)):
        raise NativeAcceptanceError(
            "browser working directory differs from launch policy"
        )
    _verify_main_argv(main[0].argv, policy, run_policy, evidence_root)
    for process in processes:
        _verify_raw_process_argv(process)
        if len(process.argv) > MAX_ARGUMENTS or any(
            len(item.encode("utf-8")) > MAX_ARGUMENT_BYTES for item in process.argv
        ):
            raise NativeAcceptanceError("owned process arguments exceed bounded limits")
        if any("node_modules" in PurePosixPath(item).parts for item in process.argv):
            raise NativeAcceptanceError(
                "owned process arguments reference node_modules"
            )
        switches = {
            item.split("=", 1)[0] for item in process.argv if item.startswith("--")
        }
        if switches & FORBIDDEN_SWITCHES:
            raise NativeAcceptanceError(
                "owned process uses a forbidden security/GPU switch"
            )
        if process.role == "python-sidecar":
            expected_prefix = (
                policy.core.requested_executable,
                "-E",
                "-P",
                "-m",
                "tongs.desktop.sidecar",
            )
            if process.argv[:5] != expected_prefix:
                raise NativeAcceptanceError(
                    "sidecar launch arguments do not bind installed core"
                )
            if process.executable != policy.core.proc_executable:
                raise NativeAcceptanceError(
                    "sidecar process executable differs from core binding"
                )
            if process.cwd != str(Path(policy.safe_cwd).resolve(strict=True)):
                raise NativeAcceptanceError(
                    "sidecar working directory differs from launch policy"
                )
        else:
            if not process.argv or process.argv[0] != process.executable:
                raise NativeAcceptanceError(
                    "Electron canonical argv does not name its executable"
                )
            if process.executable not in allowed_executables:
                raise NativeAcceptanceError(
                    "owned Electron process executable is undeclared"
                )
    metrics = report["metrics"]
    assert isinstance(metrics, list)
    for expected_type in ("Browser", "GPU", "Tab"):
        matches = [
            item
            for item in metrics
            if isinstance(item, dict) and item.get("type") == expected_type
        ]
        if expected_type != "Tab" and len(matches) != 1:
            raise NativeAcceptanceError(
                "production process metric identity is ambiguous"
            )
        for metric in matches:
            pid = _positive_int(metric.get("pid"), f"{expected_type} metric PID")
            role = {"Browser": "browser", "GPU": "gpu", "Tab": "renderer"}[
                expected_type
            ]
            process = by_pid.get(pid)
            if process is None or process.role != role:
                raise NativeAcceptanceError(
                    "smoke metric PID lacks owned process evidence"
                )
            if expected_type in {"GPU", "Tab"}:
                linux = _mapping(metric.get("linuxSandbox"), "metric Linux sandbox")
                expected_status = {
                    "noNewPrivs": process.sandbox.no_new_privs,
                    "seccomp": process.sandbox.seccomp,
                    "seccompFilters": process.sandbox.seccomp_filters,
                }
                if (
                    linux != expected_status
                    or expected_status["noNewPrivs"] != 1
                    or expected_status["seccomp"] != 2
                    or expected_status["seccompFilters"] < 1
                ):
                    raise NativeAcceptanceError(
                        "renderer or GPU sandbox evidence is insufficient"
                    )
                parent = by_pid.get(process.ppid)
                if parent is None:
                    raise NativeAcceptanceError(
                        "sandboxed child lacks observed parent context"
                    )
                parent_status = parent.sandbox
                if (
                    min(
                        parent_status.no_new_privs,
                        parent_status.seccomp,
                        parent_status.seccomp_filters,
                    )
                    < 0
                ):
                    raise NativeAcceptanceError("sandbox parent status is incomplete")
                transitioned = (
                    process.sandbox.seccomp_filters > parent_status.seccomp_filters
                    or (
                        process.sandbox.no_new_privs == 1
                        and parent_status.no_new_privs == 0
                    )
                )
                if not transitioned:
                    raise NativeAcceptanceError(
                        "child sandbox is indistinguishable from inherited runner filters"
                    )
    sidecar_pid = _positive_int(report["sidecarPid"], "sidecar PID")
    if sidecar_pid not in by_pid or by_pid[sidecar_pid].role != "python-sidecar":
        raise NativeAcceptanceError(
            "final sidecar PID lacks installed process evidence"
        )
    sidecar = by_pid[sidecar_pid]
    sidecar_linux = _mapping(report["sidecarLinuxSandbox"], "sidecar Linux sandbox")
    if sidecar_linux != {
        "noNewPrivs": sidecar.sandbox.no_new_privs,
        "seccomp": sidecar.sandbox.seccomp,
        "seccompFilters": sidecar.sandbox.seccomp_filters,
    }:
        raise NativeAcceptanceError("sidecar sandbox report differs from live process")


def _verify_owned_ancestry(
    processes: Mapping[int, ProcessObservation], root_pid: int
) -> None:
    for process in processes.values():
        if process.pid == root_pid:
            continue
        current = process
        seen = {current.pid}
        for _ in range(MAX_PROCESSES):
            parent = processes.get(current.ppid)
            if parent is None:
                raise NativeAcceptanceError("owned process lacks ancestry to browser")
            if parent.pid == root_pid:
                break
            if parent.pid in seen:
                raise NativeAcceptanceError("owned process ancestry contains a cycle")
            seen.add(parent.pid)
            current = parent
        else:
            raise NativeAcceptanceError("owned process ancestry exceeds its bound")


def _verify_main_argv(
    argv: tuple[str, ...],
    policy: NativePayloadPolicy,
    run: RunPolicy,
    evidence_root: Path,
) -> None:
    root = _absolute_directory(evidence_root, "evidence root")
    expected = [
        argv[0],
        "--ozone-platform=x11",
        f"--user-data-dir={(root / run.profile_path).resolve(strict=False)}",
        "--tongs-python-executable",
        policy.core.requested_executable,
        "--tongs-core-version",
        policy.core.tongs_version,
        "--tongs-safe-cwd",
        policy.safe_cwd,
        "--tongs-smoke-report",
        str((root / run.report_path).resolve(strict=False)),
        "--tongs-smoke-source-commit",
        policy.source_commit,
    ]
    if run.review_number is not None:
        expected.extend(("--tongs-smoke-review-number", str(run.review_number)))
    if argv != tuple(expected):
        raise NativeAcceptanceError("browser arguments do not match launch policy")


def _verify_captured_outputs(
    evidence_root: Path,
    run: RunPolicy,
    expected: Sequence[CapturedOutput],
    expected_uid: int,
) -> None:
    root = _private_evidence_root(evidence_root, expected_uid)
    for value in expected:
        maximum = (
            MAX_REPORT_BYTES if value.path == run.report_path else MAX_OUTPUT_BYTES
        )
        actual_result = _read_identity(
            root,
            value.path,
            maximum_size=maximum,
            expected_mode=0o600,
            return_bytes=value.path != run.report_path,
            expected_uid=expected_uid,
        )
        if isinstance(actual_result, tuple):
            actual, document = actual_result
            screenshot_index = run.screenshot_paths.index(value.path)
            _verify_png(document, run.screenshot_dimensions[screenshot_index])
        else:
            actual = actual_result
        if actual != _file_identity(value):
            raise NativeAcceptanceError(
                f"captured output {value.path!r} changed after creation"
            )


def _verify_png(document: bytes, expected_dimensions: tuple[int, int]) -> None:
    if len(document) < 57 or document[:8] != b"\x89PNG\r\n\x1a\n":
        raise NativeAcceptanceError("captured screenshot is not a complete PNG")
    offset = 8
    chunk_index = 0
    dimensions: tuple[int, int] | None = None
    saw_idat = False
    saw_iend = False
    compressed = bytearray()
    channels = 0
    while offset < len(document):
        if len(document) - offset < 12:
            raise NativeAcceptanceError("captured screenshot has a truncated PNG chunk")
        length = struct.unpack(">I", document[offset : offset + 4])[0]
        chunk_type = document[offset + 4 : offset + 8]
        end = offset + 12 + length
        if length > MAX_OUTPUT_BYTES or end > len(document):
            raise NativeAcceptanceError("captured screenshot has a truncated PNG chunk")
        data = document[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", document[offset + 8 + length : end])[0]
        if zlib.crc32(chunk_type + data) & 0xFFFFFFFF != expected_crc:
            raise NativeAcceptanceError("captured screenshot PNG checksum is invalid")
        if chunk_index == 0:
            if chunk_type != b"IHDR" or length != 13:
                raise NativeAcceptanceError(
                    "captured screenshot lacks one initial IHDR"
                )
            dimensions = struct.unpack(">II", data[:8])
            if data[8] != 8 or data[9] not in {2, 6} or data[10:] != bytes((0, 0, 0)):
                raise NativeAcceptanceError(
                    "captured screenshot PNG encoding is unsupported"
                )
            channels = 3 if data[9] == 2 else 4
        elif chunk_type == b"IHDR":
            raise NativeAcceptanceError("captured screenshot repeats IHDR")
        if chunk_type == b"IDAT":
            if not data:
                raise NativeAcceptanceError("captured screenshot has an empty IDAT")
            saw_idat = True
            compressed.extend(data)
        if chunk_type == b"IEND":
            if length != 0 or end != len(document):
                raise NativeAcceptanceError(
                    "captured screenshot has invalid trailing data"
                )
            saw_iend = True
        elif saw_iend:
            raise NativeAcceptanceError("captured screenshot has data after IEND")
        chunk_index += 1
        offset = end
    if dimensions != expected_dimensions:
        raise NativeAcceptanceError("captured screenshot dimensions differ from policy")
    if not saw_idat or not saw_iend:
        raise NativeAcceptanceError("captured screenshot is not a complete PNG")
    width, height = expected_dimensions
    decoded_size = (1 + width * channels) * height
    if decoded_size > MAX_PNG_DECODED_BYTES:
        raise NativeAcceptanceError("captured screenshot decoded size exceeds policy")
    try:
        decompressor = zlib.decompressobj()
        decoded = decompressor.decompress(bytes(compressed), decoded_size + 1)
    except zlib.error as error:
        raise NativeAcceptanceError(
            "captured screenshot image data is invalid"
        ) from error
    if (
        len(decoded) != decoded_size
        or not decompressor.eof
        or decompressor.unused_data
        or decompressor.unconsumed_tail
        or any(
            decoded[offset] > 4
            for offset in range(0, decoded_size, 1 + width * channels)
        )
    ):
        raise NativeAcceptanceError("captured screenshot image data is invalid")


def _validate_core_binding(binding: CoreBinding, policy: NativePayloadPolicy) -> None:
    for name, value in (
        ("requested interpreter", binding.requested_executable),
        ("process interpreter", binding.proc_executable),
        ("system Python target", binding.system_python_target),
        ("base Python executable", binding.base_executable),
        ("sys.prefix", binding.sys_prefix),
        ("package root", binding.package_root),
        ("distribution path", binding.distribution_path),
    ):
        _require_absolute_path(value, name)
    if not binding.site_packages or len(binding.site_packages) > 8:
        raise NativeAcceptanceError("installed site-packages paths are incomplete")
    if len(set(binding.site_packages)) != len(binding.site_packages):
        raise NativeAcceptanceError("installed site-packages paths contain duplicates")
    for value in binding.site_packages:
        _require_absolute_path(value, "site-packages")
    _require_text(binding.tongs_version, "Tongs version")
    if _VERSION.fullmatch(binding.tongs_version) is None:
        raise NativeAcceptanceError("Tongs version is malformed")
    _require_sha1(binding.source_commit, "core source commit")
    _require_sha1(binding.source_tree, "core source tree")
    _require_sha256(binding.wheel_sha256, "core wheel")
    if (
        not binding.package_members
        or len(binding.package_members) > MAX_CORE_MEMBERS
        or len({item[0] for item in binding.package_members})
        != len(binding.package_members)
    ):
        raise NativeAcceptanceError("installed package member policy is invalid")
    for path, size, digest in binding.package_members:
        _require_relative_path(path, "installed package member")
        if not path.startswith("tongs/") or type(size) is not int or size < 0:
            raise NativeAcceptanceError("installed package member policy is invalid")
        _require_sha256(digest, "installed package member")
    if (
        not binding.archive_members
        or len(binding.archive_members) > MAX_CORE_MEMBERS
        or len({item[0] for item in binding.archive_members})
        != len(binding.archive_members)
    ):
        raise NativeAcceptanceError("wheel archive member policy is invalid")
    for path, digest in binding.archive_members:
        _require_relative_path(path, "wheel archive member")
        _require_sha256(digest, "wheel archive member")
    if binding.generated_exclusion != "only __pycache__/*.pyc":
        raise NativeAcceptanceError("generated-file exclusion policy is invalid")
    if type(binding.installed_uid) is not int or binding.installed_uid < 0:
        raise NativeAcceptanceError("installed core owner policy is invalid")
    if len(binding.generated_members) > MAX_CORE_MEMBERS or len(
        {item[0] for item in binding.generated_members}
    ) != len(binding.generated_members):
        raise NativeAcceptanceError("generated member policy is invalid")
    for path, size, digest in binding.generated_members:
        if (
            not _is_generated_bytecode(path)
            or type(size) is not int
            or size < 1
            or size > MAX_CORE_FILE_BYTES
        ):
            raise NativeAcceptanceError("generated member policy is invalid")
        _require_sha256(digest, "generated member")
    if (
        binding.source_commit != policy.source_commit
        or binding.source_tree != policy.source_tree
    ):
        raise NativeAcceptanceError(
            "installed core source identity differs from candidate"
        )
    _verify_direct_url(binding.direct_url, binding.wheel_sha256)


def _verify_direct_url(value: Mapping[str, Any] | None, wheel_sha256: str) -> None:
    if value is None:
        raise NativeAcceptanceError("installed core lacks wheel direct_url metadata")
    if not isinstance(value, dict) or set(value) != {"archive_info", "url"}:
        raise NativeAcceptanceError("installed core direct_url is not a wheel archive")
    _require_text(value["url"], "installed wheel URL")
    archive = _mapping(value["archive_info"], "installed wheel archive info")
    if set(archive) - {"hash", "hashes"}:
        raise NativeAcceptanceError(
            "installed wheel archive metadata has unknown fields"
        )
    hashes = archive.get("hashes")
    if not isinstance(hashes, dict) or hashes != {"sha256": wheel_sha256}:
        raise NativeAcceptanceError("installed wheel direct_url digest disagrees")
    legacy_hash = archive.get("hash")
    if legacy_hash is not None and legacy_hash != f"sha256={wheel_sha256}":
        raise NativeAcceptanceError("installed wheel legacy digest disagrees")


def _derived_screenshot_paths(run: RunPolicy) -> tuple[str, ...]:
    if run.report_path.endswith(".json"):
        stem = run.report_path[:-5]
        main = f"{stem}.png"
        ui = f"{stem}.ui.png"
        narrow = f"{stem}.narrow.ui.png"
    else:
        main = f"{run.report_path}.png"
        ui = f"{run.report_path}.ui.png"
        narrow = f"{run.report_path}.narrow.ui.png"
    return (main,) if run.review_number is None else (main, ui, narrow)


def _captured_output(value: FileIdentity) -> CapturedOutput:
    return CapturedOutput(
        path=value.path,
        size=value.size,
        sha256=value.sha256,
        mode=value.mode,
        device=value.device,
        inode=value.inode,
        uid=value.uid,
    )


def _file_identity(value: CapturedOutput) -> FileIdentity:
    return FileIdentity(
        path=value.path,
        size=value.size,
        sha256=value.sha256,
        mode=value.mode,
        device=value.device,
        inode=value.inode,
        uid=value.uid,
    )


def _runtime_paths(
    root: Path, maximum_entries: int, *, expected_uid: int
) -> tuple[set[str], set[str]]:
    runtime = root / "runtime"
    try:
        status = runtime.stat(follow_symlinks=False)
    except OSError as error:
        raise NativeAcceptanceError("installed runtime directory is missing") from error
    if (
        not stat.S_ISDIR(status.st_mode)
        or runtime.is_symlink()
        or status.st_uid != expected_uid
        or stat.S_IMODE(status.st_mode) != 0o755
    ):
        raise NativeAcceptanceError("installed runtime must be a real directory")
    files: set[str] = set()
    folded_files: set[str] = set()
    directories = {"runtime"}
    folded_directories = {"runtime"}
    pending = [runtime]
    entries_seen = 0
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as iterator:
                entries = tuple(iterator)
        except OSError as error:
            raise NativeAcceptanceError(
                "installed runtime cannot be enumerated"
            ) from error
        for entry in entries:
            entries_seen += 1
            if entries_seen > maximum_entries:
                raise NativeAcceptanceError(
                    "installed runtime entry count exceeds manifest"
                )
            try:
                item_status = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise NativeAcceptanceError(
                    "installed runtime entry cannot be inspected"
                ) from error
            if stat.S_ISDIR(item_status.st_mode):
                if (
                    item_status.st_uid != expected_uid
                    or stat.S_IMODE(item_status.st_mode) != 0o755
                ):
                    raise NativeAcceptanceError(
                        "installed runtime directory ownership or mode is invalid"
                    )
                relative = Path(entry.path).relative_to(root).as_posix()
                _require_relative_path(relative, "installed runtime directory")
                folded = relative.casefold()
                if "node_modules" in PurePosixPath(folded).parts:
                    raise NativeAcceptanceError(
                        "installed runtime contains node_modules"
                    )
                if folded in folded_directories:
                    raise NativeAcceptanceError("installed runtime directories collide")
                directories.add(relative)
                folded_directories.add(folded)
                pending.append(Path(entry.path))
            elif stat.S_ISREG(item_status.st_mode):
                relative = Path(entry.path).relative_to(root).as_posix()
                _require_relative_path(relative, "installed runtime path")
                if relative.casefold() in folded_files:
                    raise NativeAcceptanceError("installed runtime paths collide")
                files.add(relative)
                folded_files.add(relative.casefold())
            else:
                raise NativeAcceptanceError(
                    "installed runtime contains a link or special file"
                )
    return files, directories


def _enumerate_tree(root: Path, maximum_entries: int, *, expected_uid: int) -> set[str]:
    files: set[str] = set()
    folded: set[str] = set()
    pending = [root]
    entries_seen = 0
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as iterator:
                entries = tuple(iterator)
        except OSError as error:
            raise NativeAcceptanceError(
                "installed core tree cannot be enumerated"
            ) from error
        for entry in entries:
            entries_seen += 1
            if entries_seen > maximum_entries:
                raise NativeAcceptanceError("installed core tree exceeds member bounds")
            try:
                item_status = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise NativeAcceptanceError(
                    "installed core member cannot be inspected"
                ) from error
            relative = Path(entry.path).relative_to(root).as_posix()
            _require_relative_path(relative, "installed core member")
            if stat.S_ISDIR(item_status.st_mode):
                if (
                    item_status.st_uid != expected_uid
                    or stat.S_IMODE(item_status.st_mode) != 0o755
                ):
                    raise NativeAcceptanceError(
                        "installed core directory ownership or mode is invalid"
                    )
                pending.append(Path(entry.path))
            elif stat.S_ISREG(item_status.st_mode):
                if relative.casefold() in folded:
                    raise NativeAcceptanceError("installed core member paths collide")
                folded.add(relative.casefold())
                files.add(relative)
            else:
                raise NativeAcceptanceError(
                    "installed core contains a link or special file"
                )
    return files


def _read_identity(
    root: Path,
    relative: str,
    *,
    maximum_size: int,
    expected_mode: int,
    return_bytes: bool = False,
    allow_empty: bool = False,
    expected_uid: int | None = None,
) -> FileIdentity | tuple[FileIdentity, bytes]:
    _require_relative_path(relative, "bound file path")
    try:
        descriptor = _open_regular_beneath(root, relative)
    except OSError as error:
        raise NativeAcceptanceError(
            f"bound file {relative!r} cannot be opened safely"
        ) from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise NativeAcceptanceError(f"bound file {relative!r} is not regular")
        mode = stat.S_IMODE(before.st_mode)
        if mode != expected_mode:
            raise NativeAcceptanceError(
                f"bound file {relative!r} has an unexpected mode"
            )
        if expected_uid is not None and before.st_uid != expected_uid:
            raise NativeAcceptanceError(
                f"bound file {relative!r} has an unexpected owner"
            )
        if (before.st_size < 1 and not allow_empty) or before.st_size > maximum_size:
            raise NativeAcceptanceError(
                f"bound file {relative!r} size is outside limits"
            )
        digest = hashlib.sha256()
        chunks = [] if return_bytes else None
        total = 0
        while True:
            chunk = os.read(descriptor, HASH_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_size:
                raise NativeAcceptanceError(
                    f"bound file {relative!r} exceeds its limit"
                )
            digest.update(chunk)
            if chunks is not None:
                chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_uid,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_uid,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise NativeAcceptanceError(f"bound file {relative!r} changed while read")
        identity = FileIdentity(
            relative,
            total,
            digest.hexdigest(),
            mode,
            before.st_dev,
            before.st_ino,
            before.st_uid,
        )
        if chunks is not None:
            return identity, b"".join(chunks)
        return identity
    finally:
        os.close(descriptor)


def _open_regular_beneath(root: Path, relative: str) -> int:
    """Open a file without following a symlink in any path component."""

    directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY
    file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
        file_flags |= os.O_NOFOLLOW
    root_fd = os.open(root, directory_flags)
    current_fd = root_fd
    try:
        parts = PurePosixPath(relative).parts
        for part in parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=current_fd)
            if current_fd != root_fd:
                os.close(current_fd)
            current_fd = next_fd
        return os.open(parts[-1], file_flags, dir_fd=current_fd)
    finally:
        if current_fd != root_fd:
            os.close(current_fd)
        os.close(root_fd)


def _decode_json(document: bytes, label: str) -> Mapping[str, Any]:
    if not document or len(document) > MAX_REPORT_BYTES:
        raise NativeAcceptanceError(f"{label} size is outside limits")
    try:
        value = json.loads(
            document.decode("utf-8", errors="strict"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise NativeAcceptanceError(f"{label} is not valid bounded JSON") from error
    if not isinstance(value, dict):
        raise NativeAcceptanceError(f"{label} must be a JSON object")
    _validate_json_shape(value, label)
    return value


def _validate_json_shape(value: Any, label: str) -> None:
    pending: list[tuple[Any, int]] = [(value, 1)]
    values = 0
    while pending:
        current, depth = pending.pop()
        values += 1
        if values > MAX_JSON_VALUES or depth > MAX_JSON_DEPTH:
            raise NativeAcceptanceError(f"{label} exceeds JSON structural limits")
        if isinstance(current, dict):
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise NativeAcceptanceError("native evidence contains duplicate JSON keys")
        value[key] = item
    return value


def _reject_constant(value: str) -> NoReturn:
    raise NativeAcceptanceError(f"native evidence contains {value!r}")


def _digest(document: bytes) -> str:
    if not isinstance(document, bytes):
        raise NativeAcceptanceError("manifest input must be bytes")
    if not document or len(document) > MAX_REPORT_BYTES:
        raise NativeAcceptanceError("manifest input size is outside limits")
    return hashlib.sha256(document).hexdigest()


def _absolute_directory(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise NativeAcceptanceError(f"{label} must be an absolute real directory")
    try:
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            if current.is_symlink():
                raise NativeAcceptanceError(
                    f"{label} must not traverse a symbolic link"
                )
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise NativeAcceptanceError(f"{label} does not exist") from error
    if not resolved.is_dir():
        raise NativeAcceptanceError(f"{label} must be a directory")
    return resolved


def _private_evidence_root(path: Path, expected_uid: int) -> Path:
    root = _absolute_directory(path, "evidence root")
    status = root.stat(follow_symlinks=False)
    if status.st_uid != expected_uid or stat.S_IMODE(status.st_mode) != 0o700:
        raise NativeAcceptanceError("evidence root ownership or mode is invalid")
    return root


def _require_absolute_directory(value: str, label: str) -> None:
    _absolute_directory(Path(value), label)


def _require_absolute_path(value: str, label: str) -> None:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise NativeAcceptanceError(f"{label} must be an absolute path")
    if "node_modules" in Path(value).parts:
        raise NativeAcceptanceError(f"{label} must not use node_modules")


def _require_within(parent: Path, child: Path, label: str) -> None:
    if not _is_strictly_within(parent, child):
        raise NativeAcceptanceError(f"{label} is outside the installed prefix")


def _is_strictly_within(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return child != parent


def _require_relative_path(value: str, label: str) -> None:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512:
        raise NativeAcceptanceError(f"{label} is invalid")
    path = PurePosixPath(value)
    if (
        path.as_posix() != value
        or path.is_absolute()
        or "\\" in value
        or any(ord(character) < 32 for character in value)
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise NativeAcceptanceError(f"{label} is unsafe")


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512:
        raise NativeAcceptanceError(f"{label} is invalid")
    return value


def _require_sha1(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA1.fullmatch(value) is None:
        raise NativeAcceptanceError(f"{label} must be a full lowercase Git SHA")


def _require_sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise NativeAcceptanceError(f"{label} must be a lowercase SHA-256")


def _require_positive_size(value: int, label: str) -> None:
    if type(value) is not int or value <= 0 or value > MAX_PRIOR_INPUT_BYTES:
        raise NativeAcceptanceError(f"{label} is outside bounded limits")


def _positive_int(value: Any, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise NativeAcceptanceError(f"{label} must be a positive integer")
    return value


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise NativeAcceptanceError(f"{label} must be an object")
    return value


def _require_exact_keys(value: Mapping[str, Any], keys: set[str], label: str) -> None:
    if set(value) != keys:
        raise NativeAcceptanceError(f"{label} fields are incomplete or unknown")
