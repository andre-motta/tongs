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
  a fresh build backend into an isolated environment.

Install commands are tokenized as a shell would, with comments removed, so a
comment that mentions ``pip install`` is neither parsed nor counted.

``test_release_lock.py`` pins the exact shape of the locked release installs;
these cases are the file-agnostic floor underneath it.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.ci.verify_desktop_production_gate import ROOT

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

_PIP_INSTALL = re.compile(r"(^|[\s;&|(/\"'])pip3?\s+install(\s|$)")

#: Shell control tokens that end one command and start the next.
_COMMAND_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")"})

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


def _shell_tokens(line: str) -> list[str]:
    """Split one shell line into words and control tokens, without comments."""

    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    return list(lexer)


def _pip_installs(tokens: list[str]) -> list[list[str]]:
    """Return each ``pip install`` command in a token list, one per command."""

    commands = []
    start = 0
    for index, token in enumerate(tokens):
        if token in _COMMAND_SEPARATORS:
            start = index + 1
            continue
        is_pip = token in {"pip", "pip3"} or token.endswith(("/pip", "/pip3"))
        if is_pip and tokens[index + 1 : index + 2] == ["install"]:
            end = next(
                (
                    position
                    for position in range(index + 2, len(tokens))
                    if tokens[position] in _COMMAND_SEPARATORS
                ),
                len(tokens),
            )
            commands.append(tokens[start:end])
    return commands


def _install_commands(job: dict[str, Any]) -> list[list[str]]:
    """Return every pip install in the job's ``run`` steps, in step order."""

    commands = []
    for step in job.get("steps", []):
        script = (step.get("run") or "").replace("\\\n", " ")
        for line in script.splitlines():
            if _PIP_INSTALL.search(line):
                commands.extend(_pip_installs(_shell_tokens(line)))
    return commands


def _installs_source(tokens: list[str]) -> bool:
    """Report whether an install builds a local source tree or project."""

    arguments = tokens[tokens.index("install") + 1 :]
    skip_value = False
    for argument in arguments:
        if skip_value:
            skip_value = False
            continue
        if argument in {"-e", "--editable"} or argument.startswith("--editable="):
            return True
        if argument in _OPTIONS_WITH_VALUES:
            skip_value = True
            continue
        if argument.startswith("-"):
            continue
        is_path = argument == "." or argument.startswith(("./", "../", "/", ".["))
        if is_path and not argument.endswith(".whl"):
            return True
    return False


def _install_violations(commands: list[list[str]]) -> list[str]:
    """Name each install that could resolve a package fresh from the index."""

    violations = []
    hash_checked = False
    for tokens in commands:
        if "--require-hashes" in tokens:
            hash_checked = True
            continue
        if "--no-deps" in tokens and hash_checked:
            if _installs_source(tokens) and "--no-build-isolation" not in tokens:
                violations.append(" ".join(tokens))
            continue
        violations.append(" ".join(tokens))
    return violations


def test_every_privileged_install_is_hash_checked(
    workflows: dict[str, dict[str, Any]],
) -> None:
    privileged_installs = 0
    for name, document in workflows.items():
        for job_id, job in document["jobs"].items():
            if not _is_privileged(document, job):
                continue
            commands = _install_commands(job)
            privileged_installs += len(commands)
            assert not _install_violations(commands), f"{name}:{job_id}"
    # The release signing and publishing jobs each install the lock and then
    # the source; a scan that finds none has stopped seeing them.
    assert privileged_installs >= 4


@pytest.mark.parametrize(
    "script",
    [
        "pip install build",
        "python -m pip install -e . sigstore==4.5.0",
        "python -m pip install --no-deps -e .",
        "python3 -m pip install ruff",
        "python -m pip install \\\n  -e .",
    ],
)
def test_the_install_rule_refuses_a_fresh_resolution(script: str) -> None:
    job = {"steps": [{"run": script}]}
    commands = _install_commands(job)
    assert len(commands) == 1
    assert _install_violations(commands)


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
    job = {
        "steps": [
            {"run": "python -m pip install --require-hashes -r requirements/x.lock"},
            {"run": source_install},
        ]
    }
    commands = _install_commands(job)
    assert len(commands) == 2
    assert _install_violations(commands) == [" ".join(commands[1])]


def test_the_install_rule_accepts_a_locked_wheel_without_build_flags() -> None:
    job = {
        "steps": [
            {"run": "python -m pip install --require-hashes -r ./requirements/x.lock"},
            {"run": "python -m pip install --no-deps -r ./more.lock ./dist/x.whl"},
        ]
    }
    assert _install_violations(_install_commands(job)) == []


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
