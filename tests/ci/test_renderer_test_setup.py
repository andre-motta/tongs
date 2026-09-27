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
import re
import shlex
from typing import Any

import yaml

from tests.ci.verify_desktop_production_gate import ROOT

SETUP = ROOT / "tests/desktop/renderer/setup.mjs"
RENDERER_GLOB = "tests/desktop/renderer/*.test.mjs"
SETUP_IMPORT = ("--import", "../tests/desktop/renderer/setup.mjs")
RENDERER_DIR = "tests/desktop/renderer/"
DOCUMENTED_SETUP_PATHS = (
    "./tests/desktop/renderer/setup.mjs",
    "../tests/desktop/renderer/setup.mjs",
)
TESTING_GUIDE = ROOT / ".agents/testing/README.md"


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


def _documented_renderer_commands(text: str) -> list[list[str]]:
    """Every ``node --test`` command in a Markdown shell block that runs
    renderer test files, as its words."""

    commands: list[list[str]] = []
    for block in re.findall(r"```(?:bash|sh|shell)\n(.*?)```", text, re.DOTALL):
        for line in block.replace("\\\n", " ").splitlines():
            if "node --test" not in line:
                continue
            words = shlex.split(line)
            if any(
                word.startswith(RENDERER_DIR) and word.endswith(".test.mjs")
                for word in words
            ):
                commands.append(words)
    return commands


def _documented_command_imports_setup(words: list[str]) -> bool:
    """Whether the setup import comes before the first renderer test file."""

    first_file = next(
        index
        for index, word in enumerate(words)
        if word.startswith(RENDERER_DIR) and word.endswith(".test.mjs")
    )
    return any(
        words[index] == "--import" and words[index + 1] in DOCUMENTED_SETUP_PATHS
        for index in range(words.index("--test"), first_file - 1)
    )


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


def test_ci_yml_runs_no_second_copy_of_the_renderer_tests() -> None:
    """The desktop production TAP job is the single workflow run of the
    Electron and renderer tests, checked above; ordinary CI runs neither
    ``npm test`` nor ``node --test`` for the desktop package."""

    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    runs = [
        str(step.get("run", ""))
        for job in workflow["jobs"].values()
        for step in job.get("steps") or []
    ]
    assert runs
    assert not any("npm test" in run and "desktop" in run for run in runs)
    assert not any("node --test" in run for run in runs)


def test_documented_renderer_runs_load_the_setup() -> None:
    """Focused renderer runs in the testing guide import the setup with a
    relative specifier, since a bare ``tests/...`` path does not resolve."""

    commands = _documented_renderer_commands(TESTING_GUIDE.read_text())
    assert len(commands) >= 2
    for words in commands:
        assert _documented_command_imports_setup(words), " ".join(words)


def test_documented_command_check_rejects_a_missing_or_bare_import() -> None:
    guide = (
        "```bash\n"
        "node --test --test-concurrency=1 tests/desktop/renderer/diff.test.mjs\n"
        "node --test --import tests/desktop/renderer/setup.mjs \\\n"
        "  tests/desktop/renderer/diff.test.mjs\n"
        "node --test --import ./tests/desktop/renderer/setup.mjs \\\n"
        "  tests/desktop/renderer/diff.test.mjs\n"
        "```\n"
    )
    commands = _documented_renderer_commands(guide)
    assert [_documented_command_imports_setup(words) for words in commands] == [
        False,
        False,
        True,
    ]
