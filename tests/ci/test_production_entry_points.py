"""Run every production entry point under its job's declared dependency set.

Three hosted failures in a row were the same shape: a job's interpreter lacked
something an entry point imported, and the command died before doing any work.
The failure only surfaced after a multi-hour hosted run, and each instance cost
a full cycle to diagnose.

This module closes that loop locally.  For each entry point the production
workflows invoke, it records which jobs run it and which third-party top-level
modules that entry point is allowed to import.  It then runs the program in a
subprocess whose import system refuses everything else, which is the hosted
condition reproduced without building an environment, and separately checks
that every job running the entry point installs at least what the entry point
may import.

The subprocesses run ``--help``, so each one costs milliseconds.  That is the
right depth for this class: all three failures were at import time, before any
argument was read.  The deeper archive-evidence path, where lazy adapter loading
matters, is covered by
``test_desktop_production_expectations.test_archive_expectations_never_import_the_sbom_chain``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from tests.ci.verify_desktop_production_gate import ROOT

#: Top-level modules the core distribution and its dependencies provide.
CORE_MODULES = frozenset(
    {
        "aiosqlite",
        "httpx",
        "packaging",
        "platformdirs",
        "pyperclip",
        "sigstore",
        "textual",
        "tongs",
    }
)
#: Top-level modules only the ``dev`` extra provides.
DEV_MODULES = frozenset({"jsonschema", "mkdocs", "pytest", "yaml"})
#: Top-level modules only the ``mcp`` extra provides.
MCP_MODULES = frozenset({"mcp"})
OPTIONAL_MODULES = CORE_MODULES | DEV_MODULES | MCP_MODULES

#: What each pip invocation in the workflows makes importable.  Anything not
#: listed here contributes nothing an entry point of ours may import.
INSTALL_PROVIDES: dict[str, frozenset[str]] = {
    'python -m pip install -e ".[dev,mcp]"': CORE_MODULES | DEV_MODULES | MCP_MODULES,
    'python -m pip install -e ".[dev]"': CORE_MODULES | DEV_MODULES,
    "python -m pip install -e .": CORE_MODULES,
    '"$TRANSFER_PYTHON" -I -m pip install --isolated '
    "--disable-pip-version-check -e .": CORE_MODULES,
    '"$CANDIDATE_PYTHON" -I -m pip install --isolated '
    "--disable-pip-version-check -e . "
    '"jsonschema>=4.18,<5" "sigstore==${SIGSTORE_VERSION}"': CORE_MODULES
    | frozenset({"jsonschema"}),
    '"$CANDIDATE_PYTHON" -I -m pip install --isolated '
    "--disable-pip-version-check -e . "
    '"sigstore==${SIGSTORE_VERSION}"': CORE_MODULES,
    '"$CANDIDATE_PYTHON" -I -m pip install --isolated '
    '--disable-pip-version-check -e ".[dev]" '
    '"sigstore==${SIGSTORE_VERSION}"': CORE_MODULES | DEV_MODULES,
    "python -m pip install build": frozenset(),
    "python -m pip install ./examples/desktop-plugin": frozenset(),
}

#: Every workflow is parsed.  Scoping this to two files is what let the fifth
#: instance of the missing-dependency class reach a trusted push run: the
#: candidate attestation job invokes the SPDX generator, and nothing here looked
#: at release-desktop.yml.
WORKFLOW_DIRECTORY = ".github/workflows"


@dataclass(frozen=True)
class EntryPoint:
    """One production program, its callers, and what it may import."""

    program: str
    #: Jobs that invoke it, as ``<workflow>:<job>``.
    jobs: tuple[str, ...]
    #: Third-party top-level modules the program may import to reach argument
    #: parsing.  Empty means the program must be standard library only.
    permitted: frozenset[str] = frozenset()
    #: Modules a subcommand needs later, which the job must therefore install
    #: even though importing the module itself does not require them.
    command_requires: frozenset[str] = frozenset()
    arguments: tuple[str, ...] = ("--help",)
    note: str = ""


ENTRY_POINTS: tuple[EntryPoint, ...] = (
    EntryPoint(
        program="tests/ci/verify_desktop_ci.py",
        jobs=("ci.yml:core", "ci.yml:desktop-pr-gate"),
        note="the aggregate job installs nothing, so this must stay stdlib only",
    ),
    EntryPoint(
        program="tests/ci/verify_desktop_production_gate.py",
        jobs=("ci.yml:desktop-pr-gate",),
        note="loads the issue #110 reader and #115 parsers by path, both stdlib",
    ),
    EntryPoint(
        program="tests/ci/desktop_stage_receipt.py",
        jobs=(
            "ci.yml:core",
            "desktop-production.yml:desktop-tap",
            "desktop-production.yml:installed-core",
            "desktop-production.yml:rpm-lifecycle",
            "desktop-production.yml:native-payload",
        ),
        note="installed-core installs only build, so this must stay stdlib only",
    ),
    EntryPoint(
        program="tests/integration/desktop/installed_core_composition.py",
        jobs=("desktop-production.yml:installed-core",),
        note="the issue #123 producer builds its own environments",
    ),
    EntryPoint(
        program="tests/integration/desktop/rpm_payload_contract.py",
        jobs=("desktop-production.yml:rpm-lifecycle",),
        permitted=frozenset({"packaging"}),
        note="loads package_contract.py, which imports packaging.version",
    ),
    EntryPoint(
        program="tests/containers/verify_expected_failure.py",
        jobs=("desktop-podman-probe.yml:fedora-44-x86-64",),
        note="the probe job installs nothing, so this must stay stdlib only",
    ),
    EntryPoint(
        program="tests/integration/desktop/candidate_attestation.py",
        jobs=(
            "desktop-production.yml:archive",
            "release-desktop.yml:candidate-archive",
            "release-desktop.yml:candidate-attestation",
        ),
        permitted=CORE_MODULES,
        note="issue #120's transfer command, run from the job's isolated venv",
    ),
    EntryPoint(
        program="scripts/build_desktop_sbom.py",
        jobs=("release-desktop.yml:candidate-attestation",),
        permitted=CORE_MODULES | frozenset({"jsonschema"}),
        note="validates against the pinned SPDX schema, so it needs jsonschema",
    ),
    EntryPoint(
        program="tests/ci/desktop_production_expectations.py",
        jobs=(
            "desktop-production.yml:archive-evidence",
            "desktop-production.yml:archive-sbom",
        ),
        command_requires=frozenset({"tongs", "sigstore", "packaging"}),
        note="adapters load lazily per subcommand, so importing it stays stdlib only",
    ),
)

_MINIMAL_ENVIRONMENT_PROBE = """
import sys, runpy

BLOCKED = frozenset({blocked!r})


class _MinimalEnvironment:
    \"\"\"Refuse anything the job's declared dependency set does not install.\"\"\"

    def find_spec(self, name, path=None, target=None):
        root = name.split(".", 1)[0]
        if root in BLOCKED:
            raise ImportError(
                root + " is absent from this job's declared dependency set"
            )
        return None


sys.meta_path.insert(0, _MinimalEnvironment())
sys.path.insert(0, {root!r})
sys.argv = [{program!r}, *{arguments!r}]
runpy.run_path({program!r}, run_name="__main__")
"""


def _run_under_minimal_environment(
    entry: EntryPoint,
) -> subprocess.CompletedProcess[str]:
    blocked = sorted(OPTIONAL_MODULES - entry.permitted)
    script = _MINIMAL_ENVIRONMENT_PROBE.format(
        blocked=blocked,
        root=str(ROOT),
        program=str(ROOT / entry.program),
        arguments=list(entry.arguments),
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
        timeout=120,
    )


@pytest.fixture(scope="module")
def workflow_jobs() -> dict[str, dict]:
    jobs: dict[str, dict] = {}
    for path in sorted((ROOT / WORKFLOW_DIRECTORY).glob("*.yml")):
        document = yaml.safe_load(path.read_text())
        for job, body in document["jobs"].items():
            if isinstance(body, dict) and "steps" in body:
                jobs[f"{path.name}:{job}"] = body
    return jobs


def _installed_modules(job: dict) -> frozenset[str]:
    provided: set[str] = set()
    for step in job["steps"]:
        for line in (step.get("run") or "").splitlines():
            if "pip install" not in line:
                continue
            normalized = " ".join(line.split())
            if normalized not in INSTALL_PROVIDES:
                raise AssertionError(
                    f"unrecognised install command {normalized!r}; record what it "
                    "makes importable in INSTALL_PROVIDES"
                )
            provided |= INSTALL_PROVIDES[normalized]
    return frozenset(provided)


def _invoked_programs(step: dict) -> set[str]:
    """Return the repository-relative programs a step runs directly.

    Matching on the whole path token matters: ``test_candidate_attestation.py``
    contains ``candidate_attestation.py`` as a substring but is a pytest target,
    not a program with a dependency set of its own.
    """

    programs = set()
    for token in (step.get("run") or "").split():
        candidate = token.strip('"').strip("'")
        if not candidate.endswith(".py"):
            continue
        if not candidate.startswith(("tests/", "scripts/", ".github/")):
            continue
        if Path(candidate).name.startswith("test_"):
            continue
        programs.add(candidate)
    return programs


def _jobs_invoking(program: str, jobs: dict[str, dict]) -> set[str]:
    found = set()
    for label, job in jobs.items():
        for step in job["steps"]:
            if program in _invoked_programs(step):
                found.add(label)
    return found


@pytest.mark.parametrize(
    "entry", ENTRY_POINTS, ids=[Path(item.program).stem for item in ENTRY_POINTS]
)
def test_entry_point_imports_under_its_declared_dependency_set(
    entry: EntryPoint,
) -> None:
    """Reaching argument parsing must not need an undeclared dependency."""

    completed = _run_under_minimal_environment(entry)
    assert completed.returncode == 0, (
        f"{entry.program} failed with only {sorted(entry.permitted)} permitted "
        f"({entry.note}):\n{completed.stderr}"
    )


@pytest.mark.parametrize(
    "entry", ENTRY_POINTS, ids=[Path(item.program).stem for item in ENTRY_POINTS]
)
def test_the_probe_rejects_a_dependency_the_entry_point_does_not_declare(
    entry: EntryPoint,
) -> None:
    """Prove the probe is not vacuous by blocking something it does need."""

    # A program permitted nothing cannot be starved of a declared dependency,
    # so block a module every interpreter has instead.  Either way the probe
    # must refuse to run the program.
    blocked = entry.permitted or frozenset({"json"})
    script = _MINIMAL_ENVIRONMENT_PROBE.format(
        blocked=sorted(OPTIONAL_MODULES | blocked),
        root=str(ROOT),
        program=str(ROOT / entry.program),
        arguments=list(entry.arguments),
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
        timeout=120,
    )
    assert completed.returncode != 0, (
        f"{entry.program} still ran with {sorted(blocked)} blocked, so the probe "
        "proves nothing"
    )
    assert "absent from this job's declared dependency set" in completed.stderr


@pytest.mark.parametrize(
    "entry", ENTRY_POINTS, ids=[Path(item.program).stem for item in ENTRY_POINTS]
)
def test_every_job_installs_what_its_entry_point_needs(
    entry: EntryPoint, workflow_jobs: dict[str, dict]
) -> None:
    """The workflows must actually provide what the table says is permitted."""

    assert _jobs_invoking(entry.program, workflow_jobs) == set(entry.jobs)
    required = entry.permitted | entry.command_requires
    for label in entry.jobs:
        provided = _installed_modules(workflow_jobs[label])
        missing = sorted(required - provided)
        assert not missing, (
            f"{label} runs {entry.program} but does not install {missing}"
        )


def test_the_table_covers_every_entry_point_the_workflows_invoke(
    workflow_jobs: dict[str, dict],
) -> None:
    """A new production program must be added here, not silently trusted."""

    recorded = {entry.program for entry in ENTRY_POINTS}
    invoked: set[str] = set()
    for job in workflow_jobs.values():
        for step in job["steps"]:
            invoked |= _invoked_programs(step)
    assert invoked <= recorded, sorted(invoked - recorded)
    assert invoked, "the workflows should invoke at least one recorded program"


def test_the_install_table_matches_the_declared_extras() -> None:
    """The recorded extras must match what pyproject actually declares."""

    pyproject = (ROOT / "pyproject.toml").read_text()
    for module, marker in (
        ("jsonschema", "jsonschema>="),
        ("yaml", "pyyaml>="),
        ("pytest", "pytest>="),
        ("mkdocs", "mkdocs-material>="),
    ):
        assert module in DEV_MODULES
        assert marker in pyproject, marker
    assert "mcp[cli]>=" in pyproject
    for module in ("sigstore", "packaging", "textual", "httpx", "aiosqlite"):
        assert module in CORE_MODULES
        assert f'"{module}>=' in pyproject, module


def test_recorded_entry_point_programs_exist() -> None:
    for entry in ENTRY_POINTS:
        assert (ROOT / entry.program).is_file(), entry.program
    assert json.dumps(sorted(OPTIONAL_MODULES))
