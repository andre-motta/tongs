"""Hold every CI file to one credential, timeout and install contract.

Each workflow under ``.github/workflows`` is loaded and checked for rules
that apply to every file, not only to the release workflows:

* every file declares ``permissions`` explicitly, at the top level or on
  every job, so no job silently inherits the repository default token scope;

* every ``actions/checkout`` step sets ``persist-credentials: false``, so no
  later step (a build backend, an npm install script) can read the job token
  from ``.git/config``;
* every job that runs steps sets ``timeout-minutes``, so a hung job cannot
  hold a runner, or a token, for the six-hour default;
* every ``pip install`` in a job holding ``id-token: write`` or
  ``contents: write`` either checks hashes or installs with ``--no-deps``
  after a hash-checked install, so nothing resolves fresh from the index
  while that token is available; a source install (``-e`` or a local path)
  after the lock also passes ``--no-build-isolation``, so pip does not fetch
  a fresh build backend into an isolated environment.  A ``--no-deps``
  install without hashes may name only local wheels and local source trees,
  never a package name, a URL or a requirements file;
* the jobs that build what a privileged job publishes, listed in
  ``PUBLISHED_ARTIFACT_JOBS``, follow the same install rule although they
  hold no token, and build without isolation.

Install commands are tokenized as a shell would, with comments removed, so a
comment that mentions ``pip install`` is neither parsed nor counted.

``test_release_lock.py`` pins the exact shape of the locked release installs;
these cases are the file-agnostic floor underneath it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.ci.verify_desktop_production_gate import ROOT
from tests.ci.workflow_installs import install_commands as _install_commands
from tests.ci.workflow_installs import shell_tokens

WORKFLOW_DIRECTORY = ROOT / ".github/workflows"

#: Checkout steps allowed to keep the token in ``.git/config``, keyed by
#: ``<file>:<job>:<step index>`` with the reason as the value.  No step needs
#: it today; an entry must say why the job has to push with the checkout
#: token rather than with a step-scoped credential.
PERSISTED_CREDENTIAL_ALLOWLIST: dict[str, str] = {}

#: Write scopes that make a job privileged for the install rule.
PRIVILEGED_SCOPES = frozenset({"id-token", "contents"})

#: Files the scan must find, so it cannot pass vacuously on a rename.
REQUIRED_FILES = frozenset({"publish.yml", "docs.yml", "release-desktop.yml", "ci.yml"})

#: Jobs that hold no token but build what a privileged job later publishes,
#: keyed by ``<file>:<job>``.  The publish job uploads these artifacts as
#: they are, so their build installs are held to the same hash-lock rule, and
#: their ``python -m build`` runs without isolation.
PUBLISHED_ARTIFACT_JOBS = frozenset({"publish.yml:build"})

_BUILD_FRONTEND = re.compile(r"(^|\s)-m\s+build(\s|$)")

#: pip options whose next token is a value, never a requirement.
_OPTIONS_WITH_VALUES = frozenset(
    {
        "-r",
        "--requirement",
        "-c",
        "--constraint",
        "-i",
        "--index-url",
        "--extra-index-url",
        "-f",
        "--find-links",
        "-t",
        "--target",
        "--prefix",
        "--root",
    }
)


def _workflow_paths() -> list[Path]:
    return sorted(
        path
        for pattern in ("*.yml", "*.yaml")
        for path in WORKFLOW_DIRECTORY.glob(pattern)
    )


@pytest.fixture(scope="module")
def workflows() -> dict[str, dict[str, Any]]:
    return {path.name: yaml.safe_load(path.read_text()) for path in _workflow_paths()}


def _jobs(
    workflows: dict[str, dict[str, Any]],
) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (name, job_id, job)
        for name, document in workflows.items()
        for job_id, job in document["jobs"].items()
    ]


def test_the_scan_sees_every_ci_file(workflows: dict[str, dict[str, Any]]) -> None:
    assert REQUIRED_FILES <= set(workflows)
    for name, document in workflows.items():
        assert isinstance(document, dict) and document.get("jobs"), name


def test_every_checkout_drops_its_credentials(
    workflows: dict[str, dict[str, Any]],
) -> None:
    checkouts = 0
    for name, job_id, job in _jobs(workflows):
        for index, step in enumerate(job.get("steps", [])):
            if not str(step.get("uses", "")).startswith("actions/checkout@"):
                continue
            checkouts += 1
            label = f"{name}:{job_id}:{index}"
            if label in PERSISTED_CREDENTIAL_ALLOWLIST:
                continue
            persist = (step.get("with") or {}).get("persist-credentials")
            assert persist is False, f"{label} keeps the token in .git/config"
    assert checkouts


def test_the_credential_allowlist_is_justified_and_current(
    workflows: dict[str, dict[str, Any]],
) -> None:
    for label, reason in PERSISTED_CREDENTIAL_ALLOWLIST.items():
        assert reason.strip(), f"{label} carries no justification"
        name, job_id, index = label.rsplit(":", 2)
        step = workflows[name]["jobs"][job_id]["steps"][int(index)]
        assert str(step.get("uses", "")).startswith("actions/checkout@"), label


def test_every_file_declares_its_permissions(
    workflows: dict[str, dict[str, Any]],
) -> None:
    for name, document in workflows.items():
        if "permissions" in document:
            continue
        undeclared = [
            job_id
            for job_id, job in document["jobs"].items()
            if "permissions" not in job
        ]
        assert not undeclared, f"{name} leaves permissions implicit for {undeclared}"


def test_every_job_has_a_timeout(workflows: dict[str, dict[str, Any]]) -> None:
    for name, job_id, job in _jobs(workflows):
        label = f"{name}:{job_id}"
        if "uses" in job:
            # GitHub refuses timeout-minutes on a job that calls a reusable
            # workflow.  The called file's own jobs carry the timeouts, and
            # this scan covers them because the called file is local.
            called = str(job["uses"])
            assert called.startswith("./.github/workflows/"), label
            assert Path(called).name in workflows, label
            assert "timeout-minutes" not in job, label
            continue
        timeout = job.get("timeout-minutes")
        assert isinstance(timeout, int) and not isinstance(timeout, bool), label
        assert 0 < timeout <= 180, f"{label} timeout {timeout}"


def _write_scopes(permissions: Any) -> set[str]:
    if permissions == "write-all":
        return set(PRIVILEGED_SCOPES)
    if isinstance(permissions, dict):
        return {scope for scope, level in permissions.items() if level == "write"}
    return set()


def _is_privileged(document: dict[str, Any], job: dict[str, Any]) -> bool:
    permissions = job.get("permissions", document.get("permissions"))
    return bool(_write_scopes(permissions) & PRIVILEGED_SCOPES)


def _is_local_path(argument: str) -> bool:
    if "://" in argument:
        return False
    return argument == "." or argument.startswith(("./", "../", "/", ".["))


def _is_local_wheel(argument: str) -> bool:
    return argument.endswith(".whl") and "://" not in argument


def _no_deps_install_is_local(tokens: list[str]) -> bool:
    """Report whether a ``--no-deps`` install names only local artifacts.

    A follow-up install without hashes is safe only when every target is a
    local wheel or a local source tree, the latter built with
    ``--no-build-isolation``.  A bare package name, a URL or a requirements
    file resolves from the index, so any of them fails.
    """

    arguments = tokens[tokens.index("install") + 1 :]
    targets = 0
    builds_source = False
    iterator = iter(arguments)
    for argument in iterator:
        if argument in {"-r", "--requirement"} or argument.startswith(
            ("-r", "--requirement=")
        ):
            return False
        if argument in {"-e", "--editable"} or argument.startswith("--editable="):
            value = argument.split("=", 1)[1] if "=" in argument else next(iterator, "")
            if not _is_local_path(value):
                return False
            targets += 1
            builds_source = True
            continue
        if argument in _OPTIONS_WITH_VALUES:
            next(iterator, None)
            continue
        if argument.startswith("-"):
            continue
        if _is_local_wheel(argument):
            targets += 1
        elif _is_local_path(argument):
            targets += 1
            builds_source = True
        else:
            return False
    if builds_source and "--no-build-isolation" not in arguments:
        return False
    return targets > 0


def _install_violations(commands: list[list[str]]) -> list[str]:
    """Name each install that could resolve a package fresh from the index."""

    violations = []
    hash_checked = False
    for tokens in commands:
        if "--require-hashes" in tokens:
            hash_checked = True
            continue
        if "--no-deps" in tokens and hash_checked and _no_deps_install_is_local(tokens):
            continue
        violations.append(" ".join(tokens))
    return violations


def _held_to_the_install_rule(
    name: str, document: dict[str, Any], job_id: str, job: dict[str, Any]
) -> bool:
    return (
        _is_privileged(document, job) or f"{name}:{job_id}" in PUBLISHED_ARTIFACT_JOBS
    )


def test_every_privileged_install_is_hash_checked(
    workflows: dict[str, dict[str, Any]],
) -> None:
    privileged_installs = 0
    for name, document in workflows.items():
        for job_id, job in document["jobs"].items():
            if not _held_to_the_install_rule(name, document, job_id, job):
                continue
            commands = _install_commands(job)
            privileged_installs += len(commands)
            assert not _install_violations(commands), f"{name}:{job_id}"
    # The release signing and publishing jobs each install the lock and then
    # the source, and the PyPI build job installs the lock; a scan that finds
    # fewer has stopped seeing them.
    assert privileged_installs >= 5


def test_published_artifacts_build_from_the_lock_without_isolation(
    workflows: dict[str, dict[str, Any]],
) -> None:
    for label in PUBLISHED_ARTIFACT_JOBS:
        name, job_id = label.split(":", 1)
        job = workflows[name]["jobs"][job_id]
        commands = _install_commands(job)
        assert commands, f"{label} no longer installs its build tools"
        assert "--require-hashes" in commands[0], label
        builds = [
            shell_tokens(line)
            for step in job.get("steps", [])
            for line in (step.get("run") or "").replace("\\\n", " ").splitlines()
            if _BUILD_FRONTEND.search(line)
        ]
        assert builds, f"{label} no longer runs python -m build"
        for tokens in builds:
            assert "--no-isolation" in tokens, f"{label}: {' '.join(tokens)}"


@pytest.mark.parametrize(
    "script",
    [
        "pip install build",
        "python -m pip install -e . sigstore==4.5.0",
        "python -m pip install --no-deps -e .",
        "python3 -m pip install ruff",
        "python -m pip install \\\n  -e .",
        "python -m pip  install  build",
        "pip3 install build",
    ],
)
def test_the_install_rule_refuses_a_fresh_resolution(script: str) -> None:
    job = {"steps": [{"run": script}]}
    commands = _install_commands(job)
    assert len(commands) == 1
    assert _install_violations(commands)


def _after_the_lock(install: str) -> list[list[str]]:
    job = {
        "steps": [
            {"run": "python -m pip install --require-hashes -r requirements/x.lock"},
            {"run": install},
        ]
    }
    commands = _install_commands(job)
    assert len(commands) == 2
    return commands


@pytest.mark.parametrize(
    "source_install",
    [
        "python -m pip install --no-deps -e .",
        "python -m pip install --no-deps --editable=.",
        'python -m pip install --no-deps ".[dev]"',
        "python -m pip install --no-deps ./examples/desktop-plugin",
    ],
)
def test_the_install_rule_refuses_an_isolated_source_build(source_install: str) -> None:
    commands = _after_the_lock(source_install)
    assert _install_violations(commands) == [" ".join(commands[1])]


@pytest.mark.parametrize(
    "install",
    [
        "python -m pip install --no-deps sigstore",
        "python -m pip install --no-deps --no-build-isolation sigstore==4.5.0",
        "python -m pip install --no-deps -r ./more.lock",
        "python -m pip install --no-deps -r ./more.lock ./dist/x.whl",
        "python -m pip install --no-deps --requirement=more.lock",
        "python -m pip install --no-deps -rmore.lock",
        "python -m pip install --no-deps https://example.com/x-1.0-py3-none-any.whl",
        "python -m pip install --no-deps --no-build-isolation git+https://x/y.git",
        "python -m pip install --no-deps --no-build-isolation -e git+https://x/y.git",
        "python -m pip install --no-deps --no-build-isolation ./src sigstore",
        "python -m pip install --no-deps",
    ],
)
def test_the_install_rule_refuses_a_no_deps_install_of_anything_remote(
    install: str,
) -> None:
    commands = _after_the_lock(install)
    assert _install_violations(commands) == [" ".join(commands[1])]


@pytest.mark.parametrize(
    "install",
    [
        "python -m pip install --no-deps ./dist/x.whl",
        "python -m pip install --no-deps dist/tongs-1.0.0-py3-none-any.whl",
        'python -m pip install --no-deps "$RUNNER_TEMP"/wheel/x.whl',
        "python -m pip install --no-deps --no-build-isolation -e .",
        "python -m pip install --no-deps --no-build-isolation ./examples/plugin",
        "python -m pip install --no-deps --require-hashes -r ./more.lock",
    ],
)
def test_the_install_rule_accepts_a_no_deps_install_of_local_artifacts(
    install: str,
) -> None:
    assert _install_violations(_after_the_lock(install)) == []


def test_the_install_scan_ignores_comments_and_splits_commands() -> None:
    job = {
        "steps": [
            {
                "run": (
                    "# Don't pip install anything unpinned here\n"
                    "echo ready  # a later pip install would need hashes\n"
                    "cd src && python -m pip install --require-hashes -r x.lock"
                    " && pip3 install ruff\n"
                )
            }
        ]
    }
    commands = _install_commands(job)
    assert commands == [
        ["python", "-m", "pip", "install", "--require-hashes", "-r", "x.lock"],
        ["pip3", "install", "ruff"],
    ]
    assert _install_violations(commands) == ["pip3 install ruff"]


def test_the_install_rule_accepts_a_locked_then_no_deps_install() -> None:
    job = {
        "steps": [
            {"run": "python -m pip install --require-hashes -r requirements/x.lock"},
            {"run": "python -m pip install \\\n  --no-deps --no-build-isolation -e ."},
        ]
    }
    commands = _install_commands(job)
    assert len(commands) == 2
    assert _install_violations(commands) == []


def test_privilege_follows_the_workflow_default() -> None:
    document = {"permissions": {"contents": "write"}}
    assert _is_privileged(document, {})
    assert not _is_privileged(document, {"permissions": {"contents": "read"}})
    assert _is_privileged({}, {"permissions": {"id-token": "write"}})
    assert _is_privileged({"permissions": "write-all"}, {})
    assert not _is_privileged({}, {"permissions": {"pages": "write"}})
