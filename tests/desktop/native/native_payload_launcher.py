"""Bounded launcher and live-process collector for archive payload acceptance.

This helper must itself run inside the approved transient cgroup. It observes only
the process group it creates and delegates evidence decisions to the pure verifier.
The installed RPM layout has a separate source-bound contract and is not launched
through this per-user archive helper.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from tests.integration.desktop.native_payload_acceptance import (
    CHROMIUM_ZYGOTE_ARGV0,
    COMPACTED_PROCESS_ROLE_TYPES,
    MAX_ARGUMENT_BYTES,
    MAX_ARGUMENTS,
    MAX_PROCESSES,
    CoreBinding,
    CoreSnapshot,
    NativeAcceptanceError,
    NativeAcceptanceResult,
    NativePayloadPolicy,
    NativeRunObservation,
    PayloadSnapshot,
    ProcessObservation,
    RunPolicy,
    SandboxStatus,
    accepted_compact_titles,
    capture_core_snapshot,
    capture_expected_outputs,
    capture_payload_snapshot,
    chromium_argv_title,
    parse_bound_manifests,
    validate_policy,
    verify_core_observation,
    verify_native_acceptance,
    verify_prior_inputs,
)
from tongs.desktop.artifact_contract import FIXED_LAUNCHER_PATH, DesktopInstallManifest

CGROUP_MEMORY_MAX = 1024 * 1024 * 1024
CGROUP_SWAP_MAX = 0
CGROUP_TASKS_MAX = 64
NODE_HEAP_MIB = 512
MAX_LOG_BYTES = 2 * 1024 * 1024
MAX_FAILURE_DIAGNOSTIC_BYTES = 4 * 1024
MAX_PROFILE_BYTES = 256 * 1024 * 1024
MAX_PROFILE_ENTRIES = 10_000
MAX_SYSTEM_PROCESS_SCAN = 65_536
POLL_SECONDS = 0.02


class _ProcessGroupAuthorityLost(NativeAcceptanceError):
    """The fresh session leader no longer pins its numeric process-group ID."""


PRESERVED_ENVIRONMENT = frozenset(
    {
        "AT_SPI_BUS_ADDRESS",
        "DBUS_SESSION_BUS_ADDRESS",
        "DESKTOP_SESSION",
        "DISPLAY",
        "GDK_BACKEND",
        "HOME",
        "KDE_FULL_SESSION",
        "KDE_SESSION_VERSION",
        "LANG",
        "LOGNAME",
        "SHELL",
        "TERM",
        "TZ",
        "USER",
        "WAYLAND_DISPLAY",
        "XAUTHORITY",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CURRENT_DESKTOP",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
        "XDG_SESSION_TYPE",
    }
)


def run_native_acceptance(
    *,
    payload_root: Path,
    evidence_root: Path,
    install_document: bytes,
    release_document: bytes,
    policy: NativePayloadPolicy,
) -> tuple[NativeAcceptanceResult, tuple[NativeRunObservation, ...]]:
    """Execute the bounded acceptance sequence for an exact per-user archive."""

    verify_transient_guard()
    payload_absolute = payload_root.resolve(strict=True)
    evidence_absolute = evidence_root.resolve(strict=True)
    if (
        payload_absolute == evidence_absolute
        or payload_absolute in evidence_absolute.parents
        or evidence_absolute in payload_absolute.parents
    ):
        raise NativeAcceptanceError(
            "installed payload and mutable evidence roots must not overlap"
        )
    install, _release = parse_bound_manifests(
        install_document, release_document, policy
    )
    verify_prior_inputs(evidence_root, policy)
    initial_payload = capture_payload_snapshot(
        payload_root,
        install,
        expected_uid=policy.payload_uid,
        root_mode=policy.payload_root_mode,
        install_manifest_sha256=policy.install_manifest_sha256,
    )
    initial_core = capture_core_snapshot(policy.core)
    core_observation = probe_installed_core(policy.core, policy)
    if capture_core_snapshot(policy.core) != initial_core:
        raise NativeAcceptanceError("installed core changed during identity probe")
    observations = tuple(
        launch_native_run(
            payload_root=payload_root,
            evidence_root=evidence_root,
            install=install,
            policy=policy,
            run=run,
            initial_payload=initial_payload,
            initial_core=initial_core,
        )
        for run in policy.runs
    )
    result = verify_native_acceptance(
        payload_root=payload_root,
        evidence_root=evidence_root,
        install=install,
        policy=policy,
        initial_payload=initial_payload,
        initial_core=initial_core,
        core_observation=core_observation,
        observations=observations,
    )
    return result, observations


def verify_transient_guard(
    *,
    proc_cgroup: Path = Path("/proc/self/cgroup"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> Path:
    """Require the documented cgroup and Node heap bounds before a launch."""

    lines = proc_cgroup.read_text(encoding="utf-8").splitlines()
    unified = [line.partition("::")[2] for line in lines if line.startswith("0::")]
    if (
        len(unified) != 1
        or not unified[0].startswith("/")
        or ".." in unified[0].split("/")
    ):
        raise NativeAcceptanceError("launcher is not in one bounded unified cgroup")
    relative = unified[0].lstrip("/")
    cgroup = cgroup_root / relative
    expected = {
        "memory.max": CGROUP_MEMORY_MAX,
        "memory.swap.max": CGROUP_SWAP_MAX,
        "pids.max": CGROUP_TASKS_MAX,
    }
    for name, limit in expected.items():
        try:
            value = (cgroup / name).read_text(encoding="ascii").strip()
        except OSError as error:
            raise NativeAcceptanceError(
                f"cgroup limit {name!r} is unreadable"
            ) from error
        if value == "max" or not value.isascii() or not value.isdecimal():
            raise NativeAcceptanceError(f"cgroup limit {name!r} is unbounded")
        if int(value) > limit:
            raise NativeAcceptanceError(f"cgroup limit {name!r} exceeds policy")
    try:
        node_options = shlex.split(os.environ.get("NODE_OPTIONS", ""))
    except ValueError as error:
        raise NativeAcceptanceError("NODE_OPTIONS cannot be parsed") from error
    heap_values = [
        item.partition("=")[2]
        for item in node_options
        if item.startswith("--max-old-space-size=")
    ]
    if heap_values != [str(NODE_HEAP_MIB)]:
        raise NativeAcceptanceError("NODE_OPTIONS lacks the exact 512 MiB heap bound")
    return cgroup


def native_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build the explicit production-launch environment without code injection hooks."""

    candidate = os.environ if source is None else source
    result = {
        key: value
        for key, value in candidate.items()
        if key in PRESERVED_ENVIRONMENT or key.startswith("LC_")
    }
    result["PATH"] = "/usr/local/bin:/usr/bin:/bin"
    result["NODE_OPTIONS"] = f"--max-old-space-size={NODE_HEAP_MIB}"
    return result


def probe_installed_core(
    binding: CoreBinding, policy: NativePayloadPolicy
) -> Mapping[str, Any]:
    """Reprobe an installed interpreter with environment/source path isolation."""

    script = r"""
import importlib.metadata
import json
import os
import pathlib
import site
import sys
import sysconfig
import tongs

dist = importlib.metadata.distribution("tongs")
dist_path = pathlib.Path(dist._path).resolve(strict=True)
direct_path = dist_path / "direct_url.json"
direct_url = json.loads(direct_path.read_text(encoding="utf-8")) if direct_path.is_file() else None
sites = []
for value in [*site.getsitepackages(), sysconfig.get_path("purelib")]:
    resolved = str(pathlib.Path(value).resolve(strict=True))
    if resolved not in sites:
        sites.append(resolved)
print(json.dumps({
    "requested_executable": sys.argv[1],
    "proc_executable": str(pathlib.Path("/proc/self/exe").resolve(strict=True)),
    "system_python_target": str(pathlib.Path(sys._base_executable).resolve(strict=True)),
    "base_executable": str(pathlib.Path(sys._base_executable).resolve(strict=True)),
    "sys_prefix": str(pathlib.Path(sys.prefix).resolve(strict=True)),
    "site_packages": sites,
    "package_root": str(pathlib.Path(tongs.__file__).resolve(strict=True).parent),
    "distribution_path": str(dist_path),
    "tongs_version": dist.version,
    "direct_url": direct_url,
    "editable": bool(isinstance(direct_url, dict) and direct_url.get("dir_info", {}).get("editable") is True),
}, sort_keys=True, separators=(",", ":")))
"""
    try:
        completed = subprocess.run(
            [
                binding.requested_executable,
                "-E",
                "-P",
                "-c",
                script,
                binding.requested_executable,
            ],
            check=False,
            capture_output=True,
            cwd=policy.safe_cwd,
            env=native_environment(),
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise NativeAcceptanceError("installed Python identity probe failed") from error
    if (
        completed.returncode != 0
        or len(completed.stdout) > 64 * 1024
        or completed.stderr
    ):
        raise NativeAcceptanceError(
            "installed Python identity probe did not complete cleanly"
        )
    try:
        value = json.loads(completed.stdout.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise NativeAcceptanceError(
            "installed Python identity probe returned invalid JSON"
        ) from error
    if not isinstance(value, dict):
        raise NativeAcceptanceError(
            "installed Python identity probe returned a non-object"
        )
    verify_core_observation(binding, value, policy)
    return value


def launch_native_run(
    *,
    payload_root: Path,
    evidence_root: Path,
    install: DesktopInstallManifest,
    policy: NativePayloadPolicy,
    run: RunPolicy,
    initial_payload: PayloadSnapshot,
    initial_core: CoreSnapshot,
) -> NativeRunObservation:
    """Run one smoke check and always remove its fresh bounded browser profile."""

    validate_policy(policy)
    if len(Path(run.profile_path).parts) != 1:
        raise NativeAcceptanceError("native profile path must be one fresh directory")
    evidence = evidence_root.resolve(strict=True)
    profile = evidence / run.profile_path
    if profile.exists() or profile.is_symlink():
        raise NativeAcceptanceError("native output/profile path must be fresh")
    profile.mkdir(mode=0o700)
    created_status = profile.stat(follow_symlinks=False)
    profile_fd: int | None = None
    try:
        profile_fd = os.open(
            profile,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        profile_status = os.fstat(profile_fd)
    except OSError as error:
        if profile_fd is not None:
            os.close(profile_fd)
        _remove_owned_profile(
            evidence,
            run.profile_path,
            profile_fd=None,
            expected_device=created_status.st_dev,
            expected_inode=created_status.st_ino,
        )
        raise NativeAcceptanceError("native profile descriptor setup failed") from error
    assert profile_fd is not None
    try:
        return _launch_native_run(
            payload_root=payload_root,
            evidence_root=evidence,
            install=install,
            policy=policy,
            run=run,
            initial_payload=initial_payload,
            initial_core=initial_core,
        )
    finally:
        try:
            _remove_owned_profile(
                evidence,
                run.profile_path,
                profile_fd=profile_fd,
                expected_device=profile_status.st_dev,
                expected_inode=profile_status.st_ino,
            )
        finally:
            os.close(profile_fd)


def _launch_native_run(
    *,
    payload_root: Path,
    evidence_root: Path,
    install: DesktopInstallManifest,
    policy: NativePayloadPolicy,
    run: RunPolicy,
    initial_payload: PayloadSnapshot,
    initial_core: CoreSnapshot,
) -> NativeRunObservation:
    """Launch one production smoke run and retain bounded owned-process evidence."""

    verify_transient_guard()
    validate_policy(policy)
    if (
        capture_payload_snapshot(
            payload_root,
            install,
            expected_uid=policy.payload_uid,
            root_mode=policy.payload_root_mode,
            install_manifest_sha256=policy.install_manifest_sha256,
        )
        != initial_payload
    ):
        raise NativeAcceptanceError("installed payload changed before native run")
    if capture_core_snapshot(policy.core) != initial_core:
        raise NativeAcceptanceError("installed core changed before native run")
    evidence = evidence_root.resolve(strict=True)
    launcher = (payload_root / FIXED_LAUNCHER_PATH).resolve(strict=True)
    report = evidence / run.report_path
    profile = evidence / run.profile_path
    output_paths = [report, *(evidence / path for path in run.screenshot_paths)]
    for path in output_paths:
        if path.exists() or path.is_symlink():
            raise NativeAcceptanceError("native output/profile path must be fresh")
    command = [
        str(launcher),
        "--ozone-platform=x11",
        f"--user-data-dir={profile}",
        "--tongs-python-executable",
        policy.core.requested_executable,
        "--tongs-core-version",
        policy.core.tongs_version,
        "--tongs-safe-cwd",
        policy.safe_cwd,
        "--tongs-smoke-report",
        str(report),
        "--tongs-smoke-source-commit",
        policy.source_commit,
    ]
    if run.review_number is not None:
        command.extend(("--tongs-smoke-review-number", str(run.review_number)))
    observations: dict[int, ProcessObservation] = {}
    with (
        tempfile.TemporaryFile(dir=evidence) as stdout,
        tempfile.TemporaryFile(dir=evidence) as stderr,
    ):
        try:
            process = subprocess.Popen(
                command,
                cwd=policy.safe_cwd,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
                env=native_environment(),
            )
        except OSError as error:
            raise NativeAcceptanceError(
                "installed Electron launcher failed to start: "
                f"{type(error).__name__}(errno={error.errno},"
                f"message={str(error)[:512]!a})"
            ) from error
        deadline = time.monotonic() + policy.deadline_seconds
        next_profile_check = time.monotonic()
        try:
            while process.pid not in observations:
                _collect_owned_tree(process.pid, observations)
                now, next_profile_check = _verify_running_resources(
                    stdout,
                    stderr,
                    profile,
                    next_profile_check=next_profile_check,
                )
                if process.pid in observations:
                    _verify_run_deadline(now, deadline)
                    break
                exit_code = _peek_unreaped_returncode(process)
                if exit_code is not None:
                    raise NativeAcceptanceError(
                        "browser process exited before observation with "
                        f"status {exit_code}"
                    )
                _verify_run_deadline(now, deadline)
                time.sleep(POLL_SECONDS)
            while process.poll() is None:
                _collect_owned_tree(process.pid, observations)
                now, next_profile_check = _verify_running_resources(
                    stdout,
                    stderr,
                    profile,
                    next_profile_check=next_profile_check,
                )
                _verify_run_deadline(now, deadline)
                time.sleep(POLL_SECONDS)
            _collect_owned_tree(process.pid, observations)
            return_code = process.wait(timeout=2)
            if (
                _stream_size(stdout) > MAX_LOG_BYTES
                or _stream_size(stderr) > MAX_LOG_BYTES
            ):
                raise NativeAcceptanceError("native process output exceeded its bound")
            _verify_profile_bounds(profile)
            if not _wait_owned_group_exit(process.pid, observations, 2):
                raise NativeAcceptanceError(
                    "native child process group remained after browser exit"
                )
            if return_code != 0:
                raise NativeAcceptanceError("browser process exited unsuccessfully")
        except _ProcessGroupAuthorityLost as error:
            group_state = _observe_untrusted_group_state(process.pid)
            diagnostic = _process_failure_diagnostic(process, stdout, stderr)
            raise NativeAcceptanceError(
                f"{error}; group-state={group_state}; {diagnostic}"
            ) from error
        except NativeAcceptanceError as error:
            _stop_after_collection_failure(process, observations)
            diagnostic = _process_failure_diagnostic(process, stdout, stderr)
            raise NativeAcceptanceError(f"{error}; {diagnostic}") from error
        except BaseException:
            _stop_after_collection_failure(process, observations)
            raise
    outputs = capture_expected_outputs(evidence, run, policy.evidence_uid)
    payload_after = capture_payload_snapshot(
        payload_root,
        install,
        expected_uid=policy.payload_uid,
        root_mode=policy.payload_root_mode,
        install_manifest_sha256=policy.install_manifest_sha256,
    )
    core_after = capture_core_snapshot(policy.core)
    return NativeRunObservation(
        exit_code=return_code if return_code >= 0 else None,
        signal=-return_code if return_code < 0 else None,
        outputs=outputs,
        processes=tuple(sorted(observations.values(), key=lambda item: item.pid)),
        payload_after=payload_after,
        core_after=core_after,
    )


def _collect_owned_tree(
    root_pid: int, observations: dict[int, ProcessObservation]
) -> None:
    pending = [root_pid]
    seen: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        if len(seen) > MAX_PROCESSES:
            raise NativeAcceptanceError("owned process tree exceeds its bound")
        observation = _observe_process(pid, root_pid)
        if observation is None:
            continue
        previous = observations.get(pid)
        if previous is not None:
            observation = _validate_process_refresh(previous, observation, observations)
        observations[pid] = observation
        # Observations accumulate across the whole run, so bound the retained
        # set with the same limit the pure verifier applies to the evidence it
        # consumes and fail closed rather than growing without limit.
        if len(observations) > MAX_PROCESSES:
            raise NativeAcceptanceError(
                "retained owned process evidence exceeds its bound"
            )
        pending.extend(_child_pids(pid))


def _validate_process_refresh(
    previous: ProcessObservation,
    current: ProcessObservation,
    observations: Mapping[int, ProcessObservation],
) -> ProcessObservation:
    """Allow only proven exec or exact Chromium argv-storage transitions."""

    stable_changed = (
        previous.pid != current.pid
        or previous.start_time_ticks != current.start_time_ticks
        or previous.process_group != current.process_group
        or previous.ppid != current.ppid
    )
    if stable_changed:
        raise NativeAcceptanceError(
            _refresh_rejection_message(previous, current, "kernel identity")
        )
    if previous.executable == current.executable:
        if previous.argv == current.argv and previous.raw_argv == current.raw_argv:
            return current
        if (
            _is_compaction_role_transition(previous.role, current.role)
            and _is_exact_argv_storage_compaction(
                previous.role,
                previous.executable,
                previous.argv,
                current.argv,
            )
            and current.raw_argv == current.argv
            and previous.raw_argv in (previous.argv, current.argv)
        ):
            # A compacted child title carries no ``--type=`` token of its own,
            # so the current observation was necessarily reclassified as a
            # generic helper. The role must come from the already validated
            # canonical observation, not from the compact string, so that a
            # compacted ``--type=gpu-process`` process still counts as the
            # single out-of-process GPU process. Carrying the canonical argv
            # forward also keeps the browser's installed-launcher and full main
            # argv equality checks comparing canonical values. See
            # ``.worktrees/desktop-125-chromium-process-title-audit.md``.
            return replace(current, role=previous.role, argv=previous.argv)
    parent = observations.get(previous.ppid)
    inherited_parent_image = (
        parent is not None
        and previous.executable == parent.executable
        and previous.argv == parent.argv
    )
    if inherited_parent_image and previous.raw_argv == previous.argv:
        return current
    raise NativeAcceptanceError(
        _refresh_rejection_message(
            previous, current, _failed_compaction_precondition(previous, current)
        )
    )


def _is_compaction_role_transition(previous_role: str, current_role: str) -> bool:
    """Admit only the two observed role transitions of a title rewrite."""

    if previous_role == "browser":
        # The browser keeps its role across the rewrite because it is the root
        # PID of the owned tree, which is how the collector classifies it.
        return current_role == "browser"
    # A compacted child title exposes no ``--type=`` token, so the collector
    # necessarily reclassifies it as a generic helper.
    return previous_role in COMPACTED_PROCESS_ROLE_TYPES and current_role == "helper"


def _is_exact_argv_storage_compaction(
    role: str, executable: str, previous: tuple[str, ...], current: tuple[str, ...]
) -> bool:
    """Match Chromium's exact one-field process-title rewrite of a known role.

    Per the pinned audit in
    ``.worktrees/desktop-125-chromium-process-title-audit.md``,
    ``SetProcessTitleFromCommandLine`` resolves ``/proc/self/exe`` and writes the
    resolved executable followed by every canonical argument after argv[0],
    separated by one ASCII space. The audit places that call in
    ``content_main.cc`` before the process type is dispatched, so it applies to
    the browser process as well, which is the failed native attempt 7 case.

    A zygote-forked child keeps canonical argv[0] ``/proc/self/exe`` while its
    compact title starts with the resolved executable, which is the failed
    native attempt 6 case. The browser is exec-started from the installed
    launcher instead, so its canonical argv[0] is always the resolved executable
    and it carries no ``--type=`` token at all.

    Only exact full-title equality is admitted for either shape: no whitespace
    reconstruction, no prefix, and no re-parse of the compact field.
    """

    if not previous or len(current) != 1:
        return False
    type_arguments = tuple(
        argument for argument in previous if argument.startswith("--type=")
    )
    if role == "browser":
        if previous[0] != executable or type_arguments:
            return False
    else:
        process_type = COMPACTED_PROCESS_ROLE_TYPES.get(role)
        if process_type is None:
            return False
        if previous[0] not in (executable, CHROMIUM_ZYGOTE_ARGV0):
            return False
        if type_arguments != (f"--type={process_type}",):
            return False
    # For the browser, Chromium builds the title from its parsed command line,
    # which inserts switches at the switch and argument divider
    # (``command_line.cc`` lines 462 to 464) and pushes positional arguments to
    # the back (``command_line.cc`` line 557), while the title tail itself is
    # read from ``CommandLine::argv()`` (``set_process_title.cc`` lines 96 to
    # 103). Both accepted titles are deterministic renderings of the canonical
    # argv with an identical token multiset, so admitting the permutation adds
    # no content. A compact field still cannot yield a canonical argv[0], so a
    # first-observed compact title remains unacceptable.
    return current[0] in accepted_compact_titles(role, executable, previous)


def _refresh_rejection_message(
    previous: ProcessObservation,
    current: ProcessObservation,
    precondition: str | None,
) -> str:
    """Explain a rejected refresh without changing what is accepted."""

    message = (
        "owned process PID identity changed: "
        f"previous={_process_debug_identity(previous)}; "
        f"current={_process_debug_identity(current)}"
    )
    if precondition is None:
        return message
    return f"{message}; {_compaction_debug_detail(previous, current, precondition)}"


def _failed_compaction_precondition(
    previous: ProcessObservation, current: ProcessObservation
) -> str | None:
    """Name the first unmet precondition of the exact compaction rule.

    Returns ``None`` when the current observation is not even a candidate
    compaction, so unrelated rejections keep their previous message.
    """

    if len(current.argv) != 1:
        return None
    if previous.executable != current.executable:
        return "executable equality"
    if not _is_compaction_role_transition(previous.role, current.role):
        return "role transition"
    if not previous.argv:
        return "previous argv is empty"
    type_arguments = tuple(
        argument for argument in previous.argv if argument.startswith("--type=")
    )
    if previous.role == "browser":
        if previous.argv[0] != previous.executable:
            return "previous argv[0] is not the executable"
        if type_arguments:
            return "browser carries --type arguments"
    else:
        process_type = COMPACTED_PROCESS_ROLE_TYPES.get(previous.role)
        if process_type is None:
            return "role is not compactable"
        if previous.argv[0] not in (previous.executable, CHROMIUM_ZYGOTE_ARGV0):
            return "previous argv[0] is neither the executable nor /proc/self/exe"
        if type_arguments != (f"--type={process_type}",):
            return "--type token count"
    if current.raw_argv != current.argv:
        return "current raw_argv consistency"
    if previous.raw_argv not in (previous.argv, current.argv):
        return "previous raw_argv consistency"
    return "argv join equality"


def _compaction_debug_detail(
    previous: ProcessObservation,
    current: ProcessObservation,
    precondition: str,
) -> str:
    """Bounded byte-level evidence for a rejected candidate compaction."""

    compact = current.argv[0] if current.argv else ""
    compact_bytes = compact.encode("utf-8", errors="replace")
    titles = accepted_compact_titles(
        previous.role, previous.executable, previous.argv
    ) or (chromium_argv_title(previous.executable, previous.argv),)
    labels = (("expected", ""), ("expected_command_line", "command_line_"))
    details = [
        f"precondition={precondition!r}",
        f"current_fields={len(current.argv)}",
        f"compact_len={len(compact_bytes)}",
        f"compact={_bounded_debug_value(compact_bytes)}",
    ]
    for (label, prefix), title in zip(labels, titles, strict=False):
        details.extend(_expected_title_detail(label, prefix, compact_bytes, title))
    details.append(f"previous_raw_argv={_bounded_debug_raw(previous.raw_argv)}")
    details.append(f"current_raw_argv={_bounded_debug_raw(current.raw_argv)}")
    return ",".join(details)


def _expected_title_detail(
    label: str, prefix: str, compact: bytes, title: str
) -> list[str]:
    """Compare the compact field against one accepted title, bounded."""

    expected = title.encode("utf-8", errors="replace")
    details = [
        f"{prefix}expected_len={len(expected)}",
        f"{label}={_bounded_debug_value(expected)}",
    ]
    index = _first_difference(compact, expected)
    if index is None:
        details.append(f"{prefix}first_difference=none")
        return details
    start = max(0, index - 40)
    compact_window = _bounded_debug_value(compact[start : index + 40])
    expected_window = _bounded_debug_value(expected[start : index + 40])
    details.extend(
        (
            f"{prefix}first_difference={index}",
            f"{prefix}compact_window={compact_window}",
            f"{prefix}expected_window={expected_window}",
        )
    )
    return details


def _first_difference(left: bytes, right: bytes) -> int | None:
    for index, (one, other) in enumerate(zip(left, right, strict=False)):
        if one != other:
            return index
    if len(left) != len(right):
        return min(len(left), len(right))
    return None


def _bounded_debug_value(value: bytes) -> str:
    """Render one value, bounded to the per-argument byte limit."""

    if len(value) <= MAX_ARGUMENT_BYTES:
        return repr(value.decode("utf-8", errors="replace"))
    kept = value[:MAX_ARGUMENT_BYTES].decode("utf-8", errors="replace")
    return f"{kept!r}...({len(value) - MAX_ARGUMENT_BYTES} more bytes)"


def _bounded_debug_raw(values: tuple[str, ...] | None) -> str:
    if values is None:
        return "None"
    bounded = tuple(
        _bounded_debug_value(value.encode("utf-8", errors="replace"))
        for value in values[:16]
    )
    tail = "" if len(values) <= 16 else f",...({len(values) - 16} more)"
    return f"({','.join(bounded)}{tail})"


def _process_debug_identity(observation: ProcessObservation) -> str:
    argv = _bounded_debug_argv(observation.argv)
    raw = observation.raw_argv
    raw_detail = (
        ""
        if raw is None or raw == observation.argv
        else f",raw_argv={_bounded_debug_argv(raw)!r}"
    )
    return (
        f"pid={observation.pid},start={observation.start_time_ticks},"
        f"pgid={observation.process_group},ppid={observation.ppid},"
        f"role={observation.role!r},exe={observation.executable!r},argv={argv!r}"
        f"{raw_detail}"
    )


def _bounded_debug_argv(values: tuple[str, ...]) -> tuple[str, ...]:
    bounded = tuple(
        value if len(value) <= 256 else f"{value[:253]}..." for value in values[:16]
    )
    if len(values) > len(bounded):
        bounded = (*bounded, f"...({len(values) - len(bounded)} more)")
    return bounded


def _observe_process(pid: int, root_pid: int) -> ProcessObservation | None:
    process_root = Path("/proc") / str(pid)
    try:
        raw_argv = (process_root / "cmdline").read_bytes()
        executable = str((process_root / "exe").resolve(strict=True))
        status = _parse_status((process_root / "status").read_text(encoding="ascii"))
        process_group, start_time = _parse_process_stat(
            (process_root / "stat").read_text(encoding="ascii")
        )
    except (FileNotFoundError, ProcessLookupError):
        return None
    except (OSError, UnicodeError) as error:
        raise NativeAcceptanceError("owned process metadata cannot be read") from error
    argv_values = raw_argv.rstrip(b"\0").split(b"\0") if raw_argv else []
    if not argv_values or len(argv_values) > MAX_ARGUMENTS:
        return None
    try:
        argv = tuple(value.decode("utf-8", errors="strict") for value in argv_values)
    except UnicodeDecodeError as error:
        raise NativeAcceptanceError("owned process arguments are not UTF-8") from error
    if any(len(value.encode("utf-8")) > MAX_ARGUMENT_BYTES for value in argv):
        raise NativeAcceptanceError("owned process argument exceeds its bound")
    try:
        cwd = str((process_root / "cwd").resolve(strict=True))
    except (FileNotFoundError, PermissionError):
        cwd = None
    role = _process_role(pid, root_pid, argv)
    return ProcessObservation(
        pid=pid,
        ppid=_status_int(status, "PPid"),
        process_group=process_group,
        start_time_ticks=start_time,
        role=role,
        executable=executable,
        argv=argv,
        cwd=cwd,
        sandbox=SandboxStatus(
            no_new_privs=_status_int(status, "NoNewPrivs"),
            seccomp=_status_int(status, "Seccomp"),
            seccomp_filters=_status_int(status, "Seccomp_filters"),
        ),
        raw_argv=argv,
    )


def _child_pids(pid: int) -> tuple[int, ...]:
    task_root = Path("/proc") / str(pid) / "task"
    try:
        with os.scandir(task_root) as iterator:
            task_ids = tuple(entry.name for entry in iterator if entry.name.isdecimal())
    except (FileNotFoundError, ProcessLookupError):
        return ()
    except OSError as error:
        raise NativeAcceptanceError("owned process tasks cannot be read") from error
    if len(task_ids) > 1024:
        raise NativeAcceptanceError("owned process thread count exceeds its bound")
    children: set[int] = set()
    for task_id in task_ids:
        path = task_root / task_id / "children"
        try:
            value = path.read_text(encoding="ascii").strip()
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, UnicodeError) as error:
            raise NativeAcceptanceError(
                "owned child process list cannot be read"
            ) from error
        if not value:
            continue
        fields = value.split()
        if len(fields) > MAX_PROCESSES or any(
            not field.isdecimal() for field in fields
        ):
            raise NativeAcceptanceError("owned child process list is malformed")
        children.update(int(field) for field in fields)
        if len(children) > MAX_PROCESSES:
            raise NativeAcceptanceError("owned child process count exceeds its bound")
    return tuple(sorted(children))


def _parse_status(document: str) -> dict[str, str]:
    if len(document.encode("ascii")) > 64 * 1024:
        raise NativeAcceptanceError("owned process status exceeds its bound")
    values: dict[str, str] = {}
    for line in document.splitlines():
        name, separator, value = line.partition(":")
        if separator and name in {"PPid", "NoNewPrivs", "Seccomp", "Seccomp_filters"}:
            if name in values:
                raise NativeAcceptanceError("owned process status repeats a field")
            values[name] = value.strip()
    return values


def _parse_process_stat(document: str) -> tuple[int, int]:
    if len(document.encode("ascii")) > 16 * 1024:
        raise NativeAcceptanceError("owned process stat exceeds its bound")
    end_name = document.rfind(")")
    fields = document[end_name + 2 :].split() if end_name >= 0 else []
    if len(fields) < 20 or not fields[2].isdecimal() or not fields[19].isdecimal():
        raise NativeAcceptanceError("owned process stat is malformed")
    return int(fields[2]), int(fields[19])


def _status_int(status: Mapping[str, str], name: str) -> int:
    value = status.get(name)
    if value is None or not value.isdecimal():
        return -1
    return int(value)


def _process_role(pid: int, root_pid: int, argv: tuple[str, ...]) -> str:
    if pid == root_pid:
        return "browser"
    if argv[:5] and "tongs.desktop.sidecar" in argv:
        return "python-sidecar"
    switches = {item.partition("=")[0]: item.partition("=")[2] for item in argv}
    process_type = switches.get("--type")
    if process_type == "gpu-process":
        return "gpu"
    if process_type == "renderer":
        return "renderer"
    return process_type or "helper"


def _stream_size(stream: Any) -> int:
    return os.fstat(stream.fileno()).st_size


def _verify_running_resources(
    stdout: Any,
    stderr: Any,
    profile: Path,
    *,
    next_profile_check: float,
) -> tuple[float, float]:
    """Enforce output and profile bounds while starting or running."""

    if _stream_size(stdout) > MAX_LOG_BYTES or _stream_size(stderr) > MAX_LOG_BYTES:
        raise NativeAcceptanceError("native process output exceeded its bound")
    now = time.monotonic()
    if now >= next_profile_check:
        _verify_profile_bounds(profile)
        next_profile_check = now + 0.2
    return now, next_profile_check


def _verify_run_deadline(now: float, deadline: float) -> None:
    if now >= deadline:
        raise NativeAcceptanceError("native production smoke exceeded its deadline")


def _peek_unreaped_returncode(process: subprocess.Popen[bytes]) -> int | None:
    """Observe direct-child exit without reaping the session leader before cleanup."""

    try:
        result = os.waitid(
            os.P_PID,
            process.pid,
            os.WEXITED | os.WNOHANG | os.WNOWAIT,
        )
    except ChildProcessError as error:
        raise _ProcessGroupAuthorityLost(
            "unobserved browser identity was lost before cleanup"
        ) from error
    if result is None:
        return None
    if result.si_pid != process.pid:
        raise _ProcessGroupAuthorityLost("unexpected browser wait identity")
    if result.si_code == os.CLD_EXITED:
        return result.si_status
    if result.si_code in (os.CLD_KILLED, os.CLD_DUMPED):
        return -result.si_status
    raise _ProcessGroupAuthorityLost("unexpected browser wait status")


def _process_failure_diagnostic(
    process: subprocess.Popen[bytes], stdout: Any, stderr: Any
) -> str:
    """Return fail-safe bounded process diagnostics after cleanup and reap."""

    try:
        return_code = process.poll()
        return (
            f"returncode={return_code!r},"
            f"stdout=({_stream_failure_tail(stdout)}),"
            f"stderr=({_stream_failure_tail(stderr)})"
        )
    except Exception as error:  # noqa: BLE001 - diagnostics must not mask launch errors
        return f"diagnostic-unavailable={type(error).__name__}"


def _stop_after_collection_failure(
    process: subprocess.Popen[bytes], observations: Mapping[int, ProcessObservation]
) -> None:
    """Clean up according to whether the fresh session root was identified."""

    if process.pid in observations:
        _stop_owned_group(process, observations)
    else:
        _stop_unobserved_new_group(process)


def _stream_failure_tail(stream: Any) -> str:
    stream.flush()
    size = _stream_size(stream)
    start = max(0, size - MAX_FAILURE_DIAGNOSTIC_BYTES)
    stream.seek(start)
    document = stream.read(MAX_FAILURE_DIAGNOSTIC_BYTES)
    text = document.decode("utf-8", errors="backslashreplace")
    return f"size={size},truncated={start > 0},tail={text!a}"


def _verify_profile_bounds(profile: Path) -> None:
    try:
        status = profile.stat(follow_symlinks=False)
    except FileNotFoundError:
        return
    if (
        not stat.S_ISDIR(status.st_mode)
        or profile.is_symlink()
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) != 0o700
    ):
        raise NativeAcceptanceError("native profile is not a real directory")
    total = 0
    entries_seen = 0
    pending = [profile]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as iterator:
                entries = tuple(iterator)
        except OSError as error:
            raise NativeAcceptanceError(
                "native profile cannot be enumerated"
            ) from error
        for entry in entries:
            entries_seen += 1
            if entries_seen > MAX_PROFILE_ENTRIES:
                raise NativeAcceptanceError("native profile exceeds entry bound")
            item_status = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(item_status.st_mode):
                pending.append(Path(entry.path))
            elif stat.S_ISREG(item_status.st_mode):
                total += item_status.st_size
                if total > MAX_PROFILE_BYTES:
                    raise NativeAcceptanceError("native profile exceeds size bound")
            else:
                raise NativeAcceptanceError(
                    "native profile contains a link or special file"
                )


def _remove_owned_profile(
    evidence_root: Path,
    profile_name: str,
    *,
    profile_fd: int | None,
    expected_device: int,
    expected_inode: int,
) -> None:
    profile = evidence_root / profile_name
    try:
        status = profile.stat(follow_symlinks=False)
    except FileNotFoundError:
        return
    expected = (expected_device, expected_inode)
    if profile_fd is not None:
        held = os.fstat(profile_fd)
        if (held.st_dev, held.st_ino) != expected:
            raise NativeAcceptanceError("native profile descriptor identity changed")
    if (
        status.st_dev,
        status.st_ino,
    ) != expected:
        raise NativeAcceptanceError(
            "native profile identity changed; replacement preserved"
        )
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        profile.unlink(missing_ok=True)
    else:
        if not shutil.rmtree.avoids_symlink_attacks:
            raise NativeAcceptanceError("platform lacks symlink-safe profile cleanup")
        shutil.rmtree(profile)
    if profile.exists() or profile.is_symlink():
        raise NativeAcceptanceError("native profile cleanup was incomplete")


def _stop_owned_group(
    process: subprocess.Popen[bytes], observations: Mapping[int, ProcessObservation]
) -> None:
    state = _owned_group_state(process.pid, observations)
    if state == "unknown":
        raise NativeAcceptanceError(
            "process group exists without a matching owned identity"
        )
    if state == "owned":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    if not _wait_owned_group_exit(process.pid, observations, 1):
        state = _owned_group_state(process.pid, observations)
        if state == "unknown":
            raise NativeAcceptanceError(
                "process group ownership became unknown during cleanup"
            )
        if state == "absent":
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired as error:
            raise NativeAcceptanceError(
                "owned process group could not be stopped"
            ) from error
    if not _wait_owned_group_exit(process.pid, observations, 1):
        raise NativeAcceptanceError("owned process group could not be stopped")


def _stop_unobserved_new_group(process: subprocess.Popen[bytes]) -> None:
    """Stop a fresh session while its unreaped leader prevents PGID reuse."""

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    else:
        time.sleep(1)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired as error:
        raise NativeAcceptanceError(
            "unobserved browser process could not be reaped"
        ) from error
    if not _wait_raw_group_exit(process.pid, 1):
        raise NativeAcceptanceError(
            "unobserved native process group could not be stopped"
        )


def _wait_raw_group_exit(process_group: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _raw_group_exists(process_group):
            return True
        time.sleep(POLL_SECONDS)
    return not _raw_group_exists(process_group)


def _raw_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def _observe_untrusted_group_state(process_group: int) -> str:
    """Observe an unpinned numeric PGID without signaling it."""

    indeterminate = False
    scanned = 0
    try:
        with os.scandir("/proc") as entries:
            for entry in entries:
                if not entry.name.isdecimal():
                    continue
                scanned += 1
                if scanned > MAX_SYSTEM_PROCESS_SCAN:
                    return "unknown"
                try:
                    if os.getpgid(int(entry.name)) == process_group:
                        return "unknown-existing"
                except ProcessLookupError:
                    continue
                except PermissionError:
                    indeterminate = True
    except OSError:
        return "unknown"
    return "unknown" if indeterminate else "absent"


def _wait_owned_group_exit(
    process_group: int,
    observations: Mapping[int, ProcessObservation],
    seconds: float,
) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _owned_group_state(process_group, observations) == "absent":
            return True
        time.sleep(POLL_SECONDS)
    return _owned_group_state(process_group, observations) == "absent"


def _owned_group_state(
    process_group: int, observations: Mapping[int, ProcessObservation]
) -> str:
    if not _raw_group_exists(process_group):
        return "absent"
    if any(
        item.process_group == process_group and _same_process(item)
        for item in observations.values()
    ):
        return "owned"
    return "unknown"


def _same_process(expected: ProcessObservation) -> bool:
    try:
        actual_group, actual_start = _parse_process_stat(
            (Path("/proc") / str(expected.pid) / "stat").read_text(encoding="ascii")
        )
    except (FileNotFoundError, ProcessLookupError):
        return False
    except (OSError, UnicodeError) as error:
        raise NativeAcceptanceError(
            "owned process identity cannot be inspected"
        ) from error
    return (
        actual_group == expected.process_group
        and actual_start == expected.start_time_ticks
    )


if __name__ == "__main__":
    sys.stderr.write("Import this bounded helper from an exact policy driver.\n")
    raise SystemExit(2)
