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
adapters actually export.
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
from tests.ci.verify_desktop_ci import REQUIRED_GATE_JOBS
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
        matrices: list[dict[str, str]] = [{}]
        matrix = (job.get("strategy") or {}).get("matrix") or {}
        if matrix:
            key, values = next(iter(matrix.items()))
            matrices = [{key: str(value)} for value in values]
        for selection in matrices:
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
    matrix = (job.get("strategy") or {}).get("matrix") or {}
    if not matrix:
        return [{}]
    key, values = next(iter(matrix.items()))
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
    assert needs == set(REQUIRED_CI_JOBS)
    assert needs == set(REQUIRED_GATE_JOBS)


def test_required_production_jobs_equal_the_results_needs(
    production: dict[str, Any],
) -> None:
    needs = set(production["jobs"][RESULTS_JOB]["needs"])
    assert needs == set(REQUIRED_PRODUCTION_JOBS)
    assert RESULTS_JOB not in needs


def test_every_configured_check_names_a_job_the_workflow_declares(
    ci: dict[str, Any], production: dict[str, Any]
) -> None:
    for check in REQUIRED_CHECKS:
        workflow = ci if check.workflow == "ci" else production
        assert check.job in workflow["jobs"], check.check_id


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
        assert "github.run_id" in name and "github.run_attempt" in name


def test_the_archive_adapter_constants_anchor_the_gate_policy() -> None:
    check = _check("desktop-archive-lifecycle")
    assert check.receipt_name == archive_adapter().RECEIPT_PATH
    assert [report.path for report in check.reports] == [archive_adapter().REPORT_PATH]
    assert check.stages == tuple(archive_adapter().STAGE_NAMES)


def test_the_sbom_adapter_constants_anchor_the_gate_policy() -> None:
    check = _check("desktop-archive-sbom")
    assert check.stages == tuple(sbom_adapter().STAGE_NAMES)
    assert [report.path for report in check.reports] == ["reports/sbom-evidence.json"]
    assert check.receipt_name == "sbom-receipt.json"


def test_every_action_in_the_required_call_chain_is_sha_pinned() -> None:
    chain = (
        "ci.yml",
        "desktop-production.yml",
        "desktop-podman-probe.yml",
        "release-desktop.yml",
    )
    unpinned: list[str] = []
    for name in chain:
        for line in (ROOT / ".github/workflows" / name).read_text().splitlines():
            stripped = line.strip()
            if not stripped.startswith("uses: actions/"):
                continue
            reference = stripped.split("@", 1)[1].split()[0]
            if re.fullmatch(r"[0-9a-f]{40}", reference) is None:
                unpinned.append(f"{name}: {stripped}")
    assert unpinned == []


def test_every_new_upload_is_retry_safe_and_retained_for_fourteen_days(
    ci: dict[str, Any], production: dict[str, Any]
) -> None:
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
                assert "github.run_attempt" in name or "GITHUB_RUN_ATTEMPT" in name, (
                    name
                )
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
