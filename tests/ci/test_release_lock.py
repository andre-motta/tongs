"""Hold the jobs that carry a signing or write token to hash-locked installs.

A job holding ``id-token: write`` or ``contents: write`` runs everything it
installs while that token is available.  A package resolved fresh from the
index at release time, or an unpinned build backend pulled in by build
isolation, would therefore run with the token.  These cases read every
workflow and require each such job to install only from a hash-locked
requirements file, and then the source tree itself without resolving
anything.  They also hold ``requirements/release.lock`` to its contract: every
entry pinned exactly and hashed, covering the core runtime dependencies, the
signing scripts' imports and the build backend.  Finally, ``GH_TOKEN`` may
reach only the steps of those jobs that actually call ``gh``.
"""

from __future__ import annotations

import re
import shlex
import tomllib
from typing import Any

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from tests.ci.verify_desktop_production_gate import ROOT

WORKFLOW_DIRECTORY = ROOT / ".github/workflows"
RELEASE_WORKFLOW = WORKFLOW_DIRECTORY / "release-desktop.yml"
RELEASE_LOCK = "requirements/release.lock"
RELEASE_INPUTS = "requirements/release.in"
PUBLICATION_PROGRAM = "tests/integration/desktop/release_publication.py"

#: The release jobs that must install from the lock.  Listing them keeps the
#: generic scan below from passing vacuously if a job is renamed.
LOCKED_JOBS = (
    "release-desktop.yml:candidate-attestation",
    "release-desktop.yml:release-publish",
)

#: pip flags a privileged install may carry.  Anything else, including a bare
#: requirement, fails the scan.
ALLOWED_INSTALL_FLAGS = frozenset(
    {
        "--isolated",
        "--disable-pip-version-check",
        "--require-hashes",
        "--only-binary=:all:",
        "--no-deps",
        "--no-build-isolation",
    }
)

_LOCK_ENTRY = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\;]+)\s*\\?$")
_HASH = re.compile(r"^\s+--hash=sha256:[0-9a-f]{64}\s*\\?$")
_GH_COMMAND = re.compile(r"(^|[\s;&|(])gh\s")
_GH_PROGRAM_COMMANDS = ("require-absent", "verify-published")


def _load_workflows() -> dict[str, dict[str, Any]]:
    workflows = {}
    for path in sorted(WORKFLOW_DIRECTORY.glob("*.yml")):
        workflows[path.name] = yaml.safe_load(path.read_text())
    return workflows


@pytest.fixture(scope="module")
def workflows() -> dict[str, dict[str, Any]]:
    return _load_workflows()


def _write_scopes(permissions: Any) -> set[str]:
    if permissions == "write-all":
        return {"write-all"}
    if isinstance(permissions, dict):
        return {scope for scope, level in permissions.items() if level == "write"}
    return set()


def _privileged_jobs(
    workflows: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Return every job holding a write or OIDC token, as ``<file>:<job>``."""

    found = {}
    for name, document in workflows.items():
        default = document.get("permissions")
        for job_id, job in document["jobs"].items():
            scopes = _write_scopes(job.get("permissions", default))
            if scopes:
                found[f"{name}:{job_id}"] = job
    return found


def _install_commands(job: dict[str, Any]) -> list[tuple[int, list[str]]]:
    commands = []
    for index, step in enumerate(job.get("steps", [])):
        for line in (step.get("run") or "").splitlines():
            if "pip install" in line:
                commands.append((index, shlex.split(line)))
    return commands


def _classify_install(tokens: list[str]) -> str:
    """Return ``locked`` or ``source``, or fail for any other install shape."""

    arguments = tokens[tokens.index("install") + 1 :]
    flags = set()
    targets = []
    iterator = iter(arguments)
    for token in iterator:
        if token in ("-r", "-e"):
            targets.append((token, next(iterator)))
        elif token in ALLOWED_INSTALL_FLAGS:
            flags.add(token)
        else:
            raise AssertionError(f"unexpected install argument {token!r} in {tokens}")
    if targets == [("-r", RELEASE_LOCK)]:
        assert {"--require-hashes", "--only-binary=:all:"} <= flags, tokens
        assert "--no-deps" not in flags, tokens
        return "locked"
    if targets == [("-e", ".")]:
        assert {"--no-deps", "--no-build-isolation"} <= flags, tokens
        return "source"
    raise AssertionError(f"install target is neither the lock nor the source: {tokens}")


def test_every_privileged_install_is_hash_locked_or_resolves_nothing(
    workflows: dict[str, dict[str, Any]],
) -> None:
    privileged = _privileged_jobs(workflows)
    assert set(LOCKED_JOBS) <= set(privileged)
    for label, job in privileged.items():
        commands = _install_commands(job)
        if label not in LOCKED_JOBS:
            # docs.yml deploy and publish.yml publish install nothing.  A new
            # install there must adopt the lock and join LOCKED_JOBS.
            assert not commands, f"{label} installs under a write token"
            continue
        kinds = [(index, _classify_install(tokens)) for index, tokens in commands]
        assert [kind for _, kind in kinds] == ["locked", "source"], label
        # The source install must follow the locked one, or it would find its
        # build backend missing and nothing else would supply it.
        assert kinds[0][0] < kinds[1][0], label


def test_the_classifier_refuses_a_fresh_resolution() -> None:
    for command in (
        'python -m pip install -e . "sigstore==4.5.0"',
        "python -m pip install -r requirements/release.lock",
        "python -m pip install --no-deps -e .",
        "python -m pip install --require-hashes --only-binary=:all: -r other.lock",
        "python -m pip install build",
    ):
        with pytest.raises(AssertionError):
            _classify_install(shlex.split(command))


def test_privileged_checkouts_never_persist_credentials(
    workflows: dict[str, dict[str, Any]],
) -> None:
    for label, job in _privileged_jobs(workflows).items():
        for step in job.get("steps", []):
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False, label


def _calls_gh(step: dict[str, Any]) -> bool:
    run = " ".join((step.get("run") or "").split())
    if _GH_COMMAND.search(run):
        return True
    return any(
        f"{PUBLICATION_PROGRAM} {command}" in run for command in _GH_PROGRAM_COMMANDS
    )


def test_gh_token_reaches_only_the_steps_that_call_gh(
    workflows: dict[str, dict[str, Any]],
) -> None:
    for name, document in workflows.items():
        assert "GH_TOKEN" not in (document.get("env") or {}), name
    calling = 0
    for label, job in _privileged_jobs(workflows).items():
        assert "GH_TOKEN" not in (job.get("env") or {}), label
        for step in job.get("steps", []):
            has_token = "GH_TOKEN" in (step.get("env") or {})
            assert has_token == _calls_gh(step), (label, step.get("name"))
            if has_token:
                assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
                calling += 1
    # require-absent, create, draft check, edit and the final check.
    assert calling == 5


def _lock_entries() -> dict[str, tuple[Version, int]]:
    """Parse the lock into ``{canonical name: (version, hash count)}``."""

    entries: dict[str, tuple[Version, int]] = {}
    current = None
    for line in (ROOT / RELEASE_LOCK).read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0].isspace():
            assert current is not None, line
            assert _HASH.match(line), line
            version, hashes = entries[current]
            entries[current] = (version, hashes + 1)
            continue
        match = _LOCK_ENTRY.match(line)
        assert match, f"lock entry is not an exact pin: {line!r}"
        current = canonicalize_name(match.group(1))
        assert current not in entries, current
        entries[current] = (Version(match.group(2)), 0)
    return entries


def test_every_lock_entry_is_pinned_and_hashed() -> None:
    entries = _lock_entries()
    assert entries
    for name, (_, hashes) in entries.items():
        assert hashes >= 1, f"{name} carries no hash"


def test_the_lock_documents_its_regeneration_command() -> None:
    header = (ROOT / RELEASE_LOCK).read_text().splitlines()[:2]
    assert header[0] == "# This file was autogenerated by uv via the following command:"
    command = header[1].removeprefix("#").strip()
    assert command.startswith(f"uv pip compile {RELEASE_INPUTS} ")
    for option in (
        "--generate-hashes",
        "--python-version 3.12",
        "--only-binary :all:",
        "--exclude-newer ",
        f"-o {RELEASE_LOCK}",
    ):
        assert option in command, option


def test_the_lock_matches_the_release_python_and_sigstore(
    workflows: dict[str, dict[str, Any]],
) -> None:
    environment = workflows[RELEASE_WORKFLOW.name]["env"]
    assert environment["PYTHON_VERSION"] == "3.12"
    assert "--python-version 3.12" in (ROOT / RELEASE_LOCK).read_text()
    version, _ = _lock_entries()["sigstore"]
    assert version == Version(environment["SIGSTORE_VERSION"])


def test_the_lock_covers_the_core_the_build_backend_and_the_signing_imports() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    entries = _lock_entries()
    required = [Requirement(item) for item in pyproject["project"]["dependencies"]]
    required += [Requirement(item) for item in pyproject["build-system"]["requires"]]
    # The SPDX generator in the signing job imports jsonschema, bounded as the
    # dev extra bounds it, and hatchling builds the editable wheel with
    # editables.
    required += [
        Requirement(item)
        for item in pyproject["project"]["optional-dependencies"]["dev"]
        if Requirement(item).name == "jsonschema"
    ]
    required.append(Requirement("editables"))
    names = {canonicalize_name(item.name) for item in required}
    assert {"sigstore", "jsonschema", "hatchling", "hatch-vcs"} <= names
    for requirement in required:
        name = canonicalize_name(requirement.name)
        assert name in entries, f"{name} is missing from {RELEASE_LOCK}"
        version, _ = entries[name]
        assert requirement.specifier.contains(version, prereleases=True), (
            f"{name} {version} does not satisfy {requirement}"
        )


def test_every_release_input_is_in_the_lock() -> None:
    entries = _lock_entries()
    inputs = [
        Requirement(line)
        for line in (ROOT / RELEASE_INPUTS).read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert inputs
    for requirement in inputs:
        name = canonicalize_name(requirement.name)
        assert name in entries, name
        assert requirement.specifier.contains(entries[name][0], prereleases=True)
