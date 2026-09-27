"""Run the Fedora container checks and retain structured evidence."""

from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
import time
import venv
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

CHECKOUT = Path("/checkout")
OUTPUT = Path("/output")
SOURCE = Path("/tmp/tongs-source")
ENVIRONMENT = Path("/tmp/tongs-installed")
EXAMPLE_PLUGIN = Path("examples/desktop-plugin")
# Installed-wheel smoke subset, grouped by what it loads from the wheel:
# entry points and the MCP dependency gate, the desktop entry-point group and
# plugin resources, packaged schemas and desktop assets, the installed sidecar,
# the MCP server import and host gate, config and platformdirs, and installed
# launcher resolution. The hosted core lane runs the whole suite.
SMOKE_TESTS = (
    "tests/test_plugins/test_plugin_system.py",
    "tests/plugins/test_desktop_discovery.py",
    "tests/plugins/test_desktop_resources.py",
    "tests/desktop/artifact_contract/test_schemas.py",
    "tests/desktop/test_assets.py",
    "tests/desktop/test_sidecar.py",
    "tests/test_mcp/test_server.py",
    "tests/test_config.py",
    "tests/desktop/installer/test_launcher.py",
)
EXPECTED_PLUGIN_EVIDENCE = {
    "desktop_states": {
        "example_dashboard": "discovered",
        "mcp": "terminal_only",
    },
    "terminal_discovery": ["example_dashboard"],
    "terminal_command": "Example dashboard (terminal)",
}


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
        "requested_head_sha": os.environ.get("TONGS_HEAD_SHA", "unknown"),
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


def validate_plugin_evidence(raw: str) -> dict[str, object]:
    """Parse and validate the installed plugin probe output."""
    try:
        evidence = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("plugin probe returned invalid JSON") from error
    if not isinstance(evidence, dict):
        raise TypeError("plugin probe must return a JSON object")
    if evidence != EXPECTED_PLUGIN_EVIDENCE:
        raise ValueError("plugin probe returned unexpected compatibility evidence")
    return evidence


def missing_smoke_tests(source: Path) -> list[str]:
    """Return the smoke test paths that are absent from the source copy."""
    return [path for path in SMOKE_TESTS if not (source / path).is_file()]


def smoke_tests_without_results(junit: Path) -> list[str]:
    """Return the smoke test paths that contributed no testcase to a report."""
    try:
        root = ET.parse(junit).getroot()
    except (OSError, ET.ParseError) as error:
        raise ValueError(f"smoke report is unreadable: {junit.name}") from error
    classnames = [case.attrib.get("classname", "") for case in root.iter("testcase")]
    empty = []
    for path in SMOKE_TESTS:
        module = path.removesuffix(".py").replace("/", ".")
        if not any(
            name == module or name.startswith(f"{module}.") for name in classnames
        ):
            empty.append(path)
    return empty


def _expose_harness_dependencies(python: Path) -> None:
    completed = subprocess.run(
        [
            str(python),
            "-c",
            "import sysconfig; print(sysconfig.get_path('purelib'))",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    child_site_packages = Path(completed.stdout.strip())
    harness_site_packages = Path(sysconfig.get_path("purelib"))
    (child_site_packages / "_tongs_harness_dependencies.pth").write_text(
        f"{harness_site_packages}\n"
    )


def _installed_plugin_probe(python: Path) -> list[str]:
    script = """
import json

from tongs.plugins.desktop_registry import DesktopPluginRegistry
from tongs.plugins.registry import PluginRegistry

registry = PluginRegistry()
registry.discover({'mcp': {'enabled': False}})
plugins = {plugin.name: plugin for plugin in registry.plugins}
terminal_commands = plugins['example_dashboard'].get_commands()

desktop_records = {
    record.plugin_id: record for record in DesktopPluginRegistry().discover()
}
dashboard = desktop_records['example_dashboard']
assert dashboard.has_terminal_entry_point and dashboard.has_desktop_entry_point
assert not desktop_records['mcp'].has_desktop_entry_point
print(json.dumps({
    'desktop_states': {
        name: str(record.state) for name, record in desktop_records.items()
    },
    'terminal_discovery': sorted(plugins),
    'terminal_command': terminal_commands[0][0],
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


def _summary(steps: list[StepResult], *, inject_failure: bool) -> int:
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


def _fail_step(result: StepResult, message: str) -> StepResult:
    with (OUTPUT / result.stderr).open("a") as stderr:
        print(message, file=stderr)
    return replace(result, returncode=1)


def _smoke_tests(python: Path) -> StepResult:
    junit = OUTPUT / "smoke-tests.junit.xml"
    missing = missing_smoke_tests(SOURCE)
    result = _run(
        "smoke-tests",
        [
            str(python),
            "-m",
            "pytest",
            "-q",
            "-m",
            "not needs_git",
            *(str(SOURCE / path) for path in SMOKE_TESTS if path not in missing),
            f"--junitxml={junit}",
        ],
        cwd=Path("/tmp"),
    )
    if missing:
        return _fail_step(result, f"Smoke test paths are missing: {missing}")
    if result.returncode == 0:
        try:
            empty = smoke_tests_without_results(junit)
        except ValueError as error:
            return _fail_step(result, str(error))
        if empty:
            return _fail_step(result, f"Smoke test paths ran no tests: {empty}")
    return result


def _plugin_discovery(python: Path) -> StepResult:
    result = _run(
        "installed-plugin-discovery",
        _installed_plugin_probe(python),
        cwd=Path("/tmp"),
    )
    if result.returncode != 0:
        return result
    try:
        evidence = validate_plugin_evidence((OUTPUT / result.stdout).read_text())
    except (OSError, TypeError, ValueError) as error:
        return _fail_step(result, f"Plugin evidence validation failed: {error}")
    _write_json(OUTPUT / "plugin-discovery.json", evidence)
    return result


def main() -> int:
    inject_failure = sys.argv[1:] == ["--inject-failure"]
    if sys.argv[1:] not in ([], ["--inject-failure"]):
        print("usage: probe.py [--inject-failure]", file=sys.stderr)
        return 2

    OUTPUT.mkdir(parents=True, exist_ok=True)
    _write_json(OUTPUT / "environment.json", _environment())

    if inject_failure:
        # The success run proves the installed product; this run proves only
        # that a failing step leaves the container with a nonzero status.
        steps = [
            _run(
                "deliberate-failure",
                _deliberate_failure(Path(sys.executable)),
                cwd=Path("/tmp"),
            )
        ]
        return _summary(steps, inject_failure=True)

    _copy_source()
    build_env = os.environ.copy()
    build_env["SETUPTOOLS_SCM_PRETEND_VERSION"] = "0.0.0+podman"
    build_env["SETUPTOOLS_SCM_PRETEND_VERSION_FOR_TONGS"] = "0.0.0+podman"
    steps: list[StepResult] = []

    for name, project, env in (
        ("build-core-wheel", SOURCE, build_env),
        ("build-example-plugin-wheel", SOURCE / EXAMPLE_PLUGIN, None),
    ):
        steps.append(
            _run(
                name,
                [
                    sys.executable,
                    "-m",
                    "build",
                    "--wheel",
                    "--no-isolation",
                    "--outdir",
                    str(OUTPUT / "wheels"),
                    str(project),
                ],
                env=env,
            )
        )

    python = ENVIRONMENT / "bin/python"
    if all(step.returncode == 0 for step in steps):
        venv.EnvBuilder(with_pip=True).create(ENVIRONMENT)
        _expose_harness_dependencies(python)
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

    if steps[-1].returncode == 0 and python.is_file():
        steps.append(
            _run(
                "cli-help",
                [str(ENVIRONMENT / "bin/tongs"), "--help"],
                cwd=Path("/tmp"),
            )
        )
        steps.append(_smoke_tests(python))
        steps.append(_plugin_discovery(python))

    return _summary(steps, inject_failure=False)


if __name__ == "__main__":
    raise SystemExit(main())
