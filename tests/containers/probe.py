"""Run the Fedora container checks and retain structured evidence."""

from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import venv
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

CHECKOUT = Path("/checkout")
OUTPUT = Path("/output")
SOURCE = Path("/tmp/tongs-source")
ENVIRONMENT = Path("/tmp/tongs-installed")


@dataclass(frozen=True)
class StepResult:
    """Serializable result for one validation step."""

    name: str
    command: list[str]
    returncode: int
    duration_seconds: float
    stdout: str
    stderr: str


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _run(
    name: str,
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> StepResult:
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    result = StepResult(
        name=name,
        command=command,
        returncode=completed.returncode,
        duration_seconds=round(time.monotonic() - started, 3),
        stdout=f"{name}.stdout.txt",
        stderr=f"{name}.stderr.txt",
    )
    (OUTPUT / result.stdout).write_text(completed.stdout)
    (OUTPUT / result.stderr).write_text(completed.stderr)
    return result


def _environment() -> dict[str, Any]:
    distributions = {}
    for name in (
        "aiosqlite",
        "build",
        "hatch-vcs",
        "hatchling",
        "httpx",
        "mcp",
        "platformdirs",
        "pyperclip",
        "pytest",
        "pytest-asyncio",
        "textual",
    ):
        distributions[name] = importlib.metadata.version(name)
    os_release = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            os_release[key] = value.strip('"')
    return {
        "architecture": platform.machine(),
        "kernel": platform.release(),
        "os_release": os_release,
        "packages": distributions,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "source_sha": os.environ.get("TONGS_SOURCE_SHA", "unknown"),
    }


def _copy_source() -> None:
    shutil.copytree(
        CHECKOUT,
        SOURCE,
        ignore=shutil.ignore_patterns(
            ".git",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            ".venv",
            "__pycache__",
            "node_modules",
        ),
    )


def _installed_plugin_probe(python: Path) -> list[str]:
    script = """
import json
import sys
from pathlib import Path

from tongs.plugins.registry import PluginRegistry

sys.path.insert(0, str(Path('/tmp/tongs-source/spikes/desktop')))
from backend import Backend

registry = PluginRegistry()
registry.discover({'mcp': {'enabled': False}})
plugins = {plugin.name: plugin for plugin in registry.plugins}
assert {'sample-desktop', 'sample-terminal'} <= plugins.keys()
terminal_commands = plugins['sample-terminal'].get_commands()
assert terminal_commands and terminal_commands[0][0] == 'Sample terminal action'

desktop_records = {
    record['id']: record for record in Backend().invoke('list_plugins')
}
assert desktop_records['sample-desktop']['status'] == 'ready'
assert desktop_records['sample-terminal']['status'] == 'terminal_only'
assert desktop_records['sample-terminal']['modules'] == []
print(json.dumps({
    'desktop_backend_statuses': {
        name: desktop_records[name]['status']
        for name in ('sample-desktop', 'sample-terminal')
    },
    'terminal_discovery': sorted(plugins),
    'terminal_only_command': terminal_commands[0][0],
}, sort_keys=True))
"""
    return [str(python), "-c", script]


def _deliberate_failure(python: Path) -> list[str]:
    failure_test = Path("/tmp/test_deliberate_failure.py")
    failure_test.write_text(
        "def test_deliberate_failure_propagates():\n"
        '    assert False, "intentional harness failure for issue 27"\n'
    )
    return [
        str(python),
        "-m",
        "pytest",
        "-q",
        str(failure_test),
        f"--junitxml={OUTPUT / 'deliberate-failure.junit.xml'}",
    ]


def main() -> int:
    inject_failure = sys.argv[1:] == ["--inject-failure"]
    if sys.argv[1:] not in ([], ["--inject-failure"]):
        print("usage: probe.py [--inject-failure]", file=sys.stderr)
        return 2

    OUTPUT.mkdir(parents=True, exist_ok=True)
    _write_json(OUTPUT / "environment.json", _environment())
    _copy_source()

    build_env = os.environ.copy()
    build_env["SETUPTOOLS_SCM_PRETEND_VERSION_FOR_TONGS"] = "0.0.0+podman"
    steps: list[StepResult] = []

    steps.append(
        _run(
            "build-core-wheel",
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                str(OUTPUT / "wheels"),
                str(SOURCE),
            ],
            env=build_env,
        )
    )
    steps.append(
        _run(
            "build-reference-plugin-wheel",
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                str(OUTPUT / "wheels"),
                str(SOURCE / "spikes/desktop/reference-plugin"),
            ],
        )
    )

    if all(step.returncode == 0 for step in steps):
        venv.EnvBuilder(with_pip=True, system_site_packages=True).create(ENVIRONMENT)
        python = ENVIRONMENT / "bin/python"
        wheels = sorted((OUTPUT / "wheels").glob("*.whl"))
        steps.append(
            _run(
                "install-wheels",
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-deps",
                    *map(str, wheels),
                ],
            )
        )
    else:
        python = ENVIRONMENT / "bin/python"

    if steps[-1].returncode == 0 and python.is_file():
        steps.extend(
            [
                _run(
                    "cli-help",
                    [str(ENVIRONMENT / "bin/tongs"), "--help"],
                    cwd=Path("/tmp"),
                ),
                _run(
                    "core-tests",
                    [
                        str(python),
                        "-m",
                        "pytest",
                        "-q",
                        str(SOURCE / "tests"),
                        f"--junitxml={OUTPUT / 'core-tests.junit.xml'}",
                    ],
                    cwd=Path("/tmp"),
                ),
                _run(
                    "backend-fixture-tests",
                    [
                        str(python),
                        "-m",
                        "pytest",
                        "-q",
                        str(SOURCE / "spikes/desktop/tests/test_backend.py"),
                        f"--junitxml={OUTPUT / 'backend-tests.junit.xml'}",
                    ],
                    cwd=SOURCE / "spikes/desktop",
                ),
                _run(
                    "installed-plugin-discovery",
                    _installed_plugin_probe(python),
                    cwd=Path("/tmp"),
                ),
            ]
        )
        plugin_result = steps[-1]
        if plugin_result.returncode == 0:
            try:
                plugin_evidence = json.loads(
                    (OUTPUT / plugin_result.stdout).read_text()
                )
            except json.JSONDecodeError:
                plugin_evidence = {"error": "plugin probe returned invalid JSON"}
            _write_json(OUTPUT / "plugin-discovery.json", plugin_evidence)
        if inject_failure:
            steps.append(
                _run(
                    "deliberate-failure",
                    _deliberate_failure(python),
                    cwd=Path("/tmp"),
                )
            )

    failed = [step.name for step in steps if step.returncode != 0]
    summary = {
        "expected_result": "failure" if inject_failure else "success",
        "failed_steps": failed,
        "result": "failed" if failed else "passed",
        "steps": [asdict(step) for step in steps],
    }
    _write_json(OUTPUT / "summary.json", summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
