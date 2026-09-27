"""Every test file has exactly one pre-merge job that runs it.

The owners are derived from the workflows themselves: the ``pytest`` commands
of every job, with their ``--ignore`` options and shell globs, and the
``node --test`` command of the desktop TAP job, resolved against the step's
working directory.  Each tracked test file must be run by exactly one job, once,
so a suite can neither fall out of CI nor run twice.  The Fedora Podman probe's
installed-wheel smoke subset is the one deliberate second run, and each of its
files must also have an ordinary owner.

The owning job also decides which lane a change to the file must select, so
this module checks that every test file selects the lane of the job that runs
it.  ``test_ci_plan_drift.py`` reuses :func:`pre_merge_owners` to map the
documentation a test reads to the reading test's lane.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import shlex
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any

import pytest
import yaml

from tests.ci.ci_plan import LANE_CI_JOBS, LANE_PRODUCTION_JOBS, classify_paths

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"
PRE_MERGE_WORKFLOWS = ("ci.yml", "desktop-production.yml")
FEDORA_PROBE = ROOT / "tests/containers/probe.py"
#: Test files pytest collects by default (``python_files``) and the node test
#: files this repository names.
PYTHON_TEST_PATTERNS = ("test_*.py", "*_test.py")
NODE_TEST_PATTERNS = ("*.test.mjs", "test_*.mjs")
TEST_ROOTS = ("tests/", "examples/")


def job_lane(label: str) -> str:
    """The lane whose selection runs the ``<workflow>:<job>`` label."""

    workflow, _, job = label.partition(":")
    if workflow == "ci.yml":
        (lane,) = [lane for lane, name in LANE_CI_JOBS.items() if name == job]
        return lane
    (lane,) = [lane for lane, jobs in LANE_PRODUCTION_JOBS.items() if job in jobs]
    return lane


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    name = PurePosixPath(path).name
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def _is_test_file(path: str) -> bool:
    return _matches(path, PYTHON_TEST_PATTERNS + NODE_TEST_PATTERNS)


def tracked_test_files() -> frozenset[str]:
    """Every tracked file a pytest or node test run would collect."""

    output = subprocess.run(
        ["git", "ls-files", "-z", *TEST_ROOTS],
        capture_output=True,
        cwd=ROOT,
        check=True,
    ).stdout.decode("utf-8")
    return frozenset(
        path for path in output.split("\0") if path and _is_test_file(path)
    )


def _jobs() -> Iterator[tuple[str, dict[str, Any]]]:
    for name in PRE_MERGE_WORKFLOWS:
        document = yaml.safe_load((WORKFLOWS / name).read_text())
        for job, body in document["jobs"].items():
            if isinstance(body, dict) and "steps" in body:
                yield f"{name}:{job}", body


def _words(script: str) -> list[str]:
    return shlex.split(script.replace("\\\n", " "), comments=True)


def _expand(argument: str, directory: Path) -> list[str]:
    """Repository-relative paths an argument names, with globs expanded."""

    if any(character in argument for character in "*?["):
        candidates = sorted(directory.glob(argument))
        assert candidates, f"{argument} matches nothing"
    else:
        candidates = [directory / argument]
    return [
        candidate.resolve().relative_to(ROOT).as_posix() for candidate in candidates
    ]


def _collected(
    paths: list[str],
    ignored: list[str],
    tracked: frozenset[str],
    patterns: tuple[str, ...],
) -> list[str]:
    """The tracked test files ``paths`` collect, less anything ``ignored``.

    A file named explicitly runs whatever its name; a directory collects the
    files below it that match ``patterns``.
    """

    found = []
    for path in paths:
        if path in tracked:
            found.append(path)
            continue
        assert (ROOT / path).is_dir(), f"{path} is neither a test file nor a directory"
        found.extend(
            sorted(
                item
                for item in tracked
                if item.startswith(path + "/") and _matches(item, patterns)
            )
        )
    # pytest drops every file under an ignored path, even one named explicitly.
    return [
        path
        for path in found
        if not any(path == item or path.startswith(item + "/") for item in ignored)
    ]


def _pytest_runs(step: dict[str, Any], tracked: frozenset[str]) -> list[str]:
    words = _words(step.get("run") or "")
    if "pytest" not in words:
        return []
    directory = ROOT / step.get("working-directory", ".")
    paths: list[str] = []
    ignored: list[str] = []
    for word in words[words.index("pytest") + 1 :]:
        if word.startswith("--ignore="):
            ignored.extend(_expand(word.removeprefix("--ignore="), directory))
        elif not word.startswith("-") and "$" not in word:
            paths.extend(_expand(word, directory))
    return _collected(paths, ignored, tracked, PYTHON_TEST_PATTERNS)


#: ``node --test`` options that take their value as the next word.
_NODE_VALUE_OPTIONS = frozenset({"--import", "--require", "--test-reporter"})


def _node_runs(step: dict[str, Any], tracked: frozenset[str]) -> list[str]:
    words = _words(step.get("run") or "")
    if words[:2] != ["node", "--test"]:
        return []
    directory = ROOT / step.get("working-directory", ".")
    paths: list[str] = []
    skip = False
    for word in words[2:]:
        if skip:
            skip = False
        elif word in _NODE_VALUE_OPTIONS:
            skip = True
        elif not word.startswith("-"):
            paths.extend(_expand(word, directory))
    return _collected(paths, [], tracked, NODE_TEST_PATTERNS)


def pre_merge_runs() -> dict[str, list[str]]:
    """Each tracked test file mapped to the job of every pre-merge run of it."""

    tracked = tracked_test_files()
    runs: dict[str, list[str]] = {path: [] for path in tracked}
    for label, job in _jobs():
        for step in job["steps"]:
            for path in _pytest_runs(step, tracked) + _node_runs(step, tracked):
                runs[path].append(label)
    return runs


def pre_merge_owners() -> dict[str, str]:
    """Each tracked test file mapped to its single owning job label."""

    return {path: labels[0] for path, labels in pre_merge_runs().items() if labels}


def _load_probe() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "lane_ownership_fedora_probe", FEDORA_PROBE
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    # Dataclasses resolve their module through sys.modules while executing.
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def runs() -> dict[str, list[str]]:
    return pre_merge_runs()


@pytest.mark.needs_git
def test_every_test_file_runs_exactly_once_before_merge(
    runs: dict[str, list[str]],
) -> None:
    assert "tests/test_config.py" in runs
    assert "examples/desktop-plugin/tests/test_dashboard_module.mjs" in runs
    orphans = sorted(path for path, labels in runs.items() if not labels)
    repeated = {path: labels for path, labels in runs.items() if len(labels) > 1}
    assert orphans == [], f"no pre-merge job runs these test files: {orphans}"
    assert repeated == {}, f"these test files run more than once: {repeated}"


@pytest.mark.needs_git
def test_the_owning_jobs_are_the_mapped_homes(runs: dict[str, list[str]]) -> None:
    owners = {path: labels[0] for path, labels in runs.items() if labels}
    expected = {
        "tests/ci/test_ci_plan.py": "ci.yml:lint-and-format",
        "tests/containers/test_verify_expected_failure.py": "ci.yml:lint-and-format",
        "tests/test_config.py": "ci.yml:core",
        "tests/test_mcp/test_server.py": "ci.yml:core",
        "tests/plugins/test_desktop_contract.py": "ci.yml:core",
        "tests/services/test_session.py": "ci.yml:core",
        "tests/integration/desktop/test_draft_process_acceptance.py": "ci.yml:core",
        "tests/integration/desktop/test_native_payload_acceptance.py": (
            "desktop-production.yml:native-payload"
        ),
        "tests/integration/desktop/test_sbom_evidence.py": (
            "desktop-production.yml:native-payload"
        ),
        "tests/packaging/rpm/desktop/test_contract.py": (
            "desktop-production.yml:native-payload"
        ),
        "tests/desktop/renderer/review-keys.test.mjs": (
            "desktop-production.yml:desktop-tap"
        ),
        "examples/desktop-plugin/tests/test_provider.py": (
            "desktop-production.yml:desktop-tap"
        ),
        "examples/desktop-plugin/tests/test_dashboard_module.mjs": (
            "desktop-production.yml:desktop-tap"
        ),
    }
    assert {path: owners.get(path) for path in expected} == expected


@pytest.mark.needs_git
def test_every_test_file_selects_the_lane_of_its_owning_job(
    runs: dict[str, list[str]],
) -> None:
    offenders = []
    for path, labels in sorted(runs.items()):
        for label in labels:
            lanes, _, _ = classify_paths([path])
            if job_lane(label) not in lanes:
                offenders.append(f"{path} ({label}) -> {sorted(lanes)}")
    assert offenders == []


@pytest.mark.needs_git
def test_the_fedora_smoke_subset_is_a_second_run_of_owned_files(
    runs: dict[str, list[str]],
) -> None:
    """The probe reruns part of the suite from the installed wheel on
    purpose; it must never be the only place a test file runs."""

    smoke = _load_probe().SMOKE_TESTS
    assert smoke
    assert [path for path in smoke if not runs.get(path)] == []
