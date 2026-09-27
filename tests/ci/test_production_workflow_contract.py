"""Cross-check the gate's consumer-owned constants against the workflow YAML.

``verify_desktop_production_gate`` declares the complete required job set, the
complete required check set, each check's evidence directory, receipt name,
report paths and ordered stage names.  Those constants are only useful if they
still describe what the workflows actually do.  Nothing else in this suite
compares the two: a rename in a workflow, a moved artifact path or a dropped
stage would otherwise surface only after a multi-hour hosted run.

This module parses the two workflows and asserts the agreement directly.  It
also anchors the two bespoke adapters, whose receipt and report names and stage
lists the gate restates as its own policy, to the constants those reviewed
adapters actually export, and pins the lane wiring: the ``changes`` job, the
canonical lane condition on every lane job, the ``run_archive`` and
``run_packaging`` inputs, the plan-derived core matrix, the lint job's harness
suites, the docs build lane and the plan-keyed aggregate.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.ci.desktop_production_expectations import archive_adapter, sbom_adapter
from tests.ci.verify_desktop_ci import CI_PLAN, REQUIRED_GATE_JOBS
from tests.ci.verify_desktop_production_gate import (
    ARTIFACT_LIFECYCLE,
    REQUIRED_CHECKS,
    REQUIRED_CI_JOBS,
    REQUIRED_PRODUCTION_JOBS,
    ROOT,
    RequiredCheck,
)

CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
PRODUCTION_WORKFLOW = ROOT / ".github/workflows/desktop-production.yml"
GATE_JOB = "desktop-pr-gate"
GATE_NAME = "CI aggregate"
CHANGES_JOB = "changes"
PLAN_PATH = '"$RUNNER_TEMP/ci-plan.json"'
ARCHIVE_CONDITION = "${{ inputs.run_archive }}"
PACKAGING_CONDITION = "${{ inputs.run_packaging }}"
#: The core matrix: the plan's interpreters, or every interpreter when the
#: plan job failed or published nothing.
CORE_MATRIX = (
    "${{ fromJSON(needs.changes.result == 'success' && "
    "needs.changes.outputs.core_versions || '"
    + json.dumps(list(CI_PLAN.CORE_VERSIONS_FULL), separators=(",", ":"))
    + "') }}"
)


def _lane_condition(lane: str) -> str:
    return (
        "${{ !cancelled() && (needs.changes.result != 'success' || "
        f"needs.changes.outputs.{lane} == 'true') }}}}"
    )


RESULTS_JOB = "production-results"
UNRESOLVED = "<workflow-expression>"
CHECKED_OUT_COMMIT = "<checked-out-commit>"
#: The two spellings of the one checked-out commit.  Ordinary CI holds it in a
#: workflow env variable; the called workflow reads it back from the
#: source-identity job that verified it.  ``test_the_two_checked_out_commit_
#: spellings_are_the_same_value`` proves they cannot diverge.
_COMMIT_SPELLINGS = (
    "${{ env.TONGS_CHECKED_OUT_SHA }}",
    "${{ needs.source-identity.outputs.checked-out-commit }}",
)


def _canonical_artifact_name(name: str) -> str:
    for spelling in _COMMIT_SPELLINGS:
        name = name.replace(spelling, CHECKED_OUT_COMMIT)
    return name


_EXPRESSION = re.compile(r"\$\{\{\s*(?P<body>[^}]+?)\s*\}\}")
_SHELL_VARIABLE = re.compile(r"\$(?P<name>[A-Z_][A-Z0-9_]*)\b")


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


@pytest.fixture(scope="module")
def ci() -> dict[str, Any]:
    return _load(CI_WORKFLOW)


@pytest.fixture(scope="module")
def production() -> dict[str, Any]:
    return _load(PRODUCTION_WORKFLOW)


def _substitutions(
    workflow: dict[str, Any], job: dict[str, Any], matrix: dict[str, str]
) -> dict[str, str]:
    values: dict[str, str] = {}
    for source in (workflow.get("env") or {}, job.get("env") or {}):
        for name, value in source.items():
            values[name] = str(value)
    resolved: dict[str, str] = {}
    for name, value in values.items():
        for key, replacement in matrix.items():
            value = value.replace(f"${{{{ matrix.{key} }}}}", replacement)
        resolved[name] = value
    return resolved


def _resolve(text: str, substitutions: dict[str, str], matrix: dict[str, str]) -> str:
    for key, replacement in matrix.items():
        text = text.replace(f"${{{{ matrix.{key} }}}}", replacement)

    def expression(match: re.Match[str]) -> str:
        body = match.group("body")
        if body.startswith("env."):
            return substitutions.get(body[4:], UNRESOLVED)
        return UNRESOLVED

    text = _EXPRESSION.sub(expression, text)
    return _SHELL_VARIABLE.sub(
        lambda match: substitutions.get(match.group("name"), UNRESOLVED), text
    )


def _plan(step: dict[str, Any], substitutions: dict[str, str], matrix: dict[str, str]):
    """Extract and decode the stage plan heredoc a step writes."""

    script = step.get("run", "")
    if "<<PLAN" not in script:
        return None
    body = script.split("<<PLAN", 1)[1].split("\nPLAN", 1)[0]
    return json.loads(_resolve(body, substitutions, matrix))


def _publish_arguments(
    step: dict[str, Any], substitutions: dict[str, str], matrix: dict[str, str]
) -> dict[str, str] | None:
    script = step.get("run", "")
    if "desktop_stage_receipt.py publish" not in script:
        return None
    tokens = shlex.split(_resolve(script, substitutions, matrix))
    arguments: dict[str, str] = {}
    for index, token in enumerate(tokens):
        if token.startswith("--") and index + 1 < len(tokens):
            arguments.setdefault(token, tokens[index + 1])
    return arguments


def _binder_publications(workflow: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Collect every stage the shared binder publishes, keyed by check id."""

    found: dict[str, dict[str, Any]] = {}
    for job_name, job in workflow["jobs"].items():
        if not isinstance(job, dict) or "steps" not in job:
            continue
        for selection in _matrix_selections(job):
            substitutions = _substitutions(workflow, job, selection)
            plan = None
            arguments = None
            for step in job["steps"]:
                plan = _plan(step, substitutions, selection) or plan
                arguments = (
                    _publish_arguments(step, substitutions, selection) or arguments
                )
            if plan is None or arguments is None:
                continue
            found[arguments["--check-id"]] = {
                "job": job_name,
                "plan": plan,
                "arguments": arguments,
            }
    return found


def _matrix_selections(job: dict[str, Any]) -> list[dict[str, str]]:
    """Every matrix leg a job can run; the plan-derived core matrix can run
    every interpreter, which :func:`test_the_core_matrix_follows_the_plan`
    pins."""

    matrix = (job.get("strategy") or {}).get("matrix") or {}
    if not matrix:
        return [{}]
    key, values = next(iter(matrix.items()))
    if values == CORE_MATRIX:
        values = CI_PLAN.CORE_VERSIONS_FULL
    return [{key: str(value)} for value in values]


def _flatten(value: object) -> str:
    return " ".join(str(value).split())


def _step_output_source(job: dict[str, Any], reference: str) -> str:
    """Resolve one ``steps.<id>.outputs.<name>`` reference to its producer.

    The archive job names its transfer artifact once in a step output and then
    reuses it, so following the indirection is what lets this test see the run
    identifiers the retry-safety rule is about.
    """

    match = re.fullmatch(
        r"\$\{\{\s*steps\.(?P<step>[\w-]+)\.outputs\.(?P<name>[\w-]+)\s*\}\}",
        reference.strip(),
    )
    if match is None:
        return reference
    for step in job.get("steps") or []:
        if step.get("id") != match.group("step"):
            continue
        script = step.get("run", "")
        for line in script.splitlines():
            if f"{match.group('name')}=" in line:
                return _flatten(line)
    return reference


def _artifact_names(workflow: dict[str, Any], action: str) -> dict[str, str]:
    """Map an artifact's destination or source path to its declared name."""

    names: dict[str, str] = {}
    for job in workflow["jobs"].values():
        if not isinstance(job, dict):
            continue
        for selection in _matrix_selections(job):
            for step in job.get("steps") or []:
                if action not in str(step.get("uses", "")):
                    continue
                with_block = step.get("with") or {}
                path = _flatten(with_block.get("path", ""))
                name = _flatten(with_block.get("name", ""))
                for key, replacement in selection.items():
                    path = path.replace(f"${{{{ matrix.{key} }}}}", replacement)
                    name = name.replace(f"${{{{ matrix.{key} }}}}", replacement)
                names[path] = name
    return names


def _check(check_id: str) -> RequiredCheck:
    return next(item for item in REQUIRED_CHECKS if item.check_id == check_id)


def test_required_ci_jobs_equal_the_aggregate_needs(ci: dict[str, Any]) -> None:
    needs = set(ci["jobs"][GATE_JOB]["needs"])
    assert needs == set(CI_PLAN.LANE_CI_JOBS.values()) | {CHANGES_JOB}
    assert needs == set(REQUIRED_CI_JOBS)
    assert needs == set(REQUIRED_GATE_JOBS)


def test_triggers_target_main_and_rerun_on_labels(ci: dict[str, Any]) -> None:
    # PyYAML reads the bare ``on`` key as boolean true.
    triggers = ci[True]
    assert set(triggers) == {"push", "pull_request"}
    assert triggers["push"] == {"branches": ["main"]}
    assert triggers["pull_request"] == {
        "branches": ["main"],
        "types": ["opened", "synchronize", "reopened", "labeled"],
    }


def test_only_pull_request_runs_cancel_each_other(ci: dict[str, Any]) -> None:
    concurrency = ci["concurrency"]
    assert _flatten(concurrency["group"]) == (
        "ci-${{ github.event_name == 'pull_request' && "
        "format('pr-{0}', github.event.pull_request.number) || "
        "format('run-{0}', github.run_id) }}"
    )
    assert concurrency["cancel-in-progress"] == (
        "${{ github.event_name == 'pull_request' }}"
    )


def test_the_changes_job_publishes_the_plan_and_every_lane(
    ci: dict[str, Any],
) -> None:
    job = ci["jobs"][CHANGES_JOB]
    assert job["name"] == "Plan CI lanes"
    assert "needs" not in job and "if" not in job
    assert job["permissions"] == {"contents": "read"}
    assert set(job["outputs"]) == {"plan", *CI_PLAN.LANES, "full", "core_versions"}
    for name, value in job["outputs"].items():
        assert value == f"${{{{ steps.plan.outputs.{name} }}}}", name
    checkout = job["steps"][0]
    assert checkout["with"] == {
        "ref": "${{ env.TONGS_CHECKED_OUT_SHA }}",
        "fetch-depth": 2,
        "persist-credentials": False,
    }
    plan = next(step for step in job["steps"] if step.get("id") == "plan")
    command = _flatten(plan["run"])
    assert command.startswith("python3 tests/ci/ci_plan.py compute ")
    for argument in (
        '--event-name "$GITHUB_EVENT_NAME"',
        '--event-path "$GITHUB_EVENT_PATH"',
        '--checked-out "$TONGS_CHECKED_OUT_SHA"',
        '--github-output "$GITHUB_OUTPUT"',
        '--step-summary "$GITHUB_STEP_SUMMARY"',
    ):
        assert argument in command, argument


def test_every_lane_job_carries_the_canonical_condition(ci: dict[str, Any]) -> None:
    for lane, job_name in CI_PLAN.LANE_CI_JOBS.items():
        job = ci["jobs"][job_name]
        assert job["needs"] == [CHANGES_JOB], job_name
        assert job["if"] == _lane_condition(lane), job_name
    lane_jobs = set(CI_PLAN.LANE_CI_JOBS.values())
    assert set(ci["jobs"]) == lane_jobs | {CHANGES_JOB, GATE_JOB}


def test_the_production_call_receives_the_archive_and_packaging_lanes(
    ci: dict[str, Any],
) -> None:
    call = ci["jobs"]["desktop-production"]
    for lane in ("archive", "packaging"):
        assert call["with"][f"run_{lane}"] == (
            "${{ needs.changes.result != 'success' || "
            f"needs.changes.outputs.{lane} == 'true' }}}}"
        )


def test_the_core_matrix_follows_the_plan(ci: dict[str, Any]) -> None:
    job = ci["jobs"]["core"]
    assert job["strategy"]["fail-fast"] is False
    assert job["strategy"]["matrix"] == {"python-version": CORE_MATRIX}
    assert job["env"]["CHECK_ID"] == "core-python-${{ matrix.python-version }}"


def _pytest_commands(job: dict[str, Any]) -> list[list[str]]:
    """The words of every pytest command in a job, comments removed."""

    commands = []
    for step in job["steps"]:
        for line in step.get("run", "").replace("\\\n", " ").splitlines():
            words = shlex.split(line, comments=True)
            if words[:1] == ["pytest"]:
                commands.append(words)
    return commands


def test_the_core_job_lists_its_suites_positively(ci: dict[str, Any]) -> None:
    """A file under an ``--ignore``d directory is dropped silently even when it
    is named, so the core job names what it runs instead of ignoring."""

    suite, mcp = _pytest_commands(ci["jobs"]["core"])
    assert not [word for word in suite + mcp if word.startswith("--ignore")]
    assert "tests/integration/desktop/test_draft_process_acceptance.py" in suite
    assert "tests/test_*.py" in suite
    for foreign in (
        "tests",
        "tests/ci",
        "tests/containers",
        "tests/integration",
        "tests/packaging",
        "tests/test_mcp",
    ):
        assert foreign not in suite, foreign
    assert mcp[:2] == ["pytest", "tests/test_mcp"]


def test_the_lint_job_runs_the_harness_suites_without_skips(
    ci: dict[str, Any],
) -> None:
    runs = [
        _flatten(step.get("run", "")) for step in ci["jobs"]["lint-and-format"]["steps"]
    ]
    (suite,) = [run for run in runs if "pytest" in run]
    assert "pytest tests/ci tests/containers " in suite
    assert '--junitxml="$RUNNER_TEMP/reports/ci-harness.junit.xml"' in suite
    (check,) = [run for run in runs if "verify_desktop_ci.py" in run]
    assert check == (
        "python tests/ci/verify_desktop_ci.py junit-report "
        '--path "$RUNNER_TEMP/reports/ci-harness.junit.xml" '
        "--prefix tests.ci. --prefix tests.containers."
    )
    assert runs.index(check) > runs.index(suite)
    assert 'python -m pip install -e ".[dev]"' in runs


#: The one site build, run by the ci.yml docs lane on every pull request that
#: selects it and by docs.yml before each Pages deploy.
SITE_BUILD_COMMANDS = ["npm ci --prefix site", "npm run build --prefix site"]
SETUP_NODE = "actions/setup-node@249970729cb0ef3589644e2896645e5dc5ba9c38"


def _run_commands(job: dict[str, Any]) -> list[str]:
    return [step["run"] for step in job["steps"] if "run" in step]


def _setup_node_inputs(job: dict[str, Any]) -> dict[str, Any]:
    (step,) = [
        step
        for step in job["steps"]
        if str(step.get("uses", "")).startswith(SETUP_NODE)
    ]
    return step["with"]


def test_the_docs_lane_builds_the_locked_site_strictly(
    ci: dict[str, Any],
) -> None:
    job = ci["jobs"]["docs"]
    assert job["name"] == "Docs build"
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == 10
    assert job["permissions"] == {"contents": "read"}
    assert "outputs" not in job
    commands = _run_commands(job)
    assert commands[:2] == SITE_BUILD_COMMANDS
    # Node 22.12 or newer is Astro's floor; "22" resolves to the newest 22.x,
    # the same line the desktop jobs use.  No package-manager cache, as in
    # every other Node job, so a restored cache never feeds a deploy.
    assert _setup_node_inputs(job) == {
        "node-version": "22",
        "package-manager-cache": False,
    }
    for step in job["steps"]:
        if "uses" in step and "checkout" in step["uses"]:
            assert step["with"]["persist-credentials"] is False


def test_the_deploy_builds_exactly_what_the_docs_lane_checks(
    ci: dict[str, Any],
) -> None:
    """The pull-request lane only proves the deploy if both run one build, so
    the two jobs must run the same commands with the same Node setup."""

    docs = _load(ROOT / ".github/workflows/docs.yml")
    # yaml parses the bare ``on`` key as True.
    assert docs[True] == {"push": {"branches": ["main"]}, "workflow_dispatch": None}
    # npm install scripts run in the build job, so only deploy may write
    # Pages or mint an OIDC token.
    assert docs["permissions"] == {"contents": "read"}
    assert docs["jobs"]["build"]["permissions"] == {"contents": "read"}
    assert docs["jobs"]["deploy"]["permissions"] == {
        "pages": "write",
        "id-token": "write",
    }
    build = docs["jobs"]["build"]
    # The lane runs the deploy's build, then its own Markdown lint, which
    # test_docs_lane_contract pins.
    deploy_commands = _run_commands(build)
    lane_commands = _run_commands(ci["jobs"]["docs"])
    assert lane_commands[: len(deploy_commands)] == deploy_commands
    assert all(
        ".github/linters" in command
        for command in lane_commands[len(deploy_commands) :]
    )
    assert _setup_node_inputs(build) == _setup_node_inputs(ci["jobs"]["docs"])
    (upload,) = [
        step
        for step in build["steps"]
        if "upload-pages-artifact" in str(step.get("uses", ""))
    ]
    assert upload["with"] == {"path": "site/dist"}
    assert docs["jobs"]["deploy"]["needs"] == "build"
    assert "mkdocs" not in (ROOT / "pyproject.toml").read_text()
    assert not (ROOT / "mkdocs.yml").exists()


def test_the_site_toolchain_is_pinned_exactly_and_locked() -> None:
    """``npm ci`` installs from the committed lockfile, and Astro and Starlight
    are 0.x or fast-moving majors whose minors break configuration, so every
    direct dependency is an exact version rather than a range."""

    package = json.loads((ROOT / "site/package.json").read_text())
    assert (ROOT / "site/package-lock.json").is_file()
    assert package["scripts"]["build"]
    dependencies = {
        **package.get("dependencies", {}),
        **package.get("devDependencies", {}),
    }
    assert {"astro", "@astrojs/starlight"} <= set(dependencies)
    ranged = {
        name: version
        for name, version in dependencies.items()
        if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.]+)?", version) is None
    }
    assert ranged == {}


def _gate_steps(ci: dict[str, Any]) -> list[dict[str, Any]]:
    return ci["jobs"][GATE_JOB]["steps"]


def test_the_aggregate_always_runs_and_recomputes_the_plan(
    ci: dict[str, Any],
) -> None:
    job = ci["jobs"][GATE_JOB]
    assert job["name"] == GATE_NAME
    assert job["if"] == "${{ always() }}"
    checkout = _gate_steps(ci)[0]
    assert checkout["with"]["fetch-depth"] == 2
    assert checkout["with"]["persist-credentials"] is False
    plan = next(step for step in _gate_steps(ci) if step.get("id") == "plan")
    assert plan["env"] == {
        "UPSTREAM_PLAN": "${{ needs.changes.outputs.plan }}",
        "CHANGES_RESULT": "${{ needs.changes.result }}",
    }
    command = _flatten(plan["run"])
    assert command.startswith("python3 tests/ci/ci_plan.py effective ")
    for argument in (
        '--upstream-json "$UPSTREAM_PLAN"',
        '--changes-result "$CHANGES_RESULT"',
        f"--output {PLAN_PATH}",
        '--github-output "$GITHUB_OUTPUT"',
    ):
        assert argument in command, argument


def test_both_verifiers_read_the_effective_plan(ci: dict[str, Any]) -> None:
    steps = _gate_steps(ci)
    plan_index = next(i for i, step in enumerate(steps) if step.get("id") == "plan")
    verifiers = [
        (index, _flatten(step["run"]))
        for index, step in enumerate(steps)
        if "verify_desktop_ci.py" in str(step.get("run", ""))
        or "verify_desktop_production_gate.py" in str(step.get("run", ""))
    ]
    assert len(verifiers) == 2
    for index, command in verifiers:
        assert index > plan_index
        assert f"--plan {PLAN_PATH}" in command, command
    assert verifiers[0][1].startswith("python3 tests/ci/verify_desktop_ci.py aggregate")


def test_every_download_is_keyed_on_its_owning_lane(ci: dict[str, Any]) -> None:
    steps = _gate_steps(ci)
    owner = {
        check: lane for lane, checks in CI_PLAN.LANE_CHECKS.items() for check in checks
    }
    root_index = next(
        index
        for index, step in enumerate(steps)
        if _flatten(step.get("run", "")) == 'mkdir -p "$RUNNER_TEMP/gate-evidence"'
    )
    downloads = [
        (index, step)
        for index, step in enumerate(steps)
        if "download-artifact" in str(step.get("uses", ""))
    ]
    assert len(downloads) == len(REQUIRED_CHECKS)
    seen = set()
    for index, step in downloads:
        assert index > root_index
        directory = _flatten(step["with"]["path"]).rsplit("/", 1)[1]
        check = next(c for c in REQUIRED_CHECKS if c.evidence_directory == directory)
        lane = owner[check.check_id]
        condition = f"steps.plan.outputs.{lane} == 'true'"
        if check.check_id in CI_PLAN.FULL_PLAN_ONLY_CHECKS:
            # A reduced plan never schedules this leg.
            condition += " && steps.plan.outputs.full == 'true'"
        assert step["if"] == condition, directory
        seen.add(check.check_id)
    assert seen == {check.check_id for check in REQUIRED_CHECKS}


def test_job_results_are_judged_before_and_beside_any_downloaded_evidence(
    ci: dict[str, Any],
) -> None:
    """An earlier attempt's evidence can never stand in for a failing lane.

    Gate artifact names omit the attempt, so after a partial rerun the evidence
    of a lane that uploaded and then failed may still be present.  The lane job
    results, which are always the latest attempt's, are checked before any
    download and again by the final verifier, and the final verifier bounds
    every receipt's attempt by the aggregate's own.
    """

    steps = _gate_steps(ci)
    job = ci["jobs"][GATE_JOB]
    assert job["env"]["DESKTOP_GATE_RESULTS"] == "${{ toJSON(needs) }}"
    assert "needs.desktop-production.outputs.job-results" in _flatten(
        job["env"]["DESKTOP_PRODUCTION_RESULTS"]
    )
    aggregate = next(
        index
        for index, step in enumerate(steps)
        if "verify_desktop_ci.py aggregate" in _flatten(step.get("run", ""))
    )
    downloads = [
        index
        for index, step in enumerate(steps)
        if "download-artifact" in str(step.get("uses", ""))
    ]
    assert downloads and aggregate < min(downloads)
    final = next(
        _flatten(step["run"])
        for step in steps
        if "verify_desktop_production_gate.py" in str(step.get("run", ""))
    )
    assert '--attempt "$GITHUB_RUN_ATTEMPT"' in final
    assert '--run-id "$GITHUB_RUN_ID"' in final


def test_the_lane_inputs_are_optional_booleans_defaulting_to_true(
    production: dict[str, Any],
) -> None:
    triggers = production[True]
    for trigger in ("workflow_call", "workflow_dispatch"):
        inputs = triggers[trigger]["inputs"]
        assert set(inputs) == {"checked_out_sha", "run_archive", "run_packaging"}
        for name in ("run_archive", "run_packaging"):
            assert inputs[name]["type"] == "boolean", (trigger, name)
            assert inputs[name]["required"] is False, (trigger, name)
            assert inputs[name]["default"] is True, (trigger, name)
        assert inputs["checked_out_sha"]["required"] is True
    assert set(triggers["workflow_call"]["outputs"]) == {"job-results", "source-tree"}


def test_exactly_the_archive_and_packaging_jobs_are_gated_on_their_inputs(
    production: dict[str, Any],
) -> None:
    for condition, lane in (
        (ARCHIVE_CONDITION, "archive"),
        (PACKAGING_CONDITION, "packaging"),
    ):
        gated = {
            name
            for name, job in production["jobs"].items()
            if job.get("if") == condition
        }
        assert gated == set(CI_PLAN.LANE_PRODUCTION_JOBS[lane]), lane
    ungated = set(production["jobs"]) - (
        CI_PLAN.LANE_PRODUCTION_JOBS["archive"]
        | CI_PLAN.LANE_PRODUCTION_JOBS["packaging"]
    )
    for name in ungated:
        expected = "${{ always() }}" if name == RESULTS_JOB else None
        assert production["jobs"][name].get("if") == expected, name


def test_lane_production_jobs_partition_the_production_workflow(
    production: dict[str, Any],
) -> None:
    lanes = CI_PLAN.LANE_PRODUCTION_JOBS
    owned = [job for jobs in lanes.values() for job in jobs]
    assert len(owned) == len(set(owned))
    assert set(owned) == set(REQUIRED_PRODUCTION_JOBS)
    assert set(production["jobs"]) == set(REQUIRED_PRODUCTION_JOBS) | {RESULTS_JOB}
    # A job may need only jobs of its own lane or of a lane its lane implies,
    # or deselecting a lane would skip part of another.
    for lane, jobs in lanes.items():
        allowed = set()
        for implied in CI_PLAN.close_lanes({lane}):
            allowed |= lanes.get(implied, frozenset())
        for name in jobs:
            needs = set(production["jobs"][name].get("needs") or [])
            assert needs <= allowed, (lane, name, sorted(needs - allowed))


def test_every_check_is_owned_by_the_lane_of_its_job() -> None:
    owner = {
        check: lane for lane, checks in CI_PLAN.LANE_CHECKS.items() for check in checks
    }
    assert set(owner) == {check.check_id for check in REQUIRED_CHECKS}
    for check in REQUIRED_CHECKS:
        lane = owner[check.check_id]
        if check.workflow == "ci":
            assert CI_PLAN.LANE_CI_JOBS[lane] == check.job, check.check_id
        else:
            assert check.job in CI_PLAN.LANE_PRODUCTION_JOBS[lane], check.check_id


def test_required_production_jobs_equal_the_results_needs(
    production: dict[str, Any],
) -> None:
    needs = set(production["jobs"][RESULTS_JOB]["needs"])
    assert needs == set(REQUIRED_PRODUCTION_JOBS)
    assert RESULTS_JOB not in needs


def test_binder_publications_match_the_configured_checks(
    ci: dict[str, Any], production: dict[str, Any]
) -> None:
    published = {**_binder_publications(ci), **_binder_publications(production)}
    binder_checks = {
        check.check_id
        for check in REQUIRED_CHECKS
        if check.check_id not in {"desktop-archive-lifecycle", "desktop-archive-sbom"}
    }
    assert set(published) == binder_checks

    for check_id, record in published.items():
        check = _check(check_id)
        arguments = record["arguments"]
        plan = record["plan"]
        assert record["job"] == check.job, check_id
        assert arguments["--receipt-name"] == check.receipt_name, check_id
        assert arguments["--output-root"].endswith(f"/{check.evidence_directory}"), (
            check_id
        )
        assert plan["check_id"] == check_id
        assert [stage["name"] for stage in plan["stages"]] == list(check.stages)
        lifecycle = [
            report.path
            for report in check.reports
            if report.report_format == ARTIFACT_LIFECYCLE
        ]
        assert lifecycle == [plan["report_path"]], check_id
        staged = {entry["path"] for entry in plan["files"]}
        expected = {
            report.path
            for report in check.reports
            if report.report_format != ARTIFACT_LIFECYCLE
        }
        assert expected <= staged, check_id
        if check.exact_pairing_input is not None:
            assert check.exact_pairing_input in staged, check_id


def test_every_gate_artifact_is_uploaded_and_downloaded_under_one_name(
    ci: dict[str, Any], production: dict[str, Any]
) -> None:
    """The aggregate must download exactly what a producer job uploaded."""

    uploads = {
        **_artifact_names(ci, "upload-artifact"),
        **_artifact_names(production, "upload-artifact"),
    }
    published = {_canonical_artifact_name(item) for item in uploads.values()}
    downloads = _artifact_names(ci, "download-artifact")
    for check in REQUIRED_CHECKS:
        source = "${{ runner.temp }}/gate/" + check.evidence_directory
        destination = "${{ runner.temp }}/gate-evidence/" + check.evidence_directory
        assert source in uploads, check.evidence_directory
        assert destination in downloads, check.evidence_directory
        name = _canonical_artifact_name(downloads[destination])
        assert name == _canonical_artifact_name(uploads[source]), (
            check.evidence_directory
        )
        assert name in published, check.evidence_directory
        assert CHECKED_OUT_COMMIT in name, check.evidence_directory
        # The name binds the run but not the attempt, so a partial rerun still
        # finds the evidence of a lane that passed in an earlier attempt.
        assert "github.run_id" in name, check.evidence_directory
        assert "run_attempt" not in name, check.evidence_directory


def _gate_uploads(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    uploads: list[dict[str, Any]] = []
    for job in workflow["jobs"].values():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if "upload-artifact" not in str(step.get("uses", "")):
                continue
            if "/gate/" in _flatten(step["with"].get("path", "")):
                uploads.append(step["with"])
    return uploads


def test_every_gate_upload_overwrites_the_evidence_of_an_earlier_attempt(
    ci: dict[str, Any], production: dict[str, Any]
) -> None:
    """A rerun lane replaces its own evidence under the attempt-free name.

    Without ``overwrite`` a lane that uploaded its evidence and then failed
    could not upload again when it reran.
    """

    uploads = _gate_uploads(ci) + _gate_uploads(production)
    directories = {
        _flatten(upload["path"]).rsplit("/gate/", 1)[1] for upload in uploads
    }
    assert {"core-python-${{ matrix.python-version }}"} | {
        check.evidence_directory
        for check in REQUIRED_CHECKS
        if not check.evidence_directory.startswith("core-python-")
    } == directories
    for upload in uploads:
        assert upload.get("overwrite") is True, upload["name"]


def test_transfer_consumers_bind_the_producer_attempt(
    production: dict[str, Any],
) -> None:
    """A consumer rerun alone must expect the attempt that built the transfer.

    The transfer artifact keeps the attempt in its name, and the archive job
    publishes that attempt beside the name, so the archive lifecycle and SBOM
    jobs never expect their own, later attempt from an earlier transfer.
    """

    archive = production["jobs"]["archive"]
    assert archive["outputs"]["run-attempt"] == "${{ steps.names.outputs.run_attempt }}"
    assert "GITHUB_RUN_ATTEMPT" in _step_output_source(
        archive, "${{ steps.names.outputs.run_attempt }}"
    )
    for name in ("archive-evidence", "archive-sbom"):
        runs = [
            _flatten(step.get("run", ""))
            for step in production["jobs"][name]["steps"]
            if "desktop_production_expectations.py" in str(step.get("run", ""))
        ]
        assert len(runs) == 1, name
        assert (
            '--transfer-attempt "${{ needs.archive.outputs.run-attempt }}"' in runs[0]
        ), name
        assert '--attempt "$GITHUB_RUN_ATTEMPT"' in runs[0], name


def test_the_archive_adapter_constants_anchor_the_gate_policy() -> None:
    check = _check("desktop-archive-lifecycle")
    assert check.receipt_name == archive_adapter().RECEIPT_PATH
    assert [report.path for report in check.reports] == [archive_adapter().REPORT_PATH]
    assert check.stages == tuple(archive_adapter().STAGE_NAMES)


def test_the_sbom_adapter_constants_anchor_the_gate_policy() -> None:
    check = _check("desktop-archive-sbom")
    assert check.stages == tuple(sbom_adapter().STAGE_NAMES)
    assert check.receipt_name == sbom_adapter().RECEIPT_PATH
    assert [report.path for report in check.reports] == [sbom_adapter().REPORT_PATH]


#: ``uses:`` values that name something other than a third-party action and
#: so are never SHA-pinned: a same-repo reusable workflow call, or a
#: container image reference.
_UNPINNED_USES_PREFIXES = ("./", "docker://")


def _workflow_uses(workflow: dict[str, Any]) -> list[str]:
    """Every non-local, non-container ``uses:`` value in a parsed workflow.

    Collects both job-level ``uses`` (a reusable-workflow call) and
    step-level ``uses`` (an action), walking dicts rather than matching line
    prefixes, so it sees list-form steps (``- uses: ...``) the same as any
    other form.
    """

    values: list[str] = []
    for job in (workflow.get("jobs") or {}).values():
        if not isinstance(job, dict):
            continue
        job_uses = job.get("uses")
        if isinstance(job_uses, str) and not job_uses.startswith(
            _UNPINNED_USES_PREFIXES
        ):
            values.append(job_uses)
        for step in job.get("steps") or []:
            step_uses = step.get("uses") if isinstance(step, dict) else None
            if isinstance(step_uses, str) and not step_uses.startswith(
                _UNPINNED_USES_PREFIXES
            ):
                values.append(step_uses)
    return values


def test_every_action_in_every_workflow_is_sha_pinned() -> None:
    """Every non-local ``uses:`` reference, job- or step-level, in every
    workflow must pin a full commit SHA followed by a trailing ``# v<version>``
    comment, no exceptions; see issue #149. This globs every ``*.yml`` and
    ``*.yaml`` file under ``.github/workflows`` instead of an enumerated
    subset, so a new workflow, a new file extension, or a job-level reusable
    workflow call is covered automatically rather than by remembering to add
    it here."""

    workflow_dir = ROOT / ".github/workflows"
    paths = sorted(workflow_dir.glob("*.yml")) + sorted(workflow_dir.glob("*.yaml"))
    unpinned: list[str] = []
    for path in paths:
        text = path.read_text()
        workflow = yaml.safe_load(text)
        for uses in _workflow_uses(workflow):
            reference = uses.split("@", 1)[1] if "@" in uses else ""
            has_sha = re.fullmatch(r"[0-9a-f]{40}", reference) is not None
            has_version_comment = (
                re.search(rf"{re.escape(uses)}[ \t]*#[ \t]*v\S", text) is not None
            )
            if not (has_sha and has_version_comment):
                unpinned.append(f"{path.name}: {uses}")
    assert unpinned == []


def test_every_new_upload_is_retry_safe_and_retained_for_fourteen_days(
    ci: dict[str, Any], production: dict[str, Any]
) -> None:
    """Every upload binds the run and survives a rerun of its job.

    Diagnostic and transfer artifacts carry the attempt in their names, so a
    rerun uploads a new immutable artifact.  Gate evidence omits the attempt,
    so the aggregate finds it in whichever attempt produced it, and overwrites
    on a rerun instead.
    """

    probe = _load(ROOT / ".github/workflows/desktop-podman-probe.yml")
    for workflow in (ci, production, probe):
        for job in workflow["jobs"].values():
            if not isinstance(job, dict):
                continue
            for step in job.get("steps") or []:
                if "upload-artifact" not in str(step.get("uses", "")):
                    continue
                with_block = step["with"]
                name = _step_output_source(job, _flatten(with_block["name"]))
                assert "github.run_id" in name or "GITHUB_RUN_ID" in name, name
                per_attempt = (
                    "github.run_attempt" in name or "GITHUB_RUN_ATTEMPT" in name
                )
                is_gate = "/gate/" in _flatten(with_block.get("path", ""))
                if is_gate:
                    assert not per_attempt, name
                    assert with_block.get("overwrite") is True, name
                else:
                    assert per_attempt, name
                    assert "overwrite" not in with_block, name
                assert with_block["retention-days"] == 14, name


def test_the_two_checked_out_commit_spellings_are_the_same_value(
    ci: dict[str, Any], production: dict[str, Any]
) -> None:
    """Artifact names use two spellings; this proves they cannot diverge.

    Ordinary CI holds the revision in ``TONGS_CHECKED_OUT_SHA`` and passes the
    same expression to the called workflow, whose ``source-identity`` job
    verifies that ``git rev-parse HEAD`` equals it before publishing it as
    ``checked-out-commit``.
    """

    assert ci["env"]["TONGS_CHECKED_OUT_SHA"] == "${{ github.sha }}"
    call = ci["jobs"]["desktop-production"]
    assert call["with"]["checked_out_sha"] == "${{ github.sha }}"
    assert ci["jobs"]["fedora-podman"]["with"]["head_sha"] == "${{ github.sha }}"

    identity = production["jobs"]["source-identity"]
    outputs = identity["outputs"]
    assert outputs["checked-out-commit"] == (
        "${{ steps.identity.outputs.checked_out_commit }}"
    )
    binder = next(step for step in identity["steps"] if step.get("id") == "identity")
    assert (
        'test "$checked_out_commit" = "${{ inputs.checked_out_sha }}"'
        in (binder["run"])
    )
    checkout = next(
        step for step in identity["steps"] if "checkout" in str(step.get("uses", ""))
    )
    assert checkout["with"]["ref"] == "${{ inputs.checked_out_sha }}"
