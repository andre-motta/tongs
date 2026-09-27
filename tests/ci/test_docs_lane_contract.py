"""Pin the Markdown lint step of the ``ci.yml`` docs lane.

The docs job's site build steps are pinned by ``test_production_workflow_contract``.
This module owns the rest of the job: the ``Lint Markdown`` step, the pinned
markdownlint-cli2 install under ``.github/linters`` and the job's read-only
permissions.  The linter is installed with ``npm ci`` from a committed
lockfile, so the version the job runs is the one this module reads from the
pin files.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
LINTERS = ROOT / ".github/linters"
PACKAGE_JSON = LINTERS / "package.json"
PACKAGE_LOCK = LINTERS / "package-lock.json"
CONFIG = ".github/linters/.markdownlint-cli2.jsonc"
LINTER = "markdownlint-cli2"
LINTER_BIN = f".github/linters/node_modules/.bin/{LINTER}"
LINT_STEP = "Lint Markdown"
SITE_BUILD = "npm run build --prefix site"
LINT_GLOBS = ("docs/**/*.md", "*.md")
REGISTRY = "https://registry.npmjs.org/"


@pytest.fixture(scope="module")
def docs_job() -> dict[str, Any]:
    return yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]["docs"]


def _steps_named(job: dict[str, Any], name: str) -> list[dict[str, Any]]:
    return [step for step in job["steps"] if step.get("name") == name]


def _lint_argv(job: dict[str, Any]) -> list[str]:
    (step,) = _steps_named(job, LINT_STEP)
    return shlex.split(step["run"])


def _pinned_version() -> str:
    package = json.loads(PACKAGE_JSON.read_text())
    return package["devDependencies"][LINTER]


def test_the_lint_step_runs_the_pinned_binary_with_the_committed_config(
    docs_job: dict[str, Any],
) -> None:
    argv = _lint_argv(docs_job)
    assert argv[0] == LINTER_BIN
    assert argv[1:3] == ["--config", CONFIG]
    assert (ROOT / CONFIG).is_file()
    assert (ROOT / CONFIG).parent == LINTERS


def test_the_lint_globs_cover_docs_and_root_markdown(
    docs_job: dict[str, Any],
) -> None:
    globs = tuple(_lint_argv(docs_job)[3:])
    assert globs == LINT_GLOBS
    covered: set[Path] = set()
    for pattern in globs:
        covered.update(ROOT.glob(pattern))
    expected = set((ROOT / "docs").rglob("*.md")) | set(ROOT.glob("*.md"))
    # Everything under docs/ is linted, including files the site build leaves out
    # of the site. Checked by walking the tree, not by naming files, so docs
    # edits never need the core lane to keep this test honest.
    assert covered == expected


def test_the_install_is_a_lockfile_npm_ci_under_the_linters_directory(
    docs_job: dict[str, Any],
) -> None:
    commands = [shlex.split(step["run"]) for step in docs_job["steps"] if "run" in step]
    # The site build has its own npm ci; only the linter install is owned here.
    installs = [
        argv
        for argv in commands
        if argv[:1] == ["npm"] and argv[argv.index("--prefix") + 1] == ".github/linters"
    ]
    assert len(installs) == 1
    (install,) = installs
    assert install[:2] == ["npm", "ci"]
    assert "--ignore-scripts" in install
    lint_index = [step.get("name") for step in docs_job["steps"]].index(LINT_STEP)
    install_index = next(
        index
        for index, step in enumerate(docs_job["steps"])
        if step.get("run", "").startswith("npm ci --prefix .github/linters")
    )
    assert install_index < lint_index


def test_the_pin_is_exact_and_matches_the_lockfile() -> None:
    version = _pinned_version()
    assert version[0].isdigit()
    assert all(part.isdigit() for part in version.split("."))
    package = json.loads(PACKAGE_JSON.read_text())
    assert set(package) >= {"private", "devDependencies"}
    assert package["private"] is True
    assert "dependencies" not in package
    lock = json.loads(PACKAGE_LOCK.read_text())
    assert lock["lockfileVersion"] == 3
    root = lock["packages"][""]
    assert root["devDependencies"] == package["devDependencies"]
    assert lock["packages"][f"node_modules/{LINTER}"]["version"] == version


def test_every_locked_package_is_registry_resolved_with_integrity() -> None:
    lock = json.loads(PACKAGE_LOCK.read_text())
    packages = {name: entry for name, entry in lock["packages"].items() if name != ""}
    assert packages
    for name, entry in packages.items():
        assert entry["resolved"].startswith(REGISTRY), name
        assert entry["integrity"].startswith("sha512-"), name


def test_the_docs_job_has_no_write_permissions(docs_job: dict[str, Any]) -> None:
    assert docs_job["permissions"] == {"contents": "read"}
    workflow = yaml.safe_load(CI_WORKFLOW.read_text())
    assert "write" not in json.dumps(workflow.get("permissions", {}))
