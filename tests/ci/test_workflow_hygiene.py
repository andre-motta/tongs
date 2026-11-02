"""Hold every CI file to one credential, permission and timeout contract.

Each workflow under ``.github/workflows`` is loaded and checked for rules
that apply to every file, not only to the release workflows:

* every file declares ``permissions`` explicitly, at the top level or on
  every job, so no job silently inherits the repository default token scope;
* every ``actions/checkout`` step sets ``persist-credentials: false``, so no
  later step (a build backend, an npm install script) can read the job token
  from ``.git/config``;
* every job that runs steps sets ``timeout-minutes``, so a hung job cannot
  hold a runner, or a token, for the six-hour default;
* the manual packaging workflows run only on ``workflow_dispatch`` with
  read-only tokens and check out the exact candidate commit;
* every workflow a release tag push triggers runs the release gate
  (``.github/scripts/release_gate.py``) before any job of it can upload or
  publish, with read scopes only, and the token reaches only the gate's
  read-only ``gh api`` fetch.

* every package a workflow ``pip install``s by name is pinned to one exact
  version, and the tools CI runs come from a pinned extra in
  ``pyproject.toml``, so Dependabot can see and bump them; no workflow
  fetches a tool through ``npx``, ``npm install``, ``pipx``, ``uvx`` or
  ``uv tool``, so ``npm ci`` from a lockfile is the only Node install.

* the RPM builder image workflow is the only one holding ``packages: write``,
  in its publish job alone; it runs only weekly, on dispatch and on pushes to
  ``main`` that change the builder, every job requires ``main``, the publish
  job attests the pushed digest with the release's ``actions/attest`` pin, and
  the pin job holds only ``contents: write`` and ``pull-requests: write``.

* no workflow can reach the RPM harness's local builder mode: none passes
  ``--local-builder`` or sets, overrides or clears ``GITHUB_ACTIONS`` or
  ``RUNNER_ENVIRONMENT``, the variables the harness refuses that mode on.

``test_release_lock.py`` holds every install under a write token to the
hash-locked release lock.
"""

from __future__ import annotations

import fnmatch
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from packaging.requirements import InvalidRequirement, Requirement

from tests.ci.verify_desktop_production_gate import ROOT
from tests.ci.workflow_installs import (
    COMMAND_SEPARATORS,
    install_commands,
    shell_tokens,
)

WORKFLOW_DIRECTORY = ROOT / ".github/workflows"
RELEASE_GATE = ".github/scripts/release_gate.py"
TAG_PUSH = "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')"
#: Actions that upload, attest or publish something.
_PUBLISHING_ACTIONS = (
    "actions/upload-artifact@",
    "actions/attest@",
    "pypa/gh-action-pypi-publish@",
)
_GH_CALL = re.compile(r"(^|[\s;&|(])gh\s+(\S+)(.*)")
#: gh api options that turn a read into a write.
_GH_WRITE_OPTIONS = re.compile(
    r"(^|\s)(-X|--method|-f|-F|--field|--raw-field|--input)(\s|=|$)"
)

#: The dispatch-only packaging workflows and the exact read scopes each holds.
#: The manual Fedora RPM workflow was retired: it downloaded a recorded archive
#: artifact that has expired, and the production gate's ``rpm-lifecycle`` job
#: proves the same lifecycle against a fresh archive.
MANUAL_PACKAGING_WORKFLOWS = {
    "desktop-archive.yml": {"contents": "read"},
    "desktop-python-rpms.yml": {"contents": "read"},
}
#: Workflows that must not come back.
RETIRED_WORKFLOWS = frozenset({"desktop-rpm.yml"})


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


def test_every_checkout_drops_its_credentials(
    workflows: dict[str, dict[str, Any]],
) -> None:
    checkouts = 0
    for name, job_id, job in _jobs(workflows):
        for index, step in enumerate(job.get("steps", [])):
            if not str(step.get("uses", "")).startswith("actions/checkout@"):
                continue
            checkouts += 1
            persist = (step.get("with") or {}).get("persist-credentials")
            assert persist is False, (
                f"{name}:{job_id}:{index} keeps the token in .git/config"
            )
    assert checkouts


def test_no_workflow_runs_on_pull_request_target(
    workflows: dict[str, dict[str, Any]],
) -> None:
    # pull_request_target runs fork code with the base repository's secrets.
    # PyYAML reads the bare `on` key as True.
    for name, document in workflows.items():
        triggers = document.get(True) or document.get("on") or {}
        if isinstance(triggers, str):
            triggers = {triggers: None}
        elif isinstance(triggers, list):
            triggers = dict.fromkeys(triggers)
        assert "pull_request_target" not in triggers, (
            f"{name} runs on pull_request_target"
        )


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


@pytest.mark.parametrize("name", sorted(MANUAL_PACKAGING_WORKFLOWS))
def test_manual_packaging_workflows_are_dispatch_only_and_read_only(
    workflows: dict[str, dict[str, Any]], name: str
) -> None:
    document = workflows[name]
    # PyYAML reads the bare ``on`` key as the boolean True.
    triggers = document.get("on", document.get(True))
    assert set(triggers) == {"workflow_dispatch"}, name
    assert document["permissions"] == MANUAL_PACKAGING_WORKFLOWS[name]
    checkouts = 0
    for job_id, job in document["jobs"].items():
        assert "permissions" not in job, f"{name}:{job_id} widens its token"
        for step in job["steps"]:
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                checkouts += 1
                assert step["with"]["ref"] == "${{ env.TONGS_HEAD_SHA }}", name
    assert checkouts, name


def test_retired_workflows_stay_retired(
    workflows: dict[str, dict[str, Any]],
) -> None:
    assert not RETIRED_WORKFLOWS & set(workflows)
    # A manual packaging workflow listed here must exist, so the dispatch-only
    # check above cannot pass vacuously.
    assert set(MANUAL_PACKAGING_WORKFLOWS) <= set(workflows)


def _triggers(document: dict[str, Any]) -> dict[str, Any]:
    triggers = document.get("on", document.get(True)) or {}
    if isinstance(triggers, str):
        return {triggers: None}
    if isinstance(triggers, list):
        return dict.fromkeys(triggers)
    return triggers


def _runs_gate(step: dict[str, Any]) -> bool:
    return RELEASE_GATE in (step.get("run") or "")


def _publishes(step: dict[str, Any]) -> bool:
    uses = str(step.get("uses", ""))
    run = " ".join((step.get("run") or "").split())
    return uses.startswith(_PUBLISHING_ACTIONS) or "gh release" in run


def _gated_jobs(document: dict[str, Any]) -> set[str]:
    """Return the jobs that cannot run a step unless the gate passed.

    A job is gated when it runs the gate unconditionally for a tag push, or
    when it needs a gated job and its condition cannot bypass that job's
    failure.
    """

    jobs = document["jobs"]
    gated = set()
    for job_id, job in jobs.items():
        for step in job.get("steps", []):
            if _runs_gate(step):
                condition = " ".join(str(step.get("if", TAG_PUSH)).split())
                if condition == TAG_PUSH:
                    gated.add(job_id)
    changed = True
    while changed:
        changed = False
        for job_id, job in jobs.items():
            if job_id in gated:
                continue
            needs = job.get("needs") or []
            needs = [needs] if isinstance(needs, str) else needs
            condition = " ".join(str(job.get("if", "")).split())
            if any(
                word in condition for word in ("always()", "failure()", "cancelled()")
            ):
                continue
            for need in needs:
                if need in gated and (
                    not condition or f"needs.{need}.result == 'success'" in condition
                ):
                    gated.add(job_id)
                    changed = True
                    break
    return gated


def test_every_tag_push_workflow_gates_what_it_publishes(
    workflows: dict[str, dict[str, Any]],
) -> None:
    tag_workflows = 0
    for name, document in workflows.items():
        push = _triggers(document).get("push") or {}
        if not push.get("tags"):
            continue
        tag_workflows += 1
        gated = _gated_jobs(document)
        assert gated, f"{name} runs on a tag push without the release gate"
        for job_id, job in document["jobs"].items():
            steps = job.get("steps", [])
            publishing = [index for index, step in enumerate(steps) if _publishes(step)]
            if not publishing:
                continue
            label = f"{name}:{job_id}"
            assert job_id in gated, f"{label} can publish without the release gate"
            gate = [index for index, step in enumerate(steps) if _runs_gate(step)]
            if gate:
                assert max(gate) < min(publishing), label
    # publish.yml and release-desktop.yml.
    assert tag_workflows == 2


def test_the_release_gate_token_is_read_only_and_reaches_only_its_fetch(
    workflows: dict[str, dict[str, Any]],
) -> None:
    gate_jobs = 0
    for name, document in workflows.items():
        for job_id, job in document["jobs"].items():
            steps = job.get("steps", [])
            gate = [index for index, step in enumerate(steps) if _runs_gate(step)]
            if not gate:
                continue
            gate_jobs += 1
            label = f"{name}:{job_id}"
            assert job["permissions"] == {"contents": "read", "actions": "read"}, label
            assert "GH_TOKEN" not in (document.get("env") or {}), label
            assert "GH_TOKEN" not in (job.get("env") or {}), label
            assert "GH_TOKEN" not in (steps[gate[0]].get("env") or {}), label
            fetches = 0
            for step in steps:
                script = (step.get("run") or "").replace("\\\n", " ")
                calls = [
                    match
                    for line in script.splitlines()
                    if (match := _GH_CALL.search(line.split("#", 1)[0]))
                ]
                # Only steps that call gh hold the token, and every call there
                # is a gh api read.
                has_token = "GH_TOKEN" in (step.get("env") or {})
                assert has_token == bool(calls), (label, step.get("name"))
                fetches += bool(calls)
                for call in calls:
                    assert call.group(2) == "api", (label, call.group(0))
                    assert not _GH_WRITE_OPTIONS.search(call.group(3)), (
                        label,
                        call.group(0),
                    )
            assert fetches == 1, label
    # publish.yml build and release-desktop.yml candidate-archive.
    assert gate_jobs == 2


#: ``pip install`` options that take the next word as their value.
_PIP_VALUE_OPTIONS = frozenset(
    {
        "-r",
        "--requirement",
        "-c",
        "--constraint",
        "-e",
        "--editable",
        "-i",
        "--index-url",
        "--extra-index-url",
        "-f",
        "--find-links",
        "-t",
        "--target",
        "--prefix",
        "--root",
        "--src",
        "--platform",
        "--python-version",
        "--implementation",
        "--abi",
        "--only-binary",
        "--no-binary",
        "--upgrade-strategy",
        "-C",
        "--config-settings",
        "--progress-bar",
        "--report",
        "--python",
        "--cache-dir",
        "--log",
    }
)
#: The extra the lint job installs Ruff from.
LINT_EXTRA = "lint"
#: Tool runners that fetch a package outside any lockfile or declared extra.
_AD_HOC_RUNNERS = re.compile(
    r"^(npx|pipx|uvx)$|^(npm|pnpm|yarn) (install|i|add)$|^uv (tool|pip)$"
)
_AD_HOC_WORD = re.compile(r"\b(npx|pipx|uvx|npm|pnpm|yarn|uv)\b")
_ENVIRONMENT_REFERENCE = re.compile(r"\$\{(\w+)\}|\$(\w+)")


def _is_local_path(word: str) -> bool:
    return word.startswith((".", "/")) and "://" not in word


def _expand(word: str, environment: dict[str, str]) -> str:
    return _ENVIRONMENT_REFERENCE.sub(
        lambda match: environment.get(match.group(1) or match.group(2), match.group()),
        word,
    )


def unpinned_install_targets(
    tokens: list[str], environment: dict[str, str]
) -> list[str]:
    """Return what one ``pip install`` command installs without an exact pin.

    Requirement files, constraints and local paths are allowed: a requirement
    file holds its own pins, and a local path is this repository, whose
    dependencies are declared in its ``pyproject.toml``.  A package named on
    the command line must pin exactly one version with ``==``, after the
    workflow's environment variables are expanded.
    """

    words = tokens[tokens.index("install") + 1 :]
    unpinned: list[str] = []
    index = 0
    while index < len(words):
        word = words[index]
        index += 1
        if word.startswith("-"):
            if word in _PIP_VALUE_OPTIONS:
                value = words[index] if index < len(words) else ""
                index += 1
                if word in {"-e", "--editable"} and not _is_local_path(value):
                    unpinned.append(value)
            continue
        if _is_local_path(word):
            continue
        try:
            requirement = Requirement(_expand(word, environment))
        except InvalidRequirement:
            unpinned.append(word)
            continue
        specifiers = list(requirement.specifier)
        if (
            requirement.url
            or len(specifiers) != 1
            or specifiers[0].operator != "=="
            or "*" in specifiers[0].version
        ):
            unpinned.append(word)
    return unpinned


@pytest.mark.parametrize(
    ("command", "unpinned"),
    [
        ("python -m pip install ruff", ["ruff"]),
        ("pip3 install 'ruff>=0.17'", ["ruff>=0.17"]),
        ("pip install ruff==0.17.* build==1.3.0", ["ruff==0.17.*"]),
        (
            "pip install git+https://example.invalid/ruff.git",
            ["git+https://example.invalid/ruff.git"],
        ),
        (
            "pip install -e git+https://example.invalid/x.git",
            ["git+https://example.invalid/x.git"],
        ),
        ('pip install "sigstore==${SIGSTORE_VERSION}"', []),
        ("pip install sigstore==$UNSET_VERSION", ["sigstore==$UNSET_VERSION"]),
        ('python -m pip install -e ".[dev,lint]" ./examples/desktop-plugin', []),
        ("pip install --require-hashes --only-binary=:all: -r x.lock -c y.txt", []),
        ("pip install --no-deps --target out ruff==0.17.0", []),
    ],
)
def test_the_pin_check_names_every_unpinned_target(
    command: str, unpinned: list[str]
) -> None:
    (tokens,) = install_commands({"steps": [{"run": command}]})
    assert unpinned_install_targets(tokens, {"SIGSTORE_VERSION": "4.5.0"}) == unpinned


def test_every_tool_a_workflow_installs_by_name_is_pinned(
    workflows: dict[str, dict[str, Any]],
) -> None:
    installs = 0
    for name, job_id, job in _jobs(workflows):
        environment = {
            **(workflows[name].get("env") or {}),
            **(job.get("env") or {}),
        }
        for step in job.get("steps", []):
            scope = {**environment, **(step.get("env") or {})}
            scope = {key: str(value) for key, value in scope.items()}
            for tokens in install_commands({"steps": [step]}):
                installs += 1
                unpinned = unpinned_install_targets(tokens, scope)
                assert not unpinned, f"{name}:{job_id} installs {unpinned} unpinned"
    assert installs


def ad_hoc_tool_fetches(script: str) -> list[str]:
    """Return each command in ``script`` that fetches a tool by name."""

    found = []
    for line in script.replace("\\\n", " ").splitlines():
        if not _AD_HOC_WORD.search(line):
            continue
        command: list[str] = []
        for token in [*shell_tokens(line), ";"]:
            if token not in COMMAND_SEPARATORS:
                command.append(token)
                continue
            words = [Path(word).name for word in command[:2]]
            if any(
                _AD_HOC_RUNNERS.match(" ".join(words[:length])) for length in (1, 2)
            ):
                found.append(" ".join(command))
            command = []
    return found


@pytest.mark.parametrize(
    ("script", "found"),
    [
        ("npx markdownlint-cli2 docs", ["npx markdownlint-cli2 docs"]),
        ("cd site && npm install astro", ["npm install astro"]),
        ("/usr/bin/pipx run build", ["/usr/bin/pipx run build"]),
        ("uvx ruff check", ["uvx ruff check"]),
        ("uv tool install ruff", ["uv tool install ruff"]),
        ("npm ci --prefix site\nnpm run build --prefix site", []),
        ("echo 'use npx here'  # npx is not run", []),
    ],
)
def test_the_fetch_check_finds_every_ad_hoc_runner(
    script: str, found: list[str]
) -> None:
    assert ad_hoc_tool_fetches(script) == found


def test_no_workflow_fetches_a_tool_outside_a_lockfile(
    workflows: dict[str, dict[str, Any]],
) -> None:
    for name, job_id, job in _jobs(workflows):
        for step in job.get("steps", []):
            found = ad_hoc_tool_fetches(step.get("run") or "")
            assert not found, (
                f"{name}:{job_id} runs {found}; install the tool from a lockfile "
                "or a pinned extra instead"
            )


def test_the_lint_extra_pins_every_tool_exactly() -> None:
    extras = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"][
        "optional-dependencies"
    ]
    requirements = [Requirement(item) for item in extras[LINT_EXTRA]]
    assert "ruff" in {requirement.name for requirement in requirements}
    for requirement in requirements:
        (specifier,) = requirement.specifier
        assert specifier.operator == "==", str(requirement)
        assert "*" not in specifier.version, str(requirement)


def test_the_lint_job_installs_ruff_from_the_pinned_extra(
    workflows: dict[str, dict[str, Any]],
) -> None:
    job = workflows["ci.yml"]["jobs"]["lint-and-format"]
    extras = []
    for tokens in install_commands(job):
        for word in tokens[tokens.index("install") + 1 :]:
            if word.startswith(".["):
                extras.extend(word[2:].rstrip("]").split(","))
    assert LINT_EXTRA in extras


def test_dependabot_proposes_grouped_monthly_updates() -> None:
    config = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())
    assert config["version"] == 2
    entries = config["updates"]
    assert sorted({entry["package-ecosystem"] for entry in entries}) == [
        "github-actions",
        "npm",
        "pip",
    ]
    for entry in entries:
        ecosystem = entry["package-ecosystem"]
        assert entry["schedule"]["interval"] == "monthly", ecosystem
        # Labels would rerun CI through the labeled event and cancel the
        # first run of every Dependabot pull request.
        assert entry["labels"] == [], ecosystem
        types = sorted(
            tuple(sorted(group.get("update-types", [])))
            for group in entry["groups"].values()
        )
        # One pull request for minor and patch updates, one for majors.
        assert types == [("major",), ("minor", "patch")], ecosystem
    updates = {
        (entry["package-ecosystem"], entry.get("directory")): entry for entry in entries
    }
    assert len(updates) == len(entries)
    assert ("github-actions", "/") in updates
    # The uv-compiled hash locks cannot be regenerated by Dependabot.
    assert updates[("pip", "/")]["exclude-paths"] == ["requirements/**"]
    # Sigstore is bumped by hand together with the release locks.
    assert updates[("pip", "/")]["ignore"] == [{"dependency-name": "sigstore"}]
    npm = updates[("npm", None)]
    assert npm["directories"] == ["/desktop", "/site", "/.github/linters"]
    lint = updates[("npm", "/desktop/tools/lint")]
    for directory in [*npm["directories"], lint["directory"]]:
        assert (ROOT / directory.lstrip("/") / "package-lock.json").is_file()
    # Every direct npm dependency is exact; "increase" keeps it exact.
    assert npm["versioning-strategy"] == "increase"
    assert lint["versioning-strategy"] == "increase"
    # Electron is bumped by hand together with the desktop archive pins.
    assert npm["ignore"] == [{"dependency-name": "electron"}]
    # The lint package's TypeScript stays inside typescript-eslint's peer
    # range, so only its patch updates are proposed.
    assert lint["ignore"] == [
        {
            "dependency-name": "typescript",
            "update-types": [
                "version-update:semver-minor",
                "version-update:semver-major",
            ],
        }
    ]
    assert set(updates) == {
        ("github-actions", "/"),
        ("pip", "/"),
        ("npm", None),
        ("npm", "/desktop/tools/lint"),
    }


#: The workflow that builds, publishes and pins the RPM builder image.
BUILDER_WORKFLOW = "rpm-builder-image.yml"
BUILDER_IMAGE = "ghcr.io/andre-motta/tongs-rpm-builder"
BUILDER_PIN = "packaging/rpm/builder/image.json"
BUILDER_PATHS = [
    "packaging/rpm/builder/**",
    f"!{BUILDER_PIN}",
    ".github/workflows/rpm-builder-image.yml",
]
MAIN_GUARD = "github.ref == 'refs/heads/main'"


def _builder(workflows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return workflows[BUILDER_WORKFLOW]


def _steps_using(job: dict[str, Any], action: str) -> list[dict[str, Any]]:
    return [
        step
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith(f"{action}@")
    ]


def _step_named(job: dict[str, Any], fragment: str) -> dict[str, Any]:
    (step,) = [step for step in job["steps"] if fragment in step.get("name", "")]
    return step


def test_only_the_builder_publish_job_can_write_packages(
    workflows: dict[str, dict[str, Any]],
) -> None:
    writers = set()
    for name, document in workflows.items():
        default = document.get("permissions")
        assert default != "write-all", name
        if isinstance(default, dict) and default.get("packages") == "write":
            writers.add(f"{name}:*")
        for job_id, job in document["jobs"].items():
            permissions = job.get("permissions", default)
            assert permissions != "write-all", f"{name}:{job_id}"
            if isinstance(permissions, dict) and permissions.get("packages") == "write":
                writers.add(f"{name}:{job_id}")
    assert writers == {f"{BUILDER_WORKFLOW}:build"}


def test_the_builder_runs_weekly_on_dispatch_and_on_builder_pushes_to_main(
    workflows: dict[str, dict[str, Any]],
) -> None:
    triggers = _triggers(_builder(workflows))
    assert set(triggers) == {"schedule", "workflow_dispatch", "push"}
    (schedule,) = triggers["schedule"]
    minute, hour, day_of_month, month, day_of_week = schedule["cron"].split()
    # Weekly: one fixed weekday, any day of the month, off the top of the hour.
    assert (day_of_month, month) == ("*", "*")
    assert day_of_week.isdigit() and 0 <= int(day_of_week) <= 6
    assert minute.isdigit() and int(minute) != 0
    assert hour.isdigit()
    assert triggers["workflow_dispatch"] is None
    assert triggers["push"] == {"branches": ["main"], "paths": BUILDER_PATHS}


def _push_paths_match(patterns: list[str], path: str) -> bool:
    """Evaluate GitHub push path filters in order: the last match decides.

    ``fnmatch`` lets a single ``*`` cross ``/``, which GitHub's filter does not,
    so this only models ``**`` globs and literal paths, the forms used here.
    """
    matched = False
    for pattern in patterns:
        negated = pattern.startswith("!")
        glob = pattern[1:] if negated else pattern
        if fnmatch.fnmatchcase(path, glob):
            matched = not negated
    return matched


def test_merging_a_pin_pull_request_cannot_rebuild_the_image(
    workflows: dict[str, dict[str, Any]],
) -> None:
    paths = _triggers(_builder(workflows))["push"]["paths"]
    # The pin job's pull request only changes the pin file; a rebuild on its
    # merge would publish a new digest and open another pin pull request.
    assert not _push_paths_match(paths, BUILDER_PIN)
    # The exclusion must follow the directory glob, or the glob re-includes it.
    assert paths.index(f"!{BUILDER_PIN}") > paths.index("packaging/rpm/builder/**")
    assert _push_paths_match(paths, "packaging/rpm/builder/Containerfile")
    assert _push_paths_match(paths, "packaging/rpm/builder/packages.txt")
    assert _push_paths_match(paths, ".github/workflows/rpm-builder-image.yml")
    assert not _push_paths_match(paths, "packaging/rpm/desktop/tongs-desktop.spec")


def test_the_builder_holds_no_token_by_default_and_every_job_needs_main(
    workflows: dict[str, dict[str, Any]],
) -> None:
    document = _builder(workflows)
    assert document["permissions"] == {}
    assert document["env"]["BUILDER_IMAGE"] == BUILDER_IMAGE
    assert document["concurrency"]["cancel-in-progress"] is False
    assert set(document["jobs"]) == {"build", "pin"}
    for job_id, job in document["jobs"].items():
        condition = " ".join(str(job.get("if", "")).split())
        assert MAIN_GUARD in condition, job_id
        assert "||" not in condition, job_id
        assert "github.repository == 'andre-motta/tongs'" in condition, job_id


def test_the_builder_publish_job_pushes_by_digest_and_attests_it(
    workflows: dict[str, dict[str, Any]],
) -> None:
    job = _builder(workflows)["jobs"]["build"]
    assert job["permissions"] == {
        "contents": "read",
        "packages": "write",
        "id-token": "write",
        "attestations": "write",
    }
    release = workflows["release-desktop.yml"]["jobs"]["candidate-attestation"]
    release_pin = {step["uses"] for step in _steps_using(release, "actions/attest")}
    (attest,) = _steps_using(job, "actions/attest")
    # The same reviewed pin the release signing job uses.
    assert {attest["uses"]} == release_pin
    assert attest["with"] == {
        "subject-name": "${{ env.BUILDER_IMAGE }}",
        "subject-digest": "${{ steps.push.outputs.digest }}",
        "push-to-registry": True,
        "create-storage-record": False,
        "show-summary": True,
    }
    names = [step.get("name", "") for step in job["steps"]]
    push = _step_named(job, "Push the image")
    assert push["id"] == "push"
    assert "podman push --digestfile" in push["run"]
    assert names.index(push["name"]) < names.index(attest["name"])
    build = _step_named(job, "Build the image")
    assert "FEDORA_DIGEST=$BASE_DIGEST" in build["run"]
    assert "SOURCE_COMMIT=$GITHUB_SHA" in build["run"]
    assert 'tag="fedora44-$(date -u +%Y%m%d)-${GITHUB_SHA::12}"' in build["run"]
    base = _step_named(job, "Resolve the current Fedora 44 base digest")
    assert "skopeo inspect" in base["run"]
    check = _step_named(job, "Require the package list")
    assert names.index(check["name"]) < names.index(build["name"])
    assert "build_requires.py --check" in check["run"]
    # The registry token reaches only the login, and only through stdin.
    login = _step_named(job, "Log in to the GitHub Container Registry")
    assert login["env"] == {"REGISTRY_TOKEN": "${{ github.token }}"}
    assert "--password-stdin" in login["run"]
    assert "--password " not in login["run"]
    for step in job["steps"]:
        if step is not login:
            assert "github.token" not in str(step), step.get("name")
    # actions/attest reads only ~/.docker/config.json, so the login writes
    # there and the last step always removes the credential again.
    assert '--authfile "$HOME/.docker/config.json"' in login["run"]
    assert "DOCKER_CONFIG=" not in str(job)
    cleanup = job["steps"][-1]
    assert cleanup["if"] == "${{ always() }}"
    assert (
        'podman logout --authfile "$HOME/.docker/config.json" ghcr.io'
        in (cleanup["run"])
    )


def test_the_builder_pin_job_only_proposes_a_pull_request(
    workflows: dict[str, dict[str, Any]],
) -> None:
    document = _builder(workflows)
    job = document["jobs"]["pin"]
    assert job["needs"] == "build"
    assert job["permissions"] == {"contents": "write", "pull-requests": "write"}
    assert not _steps_using(job, "actions/attest")
    rewrite = _step_named(job, "Rewrite the pin")
    assert "scripts/pin_rpm_builder.py" in rewrite["run"]
    assert "--check" in rewrite["run"]
    propose = _step_named(job, "Open or update the pin pull request")
    assert propose["if"] == "steps.pin.outputs.changed == 'true'"
    assert propose["env"]["GH_TOKEN"] == "${{ github.token }}"
    script = propose["run"]
    assert document["env"]["PIN_BRANCH"] == "automation/rpm-builder-pin"
    assert 'push --force origin "HEAD:refs/heads/$PIN_BRANCH"' in script
    assert "-- packaging/rpm/builder/image.json" in script
    assert "gh pr edit" in script and "gh pr create" in script
    assert "--base main" in script


#: Shell that would set or clear the variables the RPM harness reads to tell a
#: hosted runner from a developer's machine.
_RUNNER_IDENTITY_OVERRIDE = re.compile(
    r"\b(GITHUB_ACTIONS|RUNNER_ENVIRONMENT)=|"
    r"\bunset\b[^\n;&|]*\b(GITHUB_ACTIONS|RUNNER_ENVIRONMENT)\b|"
    r"(^|\s)(-u|--unset)[\s=]*(GITHUB_ACTIONS|RUNNER_ENVIRONMENT)\b"
)


def _env_maps(document: dict[str, Any]) -> list[dict[str, Any]]:
    maps = [document.get("env") or {}]
    for job in document["jobs"].values():
        maps.append(job.get("env") or {})
        maps.extend(step.get("env") or {} for step in job.get("steps", []))
    return maps


def test_no_workflow_can_reach_the_local_rpm_builder_mode(
    workflows: dict[str, dict[str, Any]],
) -> None:
    """``run_hosted.sh --local-builder`` is for local Podman loops only.

    The harness refuses it whenever ``GITHUB_ACTIONS`` or
    ``RUNNER_ENVIRONMENT`` is set, so no workflow may pass the option or set,
    override or clear either variable.
    """

    for path in _workflow_paths():
        assert "--local-builder" not in path.read_text(), path.name
    for name, document in workflows.items():
        for environment in _env_maps(document):
            assert not {"GITHUB_ACTIONS", "RUNNER_ENVIRONMENT"} & set(environment), name
        for job_id, job in document["jobs"].items():
            for step in job.get("steps", []):
                script = step.get("run") or ""
                assert not _RUNNER_IDENTITY_OVERRIDE.search(script), (
                    f"{name}:{job_id}:{step.get('name')}"
                )


@pytest.mark.parametrize(
    "script",
    [
        "GITHUB_ACTIONS= packaging/rpm/desktop/run_hosted.sh",
        "env -u GITHUB_ACTIONS packaging/rpm/desktop/run_hosted.sh",
        "env --unset=RUNNER_ENVIRONMENT run_hosted.sh",
        "unset GITHUB_ACTIONS",
        "unset FOO RUNNER_ENVIRONMENT",
        "echo 'RUNNER_ENVIRONMENT=self-hosted' >> \"$GITHUB_ENV\"",
    ],
)
def test_the_runner_identity_check_finds_every_override(script: str) -> None:
    assert _RUNNER_IDENTITY_OVERRIDE.search(script)
