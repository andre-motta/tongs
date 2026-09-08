"""Produce fail-closed evidence for ordinary installed-core TUI startup."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pty
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import time
import venv
import zipfile
from collections.abc import Sequence
from pathlib import Path

_HEX_40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_ANSI = re.compile(
    rb"(?:\x1B\][^\x07]*(?:\x07|\x1B\\)|\x1B(?:\[[0-?]*[ -/]*[@-~]|[@-_]))"
)
_MAX_CAPTURE_BYTES = 8 * 1024 * 1024
_SETUP_TIMEOUT_SECONDS = 180
_STARTUP_TIMEOUT_SECONDS = 15
_EXIT_TIMEOUT_SECONDS = 8
_REQUIRED_SCREEN_TEXT = ("tongs", "My Reviews", "My MRs", "All Open")
_WHEEL_MEMBERS = (
    "tongs/__init__.py",
    "tongs/__main__.py",
    "tongs/app.py",
    "tongs/desktop/protocol/server.py",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    path.chmod(0o600)


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        raise RuntimeError(f"required audit record is missing: {path}")
    if path.stat().st_size > 1024 * 1024:
        raise RuntimeError("audit record exceeds the 1 MiB evidence limit")
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        if not isinstance(value, dict):
            raise TypeError("audit record must be a JSON object")
        records.append(value)
    return records


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def validate_audit_records(
    records: Sequence[dict[str, object]],
    *,
    expected_python: Path,
    installed_package_root: Path,
    source_root: Path,
    audit_root: Path,
) -> dict[str, object]:
    starts = [record for record in records if record.get("event") == "audit_started"]
    finishes = [record for record in records if record.get("event") == "audit_finished"]
    blocked = [record for record in records if record.get("event") == "blocked"]
    if len(starts) != 1 or len(finishes) != 1:
        raise RuntimeError("audit must contain exactly one start and one finish record")
    if blocked or finishes[0].get("blocked") is not False:
        raise RuntimeError("measured startup attempted forbidden activity")
    if starts[0].get("mcp_available") is not False:
        raise RuntimeError("optional MCP dependency is available in minimal core")
    if Path(str(starts[0].get("executable"))).resolve() != expected_python.resolve():
        raise RuntimeError("audited interpreter is not the installed candidate")
    sys_path = starts[0].get("sys_path")
    if not isinstance(sys_path, list) or not all(
        isinstance(item, str) for item in sys_path
    ):
        raise RuntimeError("audit sys.path is missing or invalid")
    for item in sys_path:
        resolved = Path(item).resolve()
        if _inside(resolved, source_root):
            raise RuntimeError("source checkout leaked onto audited sys.path")
    if not any(_inside(Path(item), audit_root) for item in sys_path):
        raise RuntimeError("audit hook path is absent from audited sys.path")
    if finishes[0].get("loaded_forbidden_modules") != []:
        raise RuntimeError("forbidden modules were loaded during measured startup")
    origins = finishes[0].get("tongs_module_origins")
    if not isinstance(origins, dict) or not origins:
        raise RuntimeError("installed tongs module origins were not retained")
    for name, origin in origins.items():
        if not isinstance(name, str) or not isinstance(origin, str):
            raise TypeError("invalid installed module origin record")
        if not _inside(Path(origin), installed_package_root):
            raise RuntimeError(f"{name} resolved outside the installed package")
    return {
        "blocked_events": 0,
        "loaded_tongs_modules": len(origins),
        "mcp_available": False,
        "records": len(records),
    }


def _validate_input_path(path: Path, *, label: str, directory: bool) -> Path:
    if not path.is_absolute():
        raise RuntimeError(f"{label} must be an absolute path")
    resolved = path.resolve()
    exists = resolved.is_dir() if directory else resolved.is_file()
    if not exists:
        kind = "directory" if directory else "file"
        raise RuntimeError(f"{label} must identify an existing {kind}")
    return resolved


def validate_inputs(args: argparse.Namespace) -> dict[str, Path]:
    wheel = _validate_input_path(args.core_wheel, label="core wheel", directory=False)
    if wheel.suffix != ".whl":
        raise RuntimeError("core wheel must use the .whl suffix")
    source = _validate_input_path(args.source_root, label="source root", directory=True)
    if not _HEX_64.fullmatch(args.expected_wheel_sha256):
        raise RuntimeError("expected wheel SHA-256 must be lowercase hexadecimal")
    if sha256_file(wheel) != args.expected_wheel_sha256:
        raise RuntimeError("core wheel SHA-256 does not match")
    if not _HEX_40.fullmatch(args.expected_source_commit):
        raise RuntimeError("expected source commit must be lowercase hexadecimal")
    if not args.expected_core_version or len(args.expected_core_version) > 200:
        raise RuntimeError("expected core version is invalid")
    environment = args.environment_root
    evidence = args.evidence_root
    for path, label in ((environment, "environment root"), (evidence, "evidence root")):
        if not path.is_absolute():
            raise RuntimeError(f"{label} must be absolute")
        if path.exists():
            raise RuntimeError(f"{label} must not already exist")
        if _inside(path, source):
            raise RuntimeError(f"{label} must be outside the source checkout")
    if environment.resolve() == evidence.resolve():
        raise RuntimeError("environment and evidence roots must be distinct")
    helper = source / "tests/integration/desktop/installed_core_audit_sitecustomize.py"
    fixture = source / "tests/integration/desktop/draft_acceptance_sidecar.py"
    wrapper = source / "tests/integration/desktop/installed_core_sidecar_fixture.py"
    for path, label in (
        (helper, "audit helper"),
        (fixture, "controlled fixture"),
        (wrapper, "sidecar wrapper"),
    ):
        _validate_input_path(path, label=label, directory=False)
    return {
        "audit_helper": helper.resolve(),
        "controlled_fixture": fixture.resolve(),
        "core_wheel": wheel,
        "environment_root": environment.resolve(),
        "evidence_root": evidence.resolve(),
        "sidecar_wrapper": wrapper.resolve(),
        "source_root": source,
    }


def _run_setup(
    command: Sequence[str], *, cwd: Path, timeout: int = _SETUP_TIMEOUT_SECONDS
) -> dict[str, object]:
    started = time.monotonic()
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    result = {
        "argv": list(command),
        "cwd": str(cwd),
        "duration_seconds": round(time.monotonic() - started, 3),
        "returncode": completed.returncode,
        "stderr": completed.stderr[-16_384:],
        "stdout": completed.stdout[-16_384:],
    }
    if completed.returncode != 0:
        raise RuntimeError(f"setup command failed: {result}")
    return result


def _source_identity(source_root: Path, expected_commit: str) -> dict[str, object]:
    head = _run_setup(["git", "rev-parse", "HEAD"], cwd=source_root, timeout=20)[
        "stdout"
    ].strip()
    if head != expected_commit:
        raise RuntimeError("source checkout HEAD does not match expected commit")
    tree = _run_setup(["git", "rev-parse", "HEAD^{tree}"], cwd=source_root, timeout=20)[
        "stdout"
    ].strip()
    status = _run_setup(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=source_root,
        timeout=20,
    )["stdout"]
    if status:
        raise RuntimeError("tracked source checkout is dirty")
    return {"commit": head, "tree": tree, "tracked_status": "clean"}


def _wheel_hashes(wheel: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    with zipfile.ZipFile(wheel) as archive:
        names = frozenset(archive.namelist())
        for member in _WHEEL_MEMBERS:
            if member not in names:
                raise RuntimeError(f"wheel is missing required member {member}")
            result[member] = hashlib.sha256(archive.read(member)).hexdigest()
    return result


def _installed_identity(
    python: Path,
    *,
    expected_version: str,
    expected_wheel_sha256: str,
    source_root: Path,
    wheel_hashes: dict[str, str],
) -> dict[str, object]:
    script = r"""import hashlib, importlib.metadata, importlib.util, json, os, pathlib, site, sys
import tongs
root = pathlib.Path(tongs.__file__).resolve().parent
members = {}
for name in ("__init__.py", "__main__.py", "app.py", "desktop/protocol/server.py"):
    path = root / name
    members["tongs/" + name] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
distribution = importlib.metadata.distribution("tongs")
direct_url_text = distribution.read_text("direct_url.json")
direct_url = json.loads(direct_url_text) if direct_url_text else None
print(json.dumps({
    "base_executable": str(pathlib.Path(sys._base_executable).resolve()),
    "direct_url": direct_url,
    "distribution_path": str(pathlib.Path(distribution._path).resolve()),
    "editable": bool(direct_url and direct_url.get("dir_info", {}).get("editable")),
    "executable": str(pathlib.Path(sys.executable).resolve()),
    "mcp_available": importlib.util.find_spec("mcp") is not None,
    "package_root": str(root),
    "proc_self_exe": str(pathlib.Path("/proc/self/exe").resolve()),
    "site_packages": [str(pathlib.Path(item).resolve()) for item in site.getsitepackages()],
    "sys_prefix": str(pathlib.Path(sys.prefix).resolve()),
    "sys_path": [str(pathlib.Path(item or ".").resolve()) for item in sys.path],
    "system_python_target": str(pathlib.Path(sys._base_executable).resolve()),
    "tongs_version": distribution.version,
    "wheel_members": members,
}, sort_keys=True))"""
    command = [str(python), "-I", "-P", "-c", script]
    completed = subprocess.run(
        command,
        cwd="/tmp",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"installed identity failed: {completed.stderr[-4096:]}")
    identity = json.loads(completed.stdout)
    if identity["tongs_version"] != expected_version:
        raise RuntimeError("installed core version does not match")
    if identity["mcp_available"] is not False:
        raise RuntimeError("optional MCP dependency is installed")
    if identity["editable"] is not False:
        raise RuntimeError("installed core must not be editable")
    direct_url = identity["direct_url"]
    if not isinstance(direct_url, dict) or "dir_info" in direct_url:
        raise RuntimeError("installed core lacks non-editable wheel origin metadata")
    archive_info = direct_url.get("archive_info")
    if not isinstance(archive_info, dict):
        raise TypeError("installed core lacks wheel archive origin metadata")
    hashes = archive_info.get("hashes")
    if not isinstance(hashes, dict) or hashes.get("sha256") != expected_wheel_sha256:
        raise RuntimeError("installed origin metadata does not bind the input wheel")
    if Path(identity["executable"]).resolve() != python.resolve():
        raise RuntimeError("identity interpreter does not match candidate")
    package_root = Path(identity["package_root"]).resolve()
    if _inside(package_root, source_root):
        raise RuntimeError("installed identity resolved into source checkout")
    for entry in identity["sys_path"]:
        if _inside(Path(entry), source_root):
            raise RuntimeError("source checkout leaked into identity sys.path")
    site_packages = [Path(entry).resolve() for entry in identity["site_packages"]]
    if not site_packages or not any(
        _inside(package_root, item) for item in site_packages
    ):
        raise RuntimeError("installed package is outside recorded site-packages")
    if Path(identity["sys_prefix"]).resolve() != python.parent.parent.resolve():
        raise RuntimeError("installed sys.prefix does not match candidate environment")
    for member, expected_hash in wheel_hashes.items():
        record = identity["wheel_members"][member]
        if record["sha256"] != expected_hash:
            raise RuntimeError(f"installed bytes differ from wheel member {member}")
        if not _inside(Path(record["path"]), package_root):
            raise RuntimeError(f"installed member escaped package root: {member}")
    return identity


def _clean_terminal(raw: bytes) -> str:
    without_ansi = _ANSI.sub(b"", raw).replace(b"\r", b"\n")
    return without_ansi.decode("utf-8", errors="replace")


def _run_tui(
    executable: Path,
    *,
    python: Path,
    scan_root: Path,
    home: Path,
    audit_root: Path,
    audit_path: Path,
    source_root: Path,
    evidence_root: Path,
) -> dict[str, object]:
    master, slave = pty.openpty()
    environment = {
        "COLORTERM": "truecolor",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(audit_root),
        "TERM": "xterm-256color",
        "TONGS_INSTALLED_CORE_AUDIT_PATH": str(audit_path),
        "TONGS_INSTALLED_CORE_EXPECTED_PYTHON": str(python),
        "TONGS_INSTALLED_CORE_SOURCE_ROOT": str(source_root),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"),
    }
    command = [str(executable), "--scan-root", str(scan_root)]
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=evidence_root,
        env=environment,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    captured = bytearray()
    stable_at: float | None = None
    sent_quit = False
    deadline = started + _STARTUP_TIMEOUT_SECONDS
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master], [], [], 0.1)
            if readable:
                try:
                    chunk = os.read(master, 65_536)
                except OSError:
                    chunk = b""
                if chunk:
                    captured.extend(chunk)
                    if len(captured) > _MAX_CAPTURE_BYTES:
                        raise RuntimeError("terminal capture exceeded 8 MiB")
            text = _clean_terminal(bytes(captured))
            ready = all(value in text for value in _REQUIRED_SCREEN_TEXT)
            if ready and stable_at is None:
                stable_at = time.monotonic()
            if ready and time.monotonic() - stable_at >= 0.25:
                os.write(master, b"q")
                sent_quit = True
                break
            if process.poll() is not None:
                break
        if not sent_quit:
            raise RuntimeError("ordinary TUI did not reach the stable Inbox screen")
        exit_deadline = time.monotonic() + _EXIT_TIMEOUT_SECONDS
        while process.poll() is None and time.monotonic() < exit_deadline:
            readable, _, _ = select.select([master], [], [], 0.1)
            if readable:
                try:
                    chunk = os.read(master, 65_536)
                except OSError:
                    chunk = b""
                if chunk:
                    captured.extend(chunk)
                    if len(captured) > _MAX_CAPTURE_BYTES:
                        raise RuntimeError("terminal capture exceeded 8 MiB")
        if process.poll() is None:
            raise RuntimeError("ordinary TUI did not exit after its quit binding")
        if process.returncode != 0:
            raise RuntimeError(f"ordinary TUI exited with status {process.returncode}")
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
        os.close(master)
    raw_path = evidence_root / "installed-core-terminal.bin"
    text_path = evidence_root / "installed-core-terminal.txt"
    raw_path.write_bytes(captured)
    raw_path.chmod(0o600)
    text_path.write_text(_clean_terminal(bytes(captured)), encoding="utf-8")
    text_path.chmod(0o600)
    return {
        "argv": command,
        "cwd": str(evidence_root),
        "duration_seconds": round(time.monotonic() - started, 3),
        "environment_keys": sorted(environment),
        "exit_status": process.returncode,
        "pid": process.pid,
        "screen_markers": list(_REQUIRED_SCREEN_TEXT),
        "terminal_bin": {
            "path": str(raw_path),
            "sha256": sha256_file(raw_path),
            "size": raw_path.stat().st_size,
        },
        "terminal_text": {
            "path": str(text_path),
            "sha256": sha256_file(text_path),
            "size": text_path.stat().st_size,
        },
    }


def run(args: argparse.Namespace) -> dict[str, object]:
    paths = validate_inputs(args)
    source_identity = _source_identity(
        paths["source_root"], args.expected_source_commit
    )
    wheel_hashes = _wheel_hashes(paths["core_wheel"])
    for member, wheel_hash in wheel_hashes.items():
        source_member = paths["source_root"] / "src" / member
        if not source_member.is_file() or sha256_file(source_member) != wheel_hash:
            raise RuntimeError(f"wheel member does not match exact source: {member}")
    paths["evidence_root"].mkdir(parents=True, mode=0o700)
    commands: list[dict[str, object]] = []
    venv.EnvBuilder(with_pip=True, clear=False, symlinks=False).create(
        paths["environment_root"]
    )
    python = paths["environment_root"] / "bin/python"
    pip = paths["environment_root"] / "bin/pip"
    commands.append(
        _run_setup(
            [
                str(pip),
                "install",
                "--disable-pip-version-check",
                str(paths["core_wheel"]),
            ],
            cwd=paths["evidence_root"],
        )
    )
    identity = _installed_identity(
        python,
        expected_version=args.expected_core_version,
        expected_wheel_sha256=args.expected_wheel_sha256,
        source_root=paths["source_root"],
        wheel_hashes=wheel_hashes,
    )
    package_root = Path(str(identity["package_root"])).resolve()
    executable = paths["environment_root"] / "bin/tongs"
    if not executable.is_file() or not bool(executable.stat().st_mode & stat.S_IXUSR):
        raise RuntimeError("installed ordinary tongs entry point is missing")

    audit_root = paths["evidence_root"] / "audit-hook"
    audit_root.mkdir(mode=0o700)
    audit_copy = audit_root / "sitecustomize.py"
    shutil.copyfile(paths["audit_helper"], audit_copy)
    audit_copy.chmod(0o600)
    if sha256_file(audit_copy) != sha256_file(paths["audit_helper"]):
        raise RuntimeError("copied audit hook differs from checked-in helper")
    home = paths["evidence_root"] / "home"
    scan_root = paths["evidence_root"] / "empty-scan-root"
    home.mkdir(mode=0o700)
    scan_root.mkdir(mode=0o700)
    audit_path = paths["evidence_root"] / "installed-core-audit.jsonl"
    tui = _run_tui(
        executable,
        python=python,
        scan_root=scan_root,
        home=home,
        audit_root=audit_root,
        audit_path=audit_path,
        source_root=paths["source_root"],
        evidence_root=paths["evidence_root"],
    )
    audit_records = _read_jsonl(audit_path)
    audit_validation = validate_audit_records(
        audit_records,
        expected_python=python,
        installed_package_root=package_root,
        source_root=paths["source_root"],
        audit_root=audit_root,
    )
    report = {
        "audit": {
            "checked_in_helper": {
                "path": str(paths["audit_helper"]),
                "sha256": sha256_file(paths["audit_helper"]),
            },
            "copied_helper": {
                "path": str(audit_copy),
                "sha256": sha256_file(audit_copy),
            },
            "jsonl": {
                "path": str(audit_path),
                "sha256": sha256_file(audit_path),
                "size": audit_path.stat().st_size,
            },
            "validation": audit_validation,
        },
        "commands": commands,
        "controlled_fixture": {
            "path": str(paths["controlled_fixture"]),
            "sha256": sha256_file(paths["controlled_fixture"]),
        },
        "environment_root": str(paths["environment_root"]),
        "installed_identity": identity,
        "phase_boundary": "dependency installation completed before audited offline startup",
        "sidecar_wrapper": {
            "path": str(paths["sidecar_wrapper"]),
            "sha256": sha256_file(paths["sidecar_wrapper"]),
        },
        "source": {**source_identity, "selected_member_sha256": wheel_hashes},
        "status": "pass",
        "tui": tui,
        "wheel": {
            "path": str(paths["core_wheel"]),
            "sha256": args.expected_wheel_sha256,
            "size": paths["core_wheel"].stat().st_size,
            "selected_member_sha256": wheel_hashes,
        },
    }
    report_path = paths["evidence_root"] / "installed-core-terminal.json"
    _write_json(report_path, report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--core-wheel", type=Path, required=True)
    parser.add_argument("--expected-wheel-sha256", required=True)
    parser.add_argument("--expected-core-version", required=True)
    parser.add_argument("--expected-source-commit", required=True)
    parser.add_argument("--environment-root", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    report = run(_parser().parse_args(argv))
    print(json.dumps({"source": report["source"], "status": report["status"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
