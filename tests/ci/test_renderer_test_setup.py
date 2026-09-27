"""Pin that every desktop renderer test run loads the shared Testing Library setup.

``tests/desktop/renderer/setup.mjs`` raises Testing Library's async wait
timeout for slow runners and keeps element query failures short (issue #338).
It only takes effect when the test runner loads it with ``--import``, so a
command that starts the renderer tests without it would silently fall back to
the 1000 ms default. This module finds every such command, in the desktop
``npm test`` script and in every workflow step, and requires the import.
"""

from __future__ import annotations

import json
import shlex
from typing import Any

import yaml

from tests.ci.verify_desktop_production_gate import ROOT

SETUP = ROOT / "tests/desktop/renderer/setup.mjs"
RENDERER_GLOB = "tests/desktop/renderer/*.test.mjs"
SETUP_IMPORT = ("--import", "../tests/desktop/renderer/setup.mjs")


def _imports_setup(command: str) -> bool:
    """Whether a ``node --test`` command imports the setup before its files."""

    words = shlex.split(command.replace("\\\n", " "))
    start = words.index("--test")
    for index in range(start, len(words) - 1):
        if tuple(words[index : index + 2]) == SETUP_IMPORT:
            return index < next(
                position
                for position, word in enumerate(words)
                if word.endswith(RENDERER_GLOB)
            )
    return False


def _workflow_renderer_steps() -> list[tuple[str, dict[str, Any]]]:
    workflow_dir = ROOT / ".github/workflows"
    paths = sorted(workflow_dir.glob("*.yml")) + sorted(workflow_dir.glob("*.yaml"))
    steps: list[tuple[str, dict[str, Any]]] = []
    for path in paths:
        workflow = yaml.safe_load(path.read_text())
        for job in (workflow.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                if RENDERER_GLOB in str(step.get("run", "")):
                    steps.append((path.name, step))
    return steps


def test_the_setup_module_exists() -> None:
    assert SETUP.is_file()
    text = SETUP.read_text()
    assert "asyncUtilTimeout: ASYNC_UTIL_TIMEOUT_MS" in text
    assert "getElementError" in text


def test_npm_test_loads_the_setup_for_the_renderer_tests() -> None:
    package = json.loads((ROOT / "desktop/package.json").read_text())
    script = package["scripts"]["test"]
    (node_command,) = [
        part.strip() for part in script.split("&&") if "node --test" in part
    ]
    assert RENDERER_GLOB in node_command
    assert _imports_setup(node_command)


def test_every_workflow_renderer_run_loads_the_setup() -> None:
    steps = _workflow_renderer_steps()
    assert [name for name, _ in steps] == ["desktop-production.yml"]
    for name, step in steps:
        # The import path is relative to the desktop package directory.
        assert step.get("working-directory") == "desktop", name
        assert _imports_setup(step["run"]), name


def test_the_ci_fixture_lane_runs_renderer_tests_through_npm_test() -> None:
    """The desktop fixture lane reaches the renderer tests only through the
    ``npm test`` script checked above, so it inherits the setup import."""

    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    runs = [
        str(step.get("run", ""))
        for job in workflow["jobs"].values()
        for step in job.get("steps") or []
    ]
    assert any("npm test --prefix desktop" in run for run in runs)
    assert not any("node --test" in run and "desktop" in run for run in runs)
